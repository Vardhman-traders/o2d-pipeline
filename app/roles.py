"""Who may see and change what. Everything role-related lives here so it is easy to audit."""

ORDER_ROLES = {"shop", "godown", "shop_dispatch", "godown_dispatch", "receiving"}
# Known roles with no access to orders through this API.
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

CREATE_ORDER_ROLES = {"shop"}

# Dropdown lookups: which roles may read and add values (the dropdown they fill in).
LOOKUP_ACCESS = {
    "channels": {"shop"},
    "submission-types": {"shop"},
    "delivery-statuses": {"godown", "shop_dispatch", "godown_dispatch"},
    "payment-statuses": {"shop_dispatch", "receiving"},
}

# People lists (include phone numbers): which roles may read/add each kind.
PEOPLE_ACCESS = {
    "ready_by": {"godown"},
    "colour_making": {"godown"},
    "delivery": {"shop_dispatch", "godown_dispatch"},
}

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
    # admin: read-only visibility into every order (see main.py's ANY_ORDER_ROLE_OR_ADMIN -
    # admin is deliberately left out of EDITABLE_FIELDS, so writes stay blocked either way).
    "admin": ("TRUE", 0),
}


# TEMPORARY test guard (remove before go-live): accounts named test_* only ever see, and may only
# create, orders whose DC/Inv no starts with TEST-. Keeps testing away from real orders.
TEST_USER_PREFIX = "test_"
TEST_DC_PREFIX = "TEST-"


def is_test_user(user):
    return user["username"].startswith(TEST_USER_PREFIX)


def visibility(user):
    """(sql, params) restricting orders to those this user may see. Deny by default."""
    entry = VISIBILITY.get(user["role"])
    if entry is None:
        return "FALSE", []
    sql, n = entry
    if is_test_user(user):
        sql = f"({sql}) AND upper(o.dc_inv_no) LIKE '{TEST_DC_PREFIX}%%'"
    # Archived orders never appear on any operating screen, regardless of the
    # "or I last touched it" clauses above - archiving only ever applies to
    # orders that are already Closed or Cancelled, so there is nothing left
    # for an operating role to act on.
    return f"o.archived_at IS NULL AND ({sql})", [user["user_key"]] * n
