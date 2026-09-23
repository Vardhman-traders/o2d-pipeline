"""DB-backed settings the admin portal manages at runtime: the Apps Script client
key and which order fields each role may write. Both used to be hardcoded (an env
var, and a dict in roles.py); this makes them editable without a redeploy.

A short in-process cache keeps this off the hot path (checked on every order
read/write) - it's per-process, so on a multi-instance deploy a change can take up
to TTL seconds to reach every instance. Fine for a single small Render service.
"""
import time

from . import db

TTL = 30
CLIENT_KEY_CONFIG_KEY = "apps_script_client_key"

_value_cache: dict = {}
_value_cache_at: dict = {}


def get(key, default=None):
    now = time.time()
    if key in _value_cache and now - _value_cache_at.get(key, 0) < TTL:
        return _value_cache[key]
    with db.cursor() as cur:
        cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
        row = cur.fetchone()
    value = row["value"] if row else default
    _value_cache[key] = value
    _value_cache_at[key] = now
    return value


def set(key, value, updated_by=None):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_config (key, value, updated_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
            (key, value, updated_by))
    _value_cache[key] = value
    _value_cache_at[key] = time.time()


# Every field an order can carry - the canonical list used both for admin's
# unrestricted access and to validate the Permissions screen's role x field matrix.
ALL_ORDER_FIELDS = frozenset({
    "order_received_date", "order_via_key", "submission_type_key", "dc_inv_no",
    "shipping_location", "detailed_remarks", "ready_by_person_key", "colour_making_person_key",
    "delivery_status_key", "material_delivery_datetime", "delivered_by_person_key", "cartage",
    "date_of_receiving", "payment_status_key", "amount_received",
})

_perm_cache: "dict | None" = None
_perm_cache_at = 0.0


def editable_fields_for_role(role: str) -> set:
    if role == "admin":  # super-user: every field, on every screen
        return set(ALL_ORDER_FIELDS)
    global _perm_cache, _perm_cache_at
    now = time.time()
    if _perm_cache is None or now - _perm_cache_at > TTL:
        with db.cursor() as cur:
            cur.execute("SELECT role, field_name FROM role_field_permissions WHERE editable")
            rows = cur.fetchall()
        m: dict = {}
        for r in rows:
            m.setdefault(r["role"], set()).add(r["field_name"])
        _perm_cache = m
        _perm_cache_at = now
    return _perm_cache.get(role, set())


def all_field_permissions() -> list:
    """Every (role, field_name, editable) row, for the admin Permissions screen."""
    with db.cursor() as cur:
        cur.execute("SELECT role, field_name, editable FROM role_field_permissions ORDER BY role, field_name")
        return cur.fetchall()


def set_field_permission(role: str, field_name: str, editable: bool):
    global _perm_cache
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO role_field_permissions (role, field_name, editable) VALUES (%s, %s, %s) "
            "ON CONFLICT (role, field_name) DO UPDATE SET editable = EXCLUDED.editable",
            (role, field_name, editable))
    _perm_cache = None  # invalidate; next read repopulates
