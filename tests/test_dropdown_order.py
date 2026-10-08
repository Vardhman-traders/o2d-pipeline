# ruff: noqa: F811, E501
"""Dropdown lists: a drag-and-drop order that every screen follows, new values going last, Delivery / Payment status limited
to chosen screens, and the built-in 'Shop' delivery status shown as 'Dispatch for Shop'."""
import uuid

from tests.test_admin_tools import client, login, seed  # noqa: F401  (fixtures + helpers)


def _names(rows):
    return [r["name"] for r in rows]


def test_dragged_order_is_saved_and_every_dropdown_follows_it(client, db_conn, seed):
    admin, receiving = login(client, db_conn), login(client, db_conn, "receiving")
    rows = client.get("/admin/lookups/payment-statuses", headers=admin).json()
    keys = [r["key"] for r in rows]
    assert len(keys) >= 2
    r = client.put("/admin/reconcile/payment_status/order", json={"keys": list(reversed(keys))}, headers=admin)
    assert r.status_code == 200, r.text
    after = client.get("/admin/lookups/payment-statuses", headers=admin).json()
    assert [x["key"] for x in after] == list(reversed(keys))
    shown = client.get("/o2d/receiving", headers=receiving).json()["dropdowns"]["paymentStatus"]
    assert shown == _names(after)
    # a list that changed under the admin is refused, not half-saved
    assert client.put("/admin/reconcile/payment_status/order", json={"keys": keys + [10**9]}, headers=admin).status_code == 409


def test_new_values_go_to_the_end_and_people_lists_need_their_role(client, db_conn, seed):
    admin = login(client, db_conn)
    name = f"Aaa first {uuid.uuid4().hex[:4]}"
    assert client.post("/admin/lookups/channels", json={"name": name}, headers=admin).status_code == 201
    assert _names(client.get("/admin/lookups/channels", headers=admin).json())[-1] == name
    keys = [p["key"] for p in client.get("/admin/people?role=delivery", headers=admin).json()]
    assert client.put("/admin/reconcile/person/order", json={"keys": keys}, headers=admin).status_code == 422
    assert client.put("/admin/reconcile/person/order", json={"keys": keys, "role": "delivery"}, headers=admin).status_code == 200


def test_status_values_can_be_limited_to_screens(client, db_conn, seed):
    admin, receiving = login(client, db_conn), login(client, db_conn, "receiving")
    pending = next(r for r in client.get("/admin/lookups/payment-statuses", headers=admin).json() if r["name"] == "Pending")
    ok = client.put(f"/admin/lookups/payment-statuses/{pending['key']}", json={"name": "Pending", "screens": ["shop_dispatch"]}, headers=admin)
    assert ok.status_code == 200, ok.text
    assert "Pending" not in client.get("/o2d/receiving", headers=receiving).json()["dropdowns"]["paymentStatus"]
    assert "Paid" in client.get("/o2d/receiving", headers=receiving).json()["dropdowns"]["paymentStatus"]
    assert "Pending" in client.get("/o2d/dispatch?view_as=shop_dispatch", headers=admin).json()["dropdowns"]["paymentStatus"]
    # nothing ticked, or a screen that list is never shown on, is refused; ticking every screen goes back to "all"
    assert client.put(f"/admin/lookups/payment-statuses/{pending['key']}", json={"name": "Pending", "screens": []}, headers=admin).status_code == 422
    assert client.put(f"/admin/lookups/payment-statuses/{pending['key']}", json={"name": "Pending", "screens": ["shop"]}, headers=admin).status_code == 422
    opts = client.get("/admin/lookup-screens", headers=admin).json()["payment-statuses"]
    client.put(f"/admin/lookups/payment-statuses/{pending['key']}", json={"name": "Pending", "screens": [o[0] for o in opts]}, headers=admin)
    assert "Pending" in client.get("/o2d/receiving", headers=receiving).json()["dropdowns"]["paymentStatus"]


def test_shop_status_is_worded_dispatch_for_shop_in_dropdowns_and_accepted_when_saved(client, db_conn, seed):
    admin, godown = login(client, db_conn), login(client, db_conn, "godown")
    shown = client.get("/o2d/godown", headers=godown).json()["dropdowns"]["deliveryStatus"]
    assert "Dispatch for Shop" in shown and "Shop" not in shown
    rows = client.get("/admin/reconcile/delivery_status", headers=admin).json()["values"]
    shop = next(v for v in rows if v["name"] == "Shop")
    assert shop["label"] == "Dispatch for Shop" and shop["protected"] is True
