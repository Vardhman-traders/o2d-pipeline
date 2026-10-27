# ruff: noqa: F811, E501
"""Godown Receiving and Shop Receiving reports, checked with dummy orders on both sides in every payment mode: each report holds only its own
side, the combined reports hold both, and the PDFs build."""
from datetime import timedelta

from app import o2d_reports
from tests.test_client_feedback import _receive
from tests.test_o2d_screens import add, client, ensure_date, login, seed  # noqa: F401  (fixtures + helpers)
from tests.test_order_journey import _dc, _dispatch, _status, people  # noqa: F401  (fixture + helpers)


def _fetch(kind: str, day) -> set:
    spec = o2d_reports.REPORTS[kind]
    rows = o2d_reports.fetch_rows({"role": "admin", "username": "t"}, spec["sections"][0][1], day, day, spec["order"], spec["date_field"])
    return {r["dc_inv_no"] for r in rows}


def test_godown_and_shop_receiving_reports_each_hold_only_their_side(client, people, db_conn):
    h, today = people
    day = today - timedelta(days=33)
    ensure_date(db_conn, day)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO dim_delivery_status (status_name, dispatch_route) VALUES ('Shop', 'shop') ON CONFLICT DO NOTHING")
        for name, split in (("Cash", "cash"), ("Bank", "bank"), ("Paid", None), ("Received signed copy", None)):
            cur.execute("INSERT INTO dim_payment_status (status_name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
            cur.execute("UPDATE dim_payment_status SET payments_split = %s WHERE status_name = %s", (split, name))
    db_conn.commit()

    made: dict = {}
    for side, status, role in (("godown", "Godown to Dispatch", "godown_dispatch"), ("shop", "Shop", "shop_dispatch")):
        for pay in ("Cash", "Cash", "Bank", "Paid", "Received signed copy"):
            dc = _dc()
            sl = add(client, h["shop"], day, dc)["slNo"]
            _status(client, h, sl, status)
            if side == "godown":
                _dispatch(client, h, role, sl)
                _receive(client, h, sl, day, 1000, payment=pay)
            else:   # the shop fills the receiving details on its own Dispatch & Receiving screen
                _dispatch(client, h, role, sl, dateOfReceiving=day.isoformat(), paymentStatus=pay, amountReceived=1000)
            made.setdefault((side, pay), set()).add(dc)

    def side(s, *pays):
        return set().union(*(made[(s, p)] for p in pays))

    assert _fetch("godown-received-cash", day) == side("godown", "Cash")
    assert _fetch("shop-received-cash", day) == side("shop", "Cash")
    assert _fetch("received-bank", day) == side("godown", "Bank") | side("shop", "Bank")
    # only the payment mode "Received signed copy": another non-cash mode ("Paid") is in none of the reports
    assert _fetch("received-other-all", day) == side("godown", "Received signed copy") | side("shop", "Received signed copy")
    assert not {"godown-received-other", "shop-received-other"} & set(o2d_reports.REPORTS)             # the "not cash" reports are gone
    assert [o2d_reports.REPORTS[k]["title"] for k in ("godown-received-cash", "shop-received-cash")] == ["Godown Cash", "Shop Cash"]

    for kind in ("godown-received-cash", "shop-received-cash", "received-bank", "received-other-all"):
        r = client.get(f"/o2d/reports/{kind}.pdf", params={"date_from": day.isoformat(), "date_to": day.isoformat()}, headers=h["admin"])
        assert r.status_code == 200 and r.content.startswith(b"%PDF"), kind


def test_cartage_pdfs_build_for_admin_and_a_cartage_user(client, people, db_conn):
    h, today = people
    day = today - timedelta(days=34)
    ensure_date(db_conn, day)
    sl = add(client, h["shop"], day, _dc())["slNo"]
    _status(client, h, sl, "Godown to Dispatch")
    _dispatch(client, h, "godown_dispatch", sl, cartage=150)
    _receive(client, h, sl, day, 1000)
    for kind, extra in (("cartage-by-employee", {}), ("cartage-detail", {"delivered_by": "Amit"})):
        r = client.get(f"/o2d/reports/{kind}.pdf", params={"date_from": day.isoformat(), "date_to": day.isoformat(), **extra}, headers=h["admin"])
        assert r.status_code == 200 and r.content.startswith(b"%PDF"), (kind, r.text[:200])
    assert client.get("/o2d/reports/cartage-detail.pdf", headers=h["admin"]).status_code == 422      # a delivery person must be chosen
