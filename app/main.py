"""Vardhman Traders API. Run: uvicorn app.main:app"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from psycopg2 import errors as pgerr
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import admin, auth, config, db, roles


@asynccontextmanager
async def lifespan(app):
    auth.check_config()
    db.init_pool()
    yield
    db.close_pool()


# API docs are a map of the system: only expose them when explicitly asked (ENABLE_DOCS=1).
_docs = os.environ.get("ENABLE_DOCS") == "1"
app = FastAPI(title="Vardhman Traders API", lifespan=lifespan,
              docs_url="/docs" if _docs else None, redoc_url=None,
              openapi_url="/openapi.json" if _docs else None)

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = CSP
    if request.url.path.startswith(("/auth", "/admin", "/orders", "/lookups", "/people", "/links")):
        response.headers["Cache-Control"] = "no-store"
    return response

LOOKUPS = config.LOOKUPS  # shared with admin.py's CRUD over the same tables
PERSON_ROLES = Literal["ready_by", "colour_making", "delivery"]
ANY_ORDER_ROLE = auth.require_roles(*roles.ORDER_ROLES)
# Read-only: lets admin view every order (roles.VISIBILITY["admin"] = unrestricted),
# without touching write access - admin has no entry in EDITABLE_FIELDS, so
# create/update stay blocked regardless of which dependency guards the route.
# admin, cashier, accounts, cartage all get read-only order visibility (roles.VISIBILITY);
# none of them can actually write anything (no role_field_permissions rows), so including
# them here only ever grants viewing, whichever write endpoint this guards.
ANY_ORDER_ROLE_OR_ADMIN = auth.require_roles(*roles.ORDER_ROLES, "admin", "cashier", "accounts", "cartage")


def date_key(d: Optional[date]) -> Optional[int]:
    return int(d.strftime("%Y%m%d")) if d else None


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


@app.get("/health")
def health():
    with db.cursor() as cur:
        cur.execute("SELECT 1")
    return {"ok": True}


@app.post("/auth/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else None
    auth.check_not_locked(body.username, ip)
    user = auth.authenticate(body.username, body.password)
    if not user:
        auth.record_failure(body.username, ip)
        raise HTTPException(401, "Invalid username or password")
    auth.clear_failures(body.username, ip)
    return {"access_token": auth.make_token(user), "token_type": "bearer",
            "must_change_password": user["must_change_password"],
            "user": {"username": user["username"], "role": user["role"],
                     "display_name": user["display_name"]}}


@app.get("/auth/me")
def me(user=Depends(auth.current_user)):
    # editable_fields lets the front end show/hide fields based on whatever admin
    # has actually granted this role in Setup -> Permissions, instead of a
    # hardcoded assumption baked into the UI.
    return {**user, "editable_fields": sorted(config.editable_fields_for_role(user["role"]))}


class SsoExchangeIn(BaseModel):
    ticket: str = Field(max_length=100)


@app.post("/auth/sso-ticket")
def sso_ticket(user=Depends(auth.current_user)):
    """Mints a 90-second, one-time ticket so the portal can hand this session off to an
    Apps Script app without putting the real (12-hour) bearer token in a URL."""
    return {"ticket": auth.create_sso_ticket(user["user_key"])}


@app.post("/auth/sso-exchange")
def sso_exchange(body: SsoExchangeIn, _ck=Depends(auth.require_client_key)):
    user_key = auth.redeem_sso_ticket(body.ticket)
    if not user_key:
        raise HTTPException(401, "This link has expired or was already used. Please sign in again.")
    with db.cursor() as cur:
        cur.execute("SELECT user_key, username, role, display_name, must_change_password, password_hash "
                    "FROM dim_user WHERE user_key = %s", (user_key,))
        user = cur.fetchone()
    if not user or user["password_hash"] == auth.DISABLED_HASH:
        raise HTTPException(401, "Unknown or disabled user")
    return {"access_token": auth.make_token(user), "token_type": "bearer",
            "must_change_password": user["must_change_password"],
            "user": {"username": user["username"], "role": user["role"],
                     "display_name": user["display_name"]}}


@app.post("/auth/change-password")
def change_password(body: ChangePasswordIn, user=Depends(auth.current_user)):
    if body.new_password == body.current_password:
        raise HTTPException(422, "New password must differ from the current one")
    with db.cursor() as cur:
        cur.execute("SELECT password_hash FROM dim_user WHERE user_key = %s", (user["user_key"],))
        if not auth.verify_password(body.current_password, cur.fetchone()["password_hash"]):
            raise HTTPException(401, "Current password is incorrect")
        cur.execute("UPDATE dim_user SET password_hash = %s, must_change_password = false WHERE user_key = %s",
                    (auth.hash_password(body.new_password), user["user_key"]))
    return {"ok": True}


# ---------------------------------------------------------------- lookups
class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean(cls, v):
        v = " ".join(v.split())
        if not v:
            raise ValueError("name must not be blank")
        return v


def _lookup(kind, user):
    if kind not in LOOKUPS or not roles.can_access(user, roles.LOOKUP_ACCESS[kind]):
        # same answer for unknown and forbidden, so nothing is revealed
        raise HTTPException(404, "Unknown lookup")
    return LOOKUPS[kind]


@app.get("/lookups/{kind}")
def list_lookup(kind: str, user=Depends(ANY_ORDER_ROLE_OR_ADMIN), _ck=Depends(auth.require_client_key)):
    table, key, name = _lookup(kind, user)
    with db.cursor() as cur:
        cur.execute(f"SELECT {key} AS key, {name} AS name FROM {table} ORDER BY lower({name})")
        return cur.fetchall()


@app.post("/lookups/{kind}")
def get_or_create_lookup(kind: str, body: NameIn, user=Depends(ANY_ORDER_ROLE_OR_ADMIN), _ck=Depends(auth.require_client_key)):
    """Return the existing value (matched ignoring case/spacing) or create it."""
    table, key, name = _lookup(kind, user)
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO {table} ({name}) VALUES (%s) "
            f"ON CONFLICT (lower(btrim({name}))) DO NOTHING RETURNING {key} AS key, {name} AS name",
            (body.name,))
        row = cur.fetchone()
        created = row is not None
        if not created:
            cur.execute(f"SELECT {key} AS key, {name} AS name FROM {table} "
                        f"WHERE lower(btrim({name})) = lower(%s)", (body.name,))
            row = cur.fetchone()
    return {**row, "created": created}


class PersonIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=150)
    person_role: PERSON_ROLES
    phone_number: Optional[str] = Field(default=None, max_length=30)

    @field_validator("full_name")
    @classmethod
    def clean(cls, v):
        v = " ".join(v.split())
        if not v:
            raise ValueError("full_name must not be blank")
        return v


def _check_people_role(person_role, user):
    if not roles.can_access(user, roles.PEOPLE_ACCESS[person_role]):
        raise HTTPException(403, "Your role is not allowed to use this list")


@app.get("/people")
def list_people(role: PERSON_ROLES, user=Depends(ANY_ORDER_ROLE_OR_ADMIN), _ck=Depends(auth.require_client_key)):
    _check_people_role(role, user)
    with db.cursor() as cur:
        cur.execute("SELECT person_key AS key, full_name, phone_number, person_role FROM dim_person "
                    "WHERE person_role = %s ORDER BY lower(full_name)", (role,))
        return cur.fetchall()


@app.post("/people")
def get_or_create_person(body: PersonIn, user=Depends(ANY_ORDER_ROLE_OR_ADMIN), _ck=Depends(auth.require_client_key)):
    _check_people_role(body.person_role, user)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO dim_person (full_name, phone_number, person_role) VALUES (%s, %s, %s) "
            "ON CONFLICT (lower(btrim(full_name)), person_role) DO NOTHING "
            "RETURNING person_key AS key, full_name, phone_number, person_role",
            (body.full_name, body.phone_number, body.person_role))
        row = cur.fetchone()
        created = row is not None
        if not created:
            cur.execute(
                "SELECT person_key AS key, full_name, phone_number, person_role FROM dim_person "
                "WHERE lower(btrim(full_name)) = lower(%s) AND person_role = %s",
                (body.full_name, body.person_role))
            row = cur.fetchone()
    return {**row, "created": created}


# ---------------------------------------------------------------- orders
ORDER_FROM = """
FROM fact_orders o
JOIN dim_date d ON d.date_key = o.order_received_date_key
LEFT JOIN dim_date rd ON rd.date_key = o.date_of_receiving_key
LEFT JOIN dim_order_channel c ON c.channel_key = o.order_via_key
LEFT JOIN dim_submission_type st ON st.submission_type_key = o.submission_type_key
LEFT JOIN dim_delivery_status ds ON ds.status_key = o.delivery_status_key
LEFT JOIN dim_payment_status ps ON ps.payment_status_key = o.payment_status_key
LEFT JOIN dim_person rp ON rp.person_key = o.ready_by_person_key
LEFT JOIN dim_person cp ON cp.person_key = o.colour_making_person_key
LEFT JOIN dim_person dp ON dp.person_key = o.delivered_by_person_key
LEFT JOIN dim_user cu ON cu.user_key = o.created_by_user_key
LEFT JOIN dim_user lu ON lu.user_key = o.last_updated_by_user_key
"""

ORDER_SELECT = """
SELECT o.order_key, o.sl_no, o.dc_inv_no,
       d.full_date AS order_received_date,
       o.order_via_key, c.channel_name AS order_via,
       o.submission_type_key, st.type_name AS submission_type,
       o.delivery_status_key, ds.status_name AS delivery_status,
       o.ready_by_person_key, rp.full_name AS ready_by,
       o.colour_making_person_key, cp.full_name AS colour_making_by,
       o.delivered_by_person_key, dp.full_name AS delivered_by, dp.phone_number AS delivered_by_phone,
       o.payment_status_key, ps.status_name AS payment_status,
       cu.display_name AS created_by, lu.display_name AS last_updated_by,
       o.shipping_location, o.detailed_remarks, o.timestamp_created,
       o.material_delivery_datetime, rd.full_date AS date_of_receiving,
       o.last_updated_at, o.amount_received, o.cartage, o.is_cancelled
""" + ORDER_FROM

Money = Optional[Decimal]


class OrderFields(BaseModel):
    """Unknown fields are rejected (422) instead of silently ignored."""
    model_config = ConfigDict(extra="forbid")

    dc_inv_no: Optional[str] = Field(default=None, max_length=50)
    order_received_date: Optional[date] = None
    order_via_key: Optional[int] = None
    submission_type_key: Optional[int] = None
    delivery_status_key: Optional[int] = None
    ready_by_person_key: Optional[int] = None
    colour_making_person_key: Optional[int] = None
    delivered_by_person_key: Optional[int] = None
    payment_status_key: Optional[int] = None
    shipping_location: Optional[str] = None
    detailed_remarks: Optional[str] = None
    material_delivery_datetime: Optional[datetime] = None
    date_of_receiving: Optional[date] = None
    amount_received: Money = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    cartage: Money = Field(default=None, ge=0, max_digits=10, decimal_places=2)


class OrderIn(OrderFields):
    order_received_date: date  # required on create


class OrderPatch(OrderFields):
    pass


PLAIN_COLUMNS = {
    "dc_inv_no", "order_via_key", "submission_type_key", "delivery_status_key",
    "ready_by_person_key", "colour_making_person_key", "delivered_by_person_key",
    "payment_status_key", "shipping_location", "detailed_remarks",
    "material_delivery_datetime", "amount_received", "cartage",
}
# is_cancelled is never client-writable: it follows the delivery status.
CANCELLED_SQL = ("COALESCE((SELECT lower(btrim(status_name)) = 'cancelled' "
                 "FROM dim_delivery_status WHERE status_key = %s), false)")


def _db_columns(fields: dict) -> dict:
    cols = {k: v for k, v in fields.items() if k in PLAIN_COLUMNS}
    if "order_received_date" in fields:
        cols["order_received_date_key"] = date_key(fields["order_received_date"])
    if "date_of_receiving" in fields:
        cols["date_of_receiving_key"] = date_key(fields["date_of_receiving"])
    return cols


def _check_editable(fields: dict, user):
    denied = sorted(set(fields) - config.editable_fields_for_role(user["role"]))
    if denied:
        raise HTTPException(403, f"Your role ({user['role']}) may not set: {', '.join(denied)}")


def _check_test_dc(fields: dict, user):
    """Test accounts must tag their orders so they stay separate from real data."""
    if roles.is_test_user(user) and "dc_inv_no" in fields and \
            not (fields["dc_inv_no"] or "").upper().startswith(roles.TEST_DC_PREFIX):
        raise HTTPException(400, f"Test accounts must use a DC/Inv No starting with {roles.TEST_DC_PREFIX}")


def _fk_error(exc):
    detail = getattr(exc.diag, "message_detail", "") or ""
    return HTTPException(400, f"Invalid reference (id or date not found): {detail}")


@app.get("/orders")
def list_orders(
    delivery_status_key: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    q: Optional[str] = Query(default=None, max_length=100,
                             description="matches DC/Inv no or shipping location"),
    include_cancelled: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user=Depends(ANY_ORDER_ROLE_OR_ADMIN),
    _ck=Depends(auth.require_client_key),
):
    vis_sql, vis_params = roles.visibility(user)
    where, params = [vis_sql], list(vis_params)
    if delivery_status_key is not None:
        where.append("o.delivery_status_key = %s"); params.append(delivery_status_key)
    if date_from:
        where.append("d.full_date >= %s"); params.append(date_from)
    if date_to:
        where.append("d.full_date <= %s"); params.append(date_to)
    if q:
        where.append("(o.dc_inv_no ILIKE %s OR o.shipping_location ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if not include_cancelled:
        where.append("NOT o.is_cancelled")
    sql = ORDER_SELECT + " WHERE " + " AND ".join(where) + \
        " ORDER BY o.timestamp_created DESC, o.order_key DESC LIMIT %s OFFSET %s"
    with db.cursor() as cur:
        cur.execute(sql, params + [limit, offset])
        return cur.fetchall()


@app.get("/orders/{sl_no}")
def get_order(sl_no: int, user=Depends(ANY_ORDER_ROLE_OR_ADMIN), _ck=Depends(auth.require_client_key)):
    vis_sql, vis_params = roles.visibility(user)
    with db.cursor() as cur:
        cur.execute(ORDER_SELECT + f" WHERE o.sl_no = %s AND {vis_sql}", [sl_no] + vis_params)
        row = cur.fetchone()
    if not row:  # missing and not-yours look identical
        raise HTTPException(404, "Order not found")
    return row


@app.post("/orders", status_code=201)
def create_order(body: OrderIn, user=Depends(auth.require_roles(*roles.CREATE_ORDER_ROLES)), _ck=Depends(auth.require_client_key)):
    fields = body.model_dump(exclude_unset=True)
    _check_editable(fields, user)
    _check_test_dc({"dc_inv_no": fields.get("dc_inv_no")} if roles.is_test_user(user) else {}, user)
    cols = _db_columns(fields)
    cols["created_by_user_key"] = cols["last_updated_by_user_key"] = user["user_key"]
    names = list(cols)
    try:
        with db.cursor() as cur:
            # Serialise serial-number assignment so concurrent creates can't collide.
            cur.execute("SELECT pg_advisory_xact_lock(7001)")
            cur.execute("SELECT COALESCE(MAX(sl_no), 0) + 1 AS n FROM fact_orders")
            sl_no = cur.fetchone()["n"]
            cur.execute(
                f"INSERT INTO fact_orders (sl_no, timestamp_created, last_updated_at, {', '.join(names)}) "
                f"VALUES (%s, now(), now(), {', '.join(['%s'] * len(names))})",
                [sl_no] + [cols[n] for n in names])
            cur.execute(ORDER_SELECT + " WHERE o.sl_no = %s", (sl_no,))
            return cur.fetchone()
    except pgerr.ForeignKeyViolation as e:
        raise _fk_error(e)


@app.patch("/orders/{sl_no}")
def update_order(sl_no: int, body: OrderPatch, user=Depends(ANY_ORDER_ROLE_OR_ADMIN), _ck=Depends(auth.require_client_key)):
    fields = body.model_dump(exclude_unset=True)
    _check_editable(fields, user)
    _check_test_dc(fields, user)
    if not fields:
        raise HTTPException(422, "No fields to update")
    if fields.get("order_received_date", True) is None:
        raise HTTPException(422, "order_received_date cannot be null")
    cols = _db_columns(fields)
    sets = [f"{c} = %s" for c in cols]
    params = list(cols.values())
    if "delivery_status_key" in fields:
        sets.append(f"is_cancelled = {CANCELLED_SQL}")
        params.append(fields["delivery_status_key"])
    vis_sql, vis_params = roles.visibility(user)
    try:
        with db.cursor() as cur:
            # Lock the row only if this user is allowed to see it.
            cur.execute(
                "SELECT o.order_key FROM fact_orders o "
                "LEFT JOIN dim_delivery_status ds ON ds.status_key = o.delivery_status_key "
                "LEFT JOIN dim_submission_type st ON st.submission_type_key = o.submission_type_key "
                f"WHERE o.sl_no = %s AND {vis_sql} FOR UPDATE OF o", [sl_no] + vis_params)
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Order not found")
            cur.execute(
                f"UPDATE fact_orders SET {', '.join(sets)}, last_updated_by_user_key = %s, "
                f"last_updated_at = now() WHERE order_key = %s",
                params + [user["user_key"], row["order_key"]])
            cur.execute(ORDER_SELECT + " WHERE o.order_key = %s", (row["order_key"],))
            return cur.fetchone()
    except pgerr.ForeignKeyViolation as e:
        raise _fk_error(e)


# ---------------------------------------------------------------- admin routes + portal pages
app.include_router(admin.router)
app.include_router(admin.public_router)
# Must be last: serves the login/portal pages for any path not matched above.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="portal")
