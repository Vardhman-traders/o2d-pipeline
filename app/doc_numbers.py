"""Bill numbers: the two series, the rules on entering them, and finding the ones nobody has punched.

Challan  - a daily bill; the number restarts at 1 every day.
Invoice  - the BUSY bill; one running series per financial year (1 April to 31 March).
The submission type of an order says which series it belongs to - an admin setting on each type (Setup > Dropdown values >
Submission type > "Bill number series", column dim_submission_type.bill_series). Orders of any other type are free-form.
"""
from datetime import date, timedelta

from fastapi import HTTPException

from . import roles

SERIES = {"challan": "Challan", "invoice": "Invoice"}
MAX_LISTED = 200          # a typo like 99999 must not produce a list of thousands of "missing" numbers


def series_of(cur, type_name: str | None) -> str | None:
    """'Challan' / 'Invoice' when this submission type is a numbered bill (per the admin setting), else None."""
    name = (type_name or "").strip()
    if not name:
        return None
    cur.execute("SELECT bill_series FROM dim_submission_type WHERE lower(btrim(type_name)) = lower(%s)", (name,))
    row = cur.fetchone()
    return SERIES.get(row["bill_series"]) if row and row["bill_series"] else None


def types_by_series(cur) -> dict[str, list[str]]:
    """{'Challan': [type names], 'Invoice': [...]} - for the screens (which type to pre-select when a missing number is clicked)."""
    cur.execute("SELECT type_name, bill_series FROM dim_submission_type WHERE bill_series IS NOT NULL ORDER BY sort_order, lower(type_name)")
    out: dict[str, list[str]] = {label: [] for label in SERIES.values()}
    for r in cur.fetchall():
        out[SERIES[r["bill_series"]]].append(r["type_name"])
    return out


def fy_label(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def fy_bounds(d: date) -> tuple[date, date]:
    start = d.year if d.month >= 4 else d.year - 1
    return date(start, 4, 1), date(start + 1, 3, 31)


def series_key(doc_type: str, d: date) -> str:
    return d.isoformat() if doc_type == "Challan" else fy_label(d)


def _window(doc_type: str, d: date) -> tuple[date, date]:
    return (d, d) if doc_type == "Challan" else fy_bounds(d)


# ------------------------------------------------------------------ rules when an order is saved
def validate(cur, user, *, dc_no: str | None, type_name: str | None, order_date: date | None, own_order_key=None):
    """Challan / Invoice numbers must be digits and must not repeat inside their series. Test accounts are exempt:
    they tag their orders TEST-... so they stay apart from real data."""
    doc_type = series_of(cur, type_name)
    dc = (dc_no or "").strip()
    if not doc_type or not dc or order_date is None or roles.is_test_user(user):
        return
    if not dc.isdigit():
        raise HTTPException(400, f"A {doc_type} number must be digits only (got \"{dc}\").")
    lo, hi = _window(doc_type, order_date)
    cur.execute("SELECT o.sl_no, d.full_date FROM fact_orders o JOIN dim_date d ON d.date_key = o.order_received_date_key "
                "JOIN dim_submission_type st ON st.submission_type_key = o.submission_type_key "
                "WHERE st.bill_series = %s AND ltrim(o.dc_inv_no, '0') = ltrim(%s, '0') "
                "AND d.full_date BETWEEN %s AND %s AND (%s::int IS NULL OR o.order_key <> %s::int) LIMIT 1",
                (doc_type.lower(), dc, lo, hi, own_order_key, own_order_key))
    dup = cur.fetchone()
    if dup:
        where = f"on {dup['full_date']:%d-%m-%Y}" if doc_type == "Challan" else f"in FY {fy_label(order_date)}"
        raise HTTPException(409, f"{doc_type} {dc} is already used {where} (Sl {dup['sl_no']}).")


# ------------------------------------------------------------------ finding gaps
def _present(cur, doc_type: str, lo: date, hi: date) -> dict[int, date]:
    cur.execute("SELECT v.dc_inv_no, v.order_date FROM v_orders_archive v "
                "WHERE lower(btrim(v.submission_type)) IN (SELECT lower(btrim(type_name)) FROM dim_submission_type WHERE bill_series = %s) "
                "AND v.dc_inv_no ~ '^[0-9]+$' AND v.order_date BETWEEN %s AND %s", (doc_type.lower(), lo, hi))
    out: dict[int, date] = {}
    for r in cur.fetchall():
        out.setdefault(int(r["dc_inv_no"]), r["order_date"])
    return out


def _voided(cur, doc_type: str, key: str) -> set[int]:
    cur.execute("SELECT doc_no FROM doc_gap_void WHERE doc_type = %s AND series_key = %s", (doc_type, key))
    return {r["doc_no"] for r in cur.fetchall()}


def _ranges(missing: list[int], present: dict[int, date]) -> list[dict]:
    out: list[dict] = []
    for n in missing:
        if out and out[-1]["to"] == n - 1:
            out[-1]["to"] = n
            out[-1]["count"] += 1
        else:
            prev = present.get(n - 1)  # the number just before the hole, so people know roughly when it was skipped
            out.append({"from": n, "to": n, "count": 1, "afterDate": prev.isoformat() if prev else None})
    return out


def _gaps_in(cur, doc_type: str, key: str, present: dict[int, date], start: int, voided: set[int] | None = None) -> dict | None:
    if not present:
        return None
    hi = max(present)
    voided = _voided(cur, doc_type, key) if voided is None else voided
    missing = [n for n in range(start, hi + 1) if n not in present and n not in voided]
    if not missing:
        return None
    # a long list keeps the NEWEST gaps: old holes (e.g. before the records were imported) must not bury a number skipped today
    return {"series": key, "highest": hi, "total": len(missing), "ranges": _ranges(missing[-MAX_LISTED:], present),
            "truncated": len(missing) > MAX_LISTED}


MAX_DAYS_LISTED = 60      # newest days with open gaps shown; older ones are only counted (clear them with "No bill / voided")


def open_gaps(cur, today: date, days: int | None = None) -> dict:
    """Challan: every day that still has an open gap, newest first, each counted from 1 - a day drops off once its numbers are entered
    or closed ("No bill / voided"), however old it is (`days` limits how far back, default: all). Invoice: this financial year (last
    year's too during April), counted from 1, since BUSY restarts the series every 1 April."""
    challan = []
    rows = _present_by_day(cur, today - timedelta(days=days - 1) if days else None, today)
    cur.execute("SELECT series_key, doc_no FROM doc_gap_void WHERE doc_type = 'Challan'")
    voids: dict[str, set[int]] = {}
    for r in cur.fetchall():
        voids.setdefault(r["series_key"], set()).add(r["doc_no"])
    for d in sorted(rows, reverse=True):
        g = _gaps_in(cur, "Challan", d.isoformat(), rows[d], 1, voids.get(d.isoformat(), set()))
        if g:
            challan.append({"date": d.isoformat(), **g})
    older_days = max(0, len(challan) - MAX_DAYS_LISTED)
    all_challan_total = sum(g["total"] for g in challan)
    challan = challan[:MAX_DAYS_LISTED]
    invoice = []
    years = [today] + ([date(today.year - 1, 4, 1)] if today.month == 4 else [])
    for ref in years:
        lo, hi = fy_bounds(ref)
        present = _present(cur, "Invoice", lo, hi)
        g = _gaps_in(cur, "Invoice", fy_label(ref), present, 1)
        if g:
            invoice.append({"fy": fy_label(ref), **g})
    return {"challan": challan, "invoice": invoice, "olderChallanDays": older_days,
            "total": all_challan_total + sum(g["total"] for g in invoice)}


def _present_by_day(cur, lo: date | None, hi: date) -> dict[date, dict[int, date]]:
    cur.execute("SELECT v.dc_inv_no, v.order_date FROM v_orders_archive v "
                "WHERE lower(btrim(v.submission_type)) IN (SELECT lower(btrim(type_name)) FROM dim_submission_type WHERE bill_series = 'challan') "
                "AND v.dc_inv_no ~ '^[0-9]+$' AND v.order_date >= COALESCE(%s::date, DATE '1900-01-01') AND v.order_date <= %s", (lo, hi))
    out: dict[date, dict[int, date]] = {}
    for r in cur.fetchall():
        out.setdefault(r["order_date"], {}).setdefault(int(r["dc_inv_no"]), r["order_date"])
    return out


def check_entry(cur, doc_type: str, order_date: date, number: int) -> dict:
    """Advice for the order form while a number is being typed: is it taken, and does it skip ahead of the series?"""
    lo, hi = _window(doc_type, order_date)
    present = _present(cur, doc_type, lo, hi)
    out = {"duplicate": number in present, "skipped": []}
    if present and number > max(present) + 1:
        voided = _voided(cur, doc_type, series_key(doc_type, order_date))
        out["skipped"] = [n for n in range(max(present) + 1, number) if n not in voided][:20]
        out["last"] = max(present)
    elif not present and doc_type == "Challan" and number > 1:
        out["skipped"] = list(range(1, number))[:20]
        out["last"] = 0
    return out
