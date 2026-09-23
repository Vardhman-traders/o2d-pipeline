"""Who may see and change what. Everything role-related lives here so it is easy to audit."""

from . import config

ORDER_ROLES = {"shop", "godown", "shop_dispatch", "godown_dispatch", "receiving"}
# Roles with no built-in operational workflow. "No access" is the default for them,
# not a permanent rule - admin can grant read-only visibility to any of these from
# Setup -> Permissions (role_view_access table). "admin" itself always sees everything
# regardless (see VISIBILITY below); "legacy" is disabled-login historical accounts,
# never assignable.
NO_ACCESS_ROLES = {"admin", "cashier", "accounts", "cartage", "legacy"}
ALL_ROLES = ORDER_ROLES | NO_ACCESS_ROLES

_DISPATCH = {"delivery_status_key", "material_delivery_datetime", "delivered_by_person_key", "cartage"}
_RECEIVING = {"date_of_receiving", "payment_status_key", "amount_received"}

# Historical default / seed data for the role_field_permissions table (migration 006).
# No longer read at request time - app/main.py's _check_editable() now calls
# config.editable_fields_for_role(), which is DB-backed and editable from the admin
# dashboard's Setup -> Permissions screen. Kept here as the documented starting point.
EDITABLE_FIELDS = {
    "shop": {"order_received_date", "order_via_key", "submission_type_key", "dc_inv_no",
             "shipping_location", "detailed_remarks"},
    "godown": {"ready_by_person_key", "colour_making_person_key", "delivery_status_key"},
    "godown_dispatch": _DISPATCH,
    "shop_dispatch": _DISPATCH | _RECEIVING,
    "receiving": _RECEIVING,
}

CREATE_ORDER_ROLES = {"shop", "admin"}

# Dropdown lookups: which roles may read and add values (the dropdown they fill in).
LOOKUP_ACCESS = {
    "channels": {"shop"},
    "submission-types": {"shop"},
    "delivery-statuses": {"godown", "shop_dispatch", "godown_dispatch"},
    # godown included so its Mat. Payment Status field (Setup -> Permissions can grant
    # payment_status_key to godown) has a list to read from, if admin turns it on.
    "payment-statuses": {"shop_dispatch", "godown_dispatch", "godown", "receiving"},
}

# People lists (include phone numbers): which roles may read/add each kind.
PEOPLE_ACCESS = {
    "ready_by": {"godown"},
    "colour_making": {"godown"},
    "delivery": {"shop_dispatch", "godown_dispatch"},
}


def can_access(user, allowed_roles) -> bool:
    """True for anyone in allowed_roles, or for admin, always - admin is a
    super-user: same powers as every operating role, on every screen."""
    return user["role"] == "admin" or user["role"] in allowed_roles

# Order visibility. Expressions use aliases: o = fact_orders, ds = dim_delivery_status,
# st = dim_submission_type. %s is the user's user_key (repeated where needed).
_OPEN = "(o.material_delivery_datetime IS NULL OR o.date_of_receiving_key IS NULL OR o.payment_status_key IS NULL)"
_STATUS = "lower(btrim(ds.status_name))"
VISIBILITY = {
    # shop: only orders they created
    "shop": ("o.created_by_user_key = %s", 1),
    # godown: orders still waiting for godown work, plus ones they last touched
    "godown": ("((o.delivery_status_key IS NULL AND COALESCE(lower(btrim(st.type_name)), '') <> 'cancelled')"
               " OR o.last_updated_by_user_key = %s)", 1),
    # shop dispatch: 'Shop' orders that are not fully closed, plus ones they last touched
    "shop_dispatch": (f"(({_STATUS} = 'shop' AND {_OPEN}) OR o.last_updated_by_user_key = %s)", 1),
    # godown dispatch: non-'Shop', non-cancelled orders not fully closed, plus ones they last touched
    "godown_dispatch": (f"(({_STATUS} IS NOT NULL AND {_STATUS} NOT IN ('shop', 'cancelled') AND {_OPEN})"
                        " OR o.last_updated_by_user_key = %s)", 1),
    # receiving: delivered non-'Shop' orders still missing receiving details, plus ones they last touched
    "receiving": (f"((o.material_delivery_datetime IS NOT NULL AND {_STATUS} IS DISTINCT FROM 'shop'"
                  " AND (o.date_of_receiving_key IS NULL OR o.payment_status_key IS NULL))"
                  " OR o.last_updated_by_user_key = %s)", 1),
    # admin: sees every order, and (per config.editable_fields_for_role) may edit any
    # field - a full super-user, same powers as every operating role combined.
    "admin": ("TRUE", 0),
    # Every other role (cashier, accounts, cartage, and anything added later) is NOT
    # listed here on purpose: those roles have no built-in operational workflow, so
    # whether they can view orders at all is a pure admin-controlled setting - see
    # visibility() below, which checks config.can_view_all_orders() for any role not
    # in this dict. Defaults to no access until admin explicitly grants it from
    # Setup -> Permissions.
}


# TEMPORARY test guard (remove before go-live): accounts named test_* only ever see, and may only
# create, orders whose DC/Inv no starts with TEST-. Keeps testing away from real orders.
TEST_USER_PREFIX = "test_"
TEST_DC_PREFIX = "TEST-"


def is_test_user(user):
    return user["username"].startswith(TEST_USER_PREFIX)


def visibility(user, archived=None):
    """(sql, params) restricting orders to those this user may see. Deny by default.

    archived: only meaningful for admin - None/False means "active only" (everyone
    else's behavior, unconditionally); True means "archived only", for the admin
    dashboard's Current/Archive toggle. No other role can ever see archived orders,
    regardless of this argument - operating screens never pass it.
    """
    entry = VISIBILITY.get(user["role"])
    if entry is None:
        # Not a role with built-in workflow visibility - fall through to the
        # admin-controlled setting. No entry / not granted = deny, same as before.
        entry = ("TRUE", 0) if config.can_view_all_orders(user["role"]) else None
    if entry is None:
        return "FALSE", []
    sql, n = entry
    if is_test_user(user):
        sql = f"({sql}) AND upper(o.dc_inv_no) LIKE '{TEST_DC_PREFIX}%%'"
    if user["role"] == "admin" and archived:
        archived_clause = "o.archived_at IS NOT NULL"
    else:
        archived_clause = "o.archived_at IS NULL"
    return f"{archived_clause} AND ({sql})", [user["user_key"]] * n
