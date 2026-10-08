# ruff: noqa: F811, E501
"""WhatsApp alert settings (Setup > WhatsApp): template filling, group / on-off / template saved by admins only."""
from app import whatsapp
from tests.test_admin_tools import client, login, seed  # noqa: F401  (fixtures + helpers)


def test_templates_fill_known_placeholders_and_leave_everything_else_alone():
    out = whatsapp.render("DC {dc_no} to {address} {unknown} {{x}}", {"dc_no": "12", "address": "Rohini"})
    assert out == "DC 12 to Rohini {unknown} {{x}}"
    assert "{order_date}" in whatsapp.ALERTS["dispatch_godown"]["template"]


def test_admin_saves_group_switch_and_template_and_others_cannot(client, db_conn, seed, monkeypatch):
    admin, godown = login(client, db_conn), login(client, db_conn, "godown_dispatch")
    assert client.get("/admin/whatsapp", headers=godown).status_code == 403
    alerts = client.get("/admin/whatsapp", headers=admin).json()["alerts"]
    assert alerts[0]["key"] == "dispatch_godown" and "{dc_no}" in alerts[0]["template"]
    body = {"group_id": "555@g.us", "enabled": True, "template": "Dispatch {dc_no} by {delivered_by}"}
    assert client.put("/admin/whatsapp/dispatch_godown", json=body, headers=godown).status_code == 403
    assert client.put("/admin/whatsapp/dispatch_godown", json={**body, "template": "Hi {nope}"}, headers=admin).status_code == 422
    assert client.put("/admin/whatsapp/dispatch_godown", json={**body, "group_id": "5 5"}, headers=admin).status_code == 422
    assert client.put("/admin/whatsapp/nothing", json=body, headers=admin).status_code == 404
    assert client.put("/admin/whatsapp/dispatch_godown", json=body, headers=admin).status_code == 200

    monkeypatch.setenv("WHATSAPP_API_USERNAME", "u")
    monkeypatch.setenv("WHATSAPP_API_PASSWORD", "p")
    seen = {}
    out = whatsapp.send_alert("dispatch_godown", {"dc_no": "77", "delivered_by": "Amit"},
                              post=lambda url, data, headers: seen.update(body=data) or (200, "ok"))
    assert out["code"] == 200 and b"555@g.us" in seen["body"] and b"Dispatch 77 by Amit" in seen["body"]

    client.put("/admin/whatsapp/dispatch_godown", json={**body, "enabled": False}, headers=admin)
    off = whatsapp.send_alert("dispatch_godown", {}, post=lambda *a: (200, "ok"))
    assert off["skipped"] is True and "switched off" in off["reason"]
