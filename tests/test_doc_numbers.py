"""Bill-number rules that need no database: series, financial year, gap ranges, entry advice."""
from datetime import date

import pytest
from fastapi import HTTPException

from app import doc_numbers as dn


class FakeCur:
    """Answers the three queries doc_numbers makes, from plain Python data."""

    def __init__(self, present=None, voided=(), dup=None):
        self.present, self.voided, self.dup, self.rows = present or {}, set(voided), dup, []

    def execute(self, sql, params=()):
        if "FROM v_orders_archive" in sql:
            self.rows = [{"dc_inv_no": str(n), "order_date": d} for n, d in self.present.items()]
        elif "FROM doc_gap_void" in sql:
            self.rows = [{"doc_no": n} for n in self.voided]
        else:
            self.rows = [self.dup] if self.dup else []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


D = date(2026, 10, 2)


def test_series_and_financial_year():
    class _Types:   # the admin setting: which submission types are numbered bills
        series = {"challan": "challan", "invoice": "invoice", "send material": "challan", "cancelled": None}

        def execute(self, sql, params=()):
            self.name = params[0].strip().lower() if params else ""

        def fetchone(self):
            return {"bill_series": self.series[self.name]} if self.name in self.series else None
    cur = _Types()
    assert dn.series_of(cur, " challan ") == "Challan" and dn.series_of(cur, "INVOICE") == "Invoice"
    assert dn.series_of(cur, "Send Material") == "Challan"          # any type can be set up as a bill series
    assert dn.series_of(cur, "Cancelled") is None and dn.series_of(cur, "Unknown type") is None and dn.series_of(cur, None) is None
    assert dn.fy_label(date(2026, 3, 31)) == "2025-26" and dn.fy_label(date(2026, 4, 1)) == "2026-27"
    assert dn.fy_bounds(date(2026, 10, 2)) == (date(2026, 4, 1), date(2027, 3, 31))


def test_challan_gaps_count_from_one_and_group_runs():
    cur = FakeCur(present={3: D, 4: D, 7: D})
    g = dn._gaps_in(cur, "Challan", D.isoformat(), cur.present, 1)
    assert [(r["from"], r["to"]) for r in g["ranges"]] == [(1, 2), (5, 6)] and g["total"] == 4


def test_voided_numbers_are_not_missing():
    cur = FakeCur(present={1: D, 4: D}, voided=[2, 3])
    assert dn._gaps_in(cur, "Invoice", "2026-27", cur.present, 1) is None


def test_validate_rules():
    cur = FakeCur()
    user = {"username": "shop1", "role": "shop"}
    dn.validate(cur, user, dc_no="12", type_name="Challan", order_date=D)                       # fine
    dn.validate(cur, user, dc_no="ABC-1", type_name="Cancelled", order_date=D)                  # other types are free-form
    with pytest.raises(HTTPException) as e:
        dn.validate(cur, user, dc_no="12A", type_name="Invoice", order_date=D)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        dn.validate(FakeCur(dup={"sl_no": 5, "full_date": D}), user, dc_no="12", type_name="Challan", order_date=D)
    assert e.value.status_code == 409


def test_entry_advice_flags_skips_and_duplicates():
    cur = FakeCur(present={1: D, 2: D})
    assert dn.check_entry(cur, "Challan", D, 2)["duplicate"] is True
    out = dn.check_entry(cur, "Challan", D, 5)
    assert out["skipped"] == [3, 4] and out["last"] == 2
    assert dn.check_entry(FakeCur(), "Challan", D, 3)["skipped"] == [1, 2]
