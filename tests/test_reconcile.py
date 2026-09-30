"""Master-data reconciliation: list values, suggest duplicates, rename, merge with preview, protected names."""
import uuid

from tests.test_admin_tools import PW, TODAY, client, login, seed  # noqa: F401  (fixtures shared with that file)

API = "/admin/reconcile"


def _by_name(payload, name):
    return next(v for v in payload["values"] if v["name"] == name)


def _order(db_conn, **keys):
    cols = ", ".join(keys)
    marks = ", ".join(["%s"] * len(keys))
    with db_conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(sl_no), 0) + 1 FROM fact_orders")
        sl = cur.fetchone()[0]
        cur.execute(f"INSERT INTO fact_orders (sl_no, dc_inv_no, order_received_date_key, {cols}) "
                    f"VALUES (%s, %s, %s, {marks}) RETURNING order_key",
                    (sl, "RC-" + uuid.uuid4().hex[:6], int(TODAY.strftime("%Y%m%d")), *keys.values()))
        key = cur.fetchone()[0]
    db_conn.commit()
    return key


def _person(db_conn, name, role, phone=None):
    return _insert(db_conn, "INSERT INTO dim_person (full_name, person_role, phone_number) VALUES (%s,%s,%s) "
                   "RETURNING person_key", name, role, phone)


def _insert(db_conn, sql, *params):
    with db_conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    db_conn.commit()
    return row[0]


def _fact(db_conn, order_key, col):
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT {col} FROM fact_orders WHERE order_key = %s", (order_key,))
        return cur.fetchone()[0]


def test_admin_only(client, db_conn, seed):  # noqa: F811
    shop = login(client, db_conn, "shop")
    assert client.get(API, headers=shop).status_code == 403
    assert client.get(f"{API}/person", headers=shop).status_code == 403
    assert client.post(f"{API}/person/rename", json={"key": 1, "name": "x"}, headers=shop).status_code == 403
    merge = client.post(f"{API}/person/merge", json={"target_key": 1, "source_keys": [2]}, headers=shop)
    assert merge.status_code == 403
    assert client.get(API).status_code == 401


def test_values_are_alphabetical_with_usage_and_duplicates_are_suggested(client, db_conn, seed):  # noqa: F811
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:4]
    a = _person(db_conn, f"Zaheer{u}", 'ready_by')
    b = _person(db_conn, f"Zaheeer{u}", 'ready_by')
    # similar name but a different role: never suggested together with the ready_by ones
    c = _person(db_conn, f"Zaheer{u}x", 'delivery')
    _order(db_conn, ready_by_person_key=a)
    _order(db_conn, ready_by_person_key=a)
    _order(db_conn, ready_by_person_key=b)
    data = client.get(f"{API}/person", headers=admin).json()
    names = [v["name"].lower() for v in data["values"]]
    assert names == sorted(names)
    assert _by_name(data, f"Zaheer{u}")["orders"] == 2 and _by_name(data, f"Zaheeer{u}")["orders"] == 1
    pairs = [sorted(s["keys"]) for s in data["suggestions"]]
    assert sorted([a, b]) in pairs
    assert not any(c in p and (a in p or b in p) for p in pairs)
    overview = {o["kind"]: o for o in client.get(API, headers=admin).json()}
    assert set(overview) == {"channel", "submission_type", "delivery_status", "payment_status", "person"}
    assert overview["person"]["possible_duplicates"] >= 1


def test_rename_reaches_every_order_and_refuses_clashes(client, db_conn, seed):  # noqa: F811
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:4]
    ch = _insert(db_conn, "INSERT INTO dim_order_channel (channel_name) VALUES (%s) RETURNING channel_key", f"Phon{u}")
    other = _insert(db_conn, "INSERT INTO dim_order_channel (channel_name) VALUES (%s) RETURNING channel_key",
                    f"Phone{u}")
    order = _order(db_conn, order_via_key=ch)
    ok = client.post(f"{API}/channel/rename", json={"key": ch, "name": f"  Phone   Call {u} "}, headers=admin)
    assert ok.status_code == 200 and ok.json()["name"] == f"Phone Call {u}"
    with db_conn.cursor() as cur:
        cur.execute("SELECT channel FROM v_orders_archive WHERE order_key = %s", (order,))
        assert cur.fetchone()[0] == f"Phone Call {u}"          # the views (and so every screen) show the new name
        db_conn.rollback()
    clash = client.post(f"{API}/channel/rename", json={"key": ch, "name": f"PHONE{u.upper()}"}, headers=admin)
    assert clash.status_code == 409 and "Merge" in clash.json()["detail"]
    same = client.post(f"{API}/channel/rename", json={"key": other, "name": f"PHONE{u.upper()}"}, headers=admin)
    assert same.status_code == 200                              # only changing the capitals of itself is fine
    assert client.post(f"{API}/channel/rename", json={"key": 10**8, "name": "x"}, headers=admin).status_code == 404
    with db_conn.cursor() as cur:
        cur.execute("SELECT action FROM admin_audit_log WHERE action = 'reconcile.rename.channel'")
        assert cur.fetchone()
        db_conn.rollback()


def test_protected_names_cannot_be_renamed_or_merged_away(client, db_conn, seed):  # noqa: F811
    admin = login(client, db_conn)
    data = client.get(f"{API}/delivery_status", headers=admin).json()
    shop, cancelled = _by_name(data, "Shop"), _by_name(data, "Cancelled")
    assert shop["protected"] and cancelled["protected"]
    assert client.post(f"{API}/delivery_status/rename", json={"key": shop["key"], "name": "Store"},
                       headers=admin).status_code == 409
    variant = _insert(db_conn, "INSERT INTO dim_delivery_status (status_name) VALUES (%s) RETURNING status_key",
                      "Shopp" + uuid.uuid4().hex[:3])
    away = client.post(f"{API}/delivery_status/merge", json={"target_key": variant, "source_keys": [shop["key"]],
                                                              "confirm": True}, headers=admin)
    assert away.status_code == 409
    reserved = client.post(f"{API}/delivery_status/rename", json={"key": variant, "name": "cancelled"}, headers=admin)
    assert reserved.status_code == 409


def test_merge_preview_then_confirm_moves_orders_and_logs(client, db_conn, seed):  # noqa: F811
    admin = login(client, db_conn)
    u = uuid.uuid4().hex[:4]
    good = _person(db_conn, f"Mohan{u}", 'delivery')
    bad = _person(db_conn, f"Mohn{u}", 'delivery', '99999')
    wrong_role = _person(db_conn, f"Mohan{u}", 'ready_by')
    o1 = _order(db_conn, delivered_by_person_key=bad)
    o2 = _order(db_conn, delivered_by_person_key=bad)
    body = {"target_key": good, "source_keys": [bad]}

    preview = client.post(f"{API}/person/merge", json=body, headers=admin).json()
    assert preview["preview"] is True and preview["orders_moved"] == 2 and preview["merging"] == [f"Mohn{u}"]
    assert _fact(db_conn, o1, "delivered_by_person_key") == bad           # a preview changes nothing

    cross = client.post(f"{API}/person/merge", json={"target_key": good, "source_keys": [wrong_role]}, headers=admin)
    assert cross.status_code == 422 and "same role" in cross.json()["detail"]
    self_merge = client.post(f"{API}/person/merge", json={"target_key": good, "source_keys": [good]}, headers=admin)
    assert self_merge.status_code == 422

    done = client.post(f"{API}/person/merge", json={**body, "confirm": True}, headers=admin).json()
    assert done["preview"] is False and done["orders_moved"] == 2
    assert _fact(db_conn, o1, "delivered_by_person_key") == good == _fact(db_conn, o2, "delivered_by_person_key")
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(phone_number) FROM dim_person WHERE person_key IN (%s, %s)", (good, bad))
        count, phone = cur.fetchone()
        cur.execute("SELECT details FROM admin_audit_log WHERE action = 'reconcile.merge.person' "
                    "AND target = %s", (f"Mohan{u}",))
        details = cur.fetchone()[0]
        db_conn.rollback()
    assert count == 1 and phone == "99999"                                # merged row gone, phone kept on the survivor
    assert details["orders_moved"] == 2 and details["merging"] == [f"Mohn{u}"]
    assert client.post(f"{API}/person/merge", json={**body, "confirm": True}, headers=admin).status_code == 404


def test_merging_a_variant_into_cancelled_marks_those_orders_cancelled(client, db_conn, seed):  # noqa: F811
    admin = login(client, db_conn)
    data = client.get(f"{API}/delivery_status", headers=admin).json()
    cancelled = _by_name(data, "Cancelled")["key"]
    variant = _insert(db_conn, "INSERT INTO dim_delivery_status (status_name) VALUES (%s) RETURNING status_key",
                      "Canceled" + uuid.uuid4().hex[:3])
    order = _order(db_conn, delivery_status_key=variant)
    assert _fact(db_conn, order, "is_cancelled") is False
    res = client.post(f"{API}/delivery_status/merge", json={"target_key": cancelled, "source_keys": [variant],
                                                             "confirm": True}, headers=admin)
    assert res.status_code == 200
    assert _fact(db_conn, order, "is_cancelled") is True
    assert _fact(db_conn, order, "delivery_status_key") == cancelled
