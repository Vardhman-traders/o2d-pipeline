"""Small helpers shared by the Payments, Delegation and Purchase modules."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg2 import errors as pgerr

from . import config

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> date:
    return datetime.now(IST).date()


def date_key(d: date | None) -> int | None:
    return int(d.strftime("%Y%m%d")) if d else None


def parse_date(v, field: str = "date") -> date | None:
    """'2026-10-07' (or a date) -> date. Empty -> None. Anything else is a clear 422, never a server error."""
    if v in (None, ""):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise HTTPException(422, f"{field} is not a valid date (use yyyy-mm-dd).")


def ist_date(dt: datetime | None) -> date | None:
    return dt.astimezone(IST).date() if dt else None


def iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def num(v) -> float | None:
    return None if v is None else float(v)


def int_setting(key: str, default: int) -> int:
    """A number an admin keeps under Setup > Module settings (falls back to the starting value)."""
    try:
        return int(float(config.get(key) or default))
    except (TypeError, ValueError):
        return default


def date_fk_error(exc: Exception):
    """A date outside the calendar table (2024-2035) reaches Postgres as a foreign-key error: say so plainly."""
    if isinstance(exc, pgerr.ForeignKeyViolation):
        return HTTPException(400, "That date is outside the supported calendar (2024-2035) or a chosen value no longer exists.")
    return exc


def lookup_key(cur, table: str, key: str, name_col: str, name: str | None) -> int | None:
    """Case/space-insensitive name -> key for a master list (None when blank or not found)."""
    if not name or not str(name).strip():
        return None
    cur.execute(f'SELECT "{key}" AS k FROM "{table}" WHERE lower(btrim("{name_col}")) = lower(btrim(%s))', (name,))
    row = cur.fetchone()
    return row["k"] if row else None
