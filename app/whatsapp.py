# ruff: noqa: E501
"""WhatsApp alerts. Each kind of alert (an "alert type") is one row in `whatsapp_alert`: which group it goes to, an on/off
switch and the message template. Admins edit them under Setup > WhatsApp; the provider login (WHATSAPP_API_KEY, or the older
WHATSAPP_API_USERNAME / _PASSWORD, and WHATSAPP_API_URL if it differs) is the only thing kept in the environment.

Adding an alert type = one row in a migration + one entry in ALERTS below + a call to `send_alert(key, values)` where it happens.
"""
import base64
import json
import logging
import os
import re
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import config, db
from .admin import ADMIN, audit

log = logging.getLogger("whatsapp")
router = APIRouter(prefix="/admin/whatsapp", tags=["whatsapp"])

API_URL = os.environ.get("WHATSAPP_API_URL", "https://app.messageautosender.com/api/v1/message/create")
PLACEHOLDER = re.compile(r"\{(\w+)\}")

DISPATCH_TEMPLATE = ("New Dispatch - Godown\n\nOrder Date: {order_date}\nDC No: {dc_no}\nReady By: {ready_by}\n"
                     "Delivery Status: {delivery_status}\nAddress: {address}\nRemarks: {remarks}\nDelivered By: {delivered_by}")

# key -> what it is, when it is sent, the {placeholders} its template may use (value shown in the Setup help), default template
ALERTS: dict[str, dict] = {
    "dispatch_godown": {
        "label": "Godown dispatch alert",
        "when": "When Godown Dispatch first fills in the delivery date/time and Delivered by on an order (and on 'Resend').",
        "placeholders": {"order_date": "Order date (dd/mm/yyyy)", "dc_no": "DC / Invoice no", "ready_by": "Ready by",
                         "delivery_status": "Delivery status", "address": "Address", "remarks": "Remarks",
                         "delivered_by": "Delivered by"},
        "template": DISPATCH_TEMPLATE},
}


def _legacy_group(key: str) -> str:
    """Before this setting existed the dispatch group lived in app_config / the WHATSAPP_GROUP_ID variable: still honoured."""
    if key != "dispatch_godown":
        return ""
    try:
        configured = config.get("whatsapp_group_id")
    except Exception:  # the settings table is unreachable: never block the order save
        configured = None
    return (configured or os.environ.get("WHATSAPP_GROUP_ID") or "").strip()


def get_alert(key: str) -> dict:
    """The saved settings of one alert type, or the built-in defaults when the database can't be read."""
    spec = ALERTS[key]
    row = None
    try:
        with db.cursor() as cur:
            cur.execute("SELECT group_id, enabled, template FROM whatsapp_alert WHERE alert_key = %s", (key,))
            row = cur.fetchone()
    except Exception:
        log.warning("Could not read the WhatsApp settings for %s - using defaults", key)
    return {"group_id": ((row or {}).get("group_id") or "").strip() or _legacy_group(key),
            "enabled": True if row is None else bool(row["enabled"]),
            "template": (row or {}).get("template") or spec["template"]}


def render(template: str, values: dict) -> str:
    """Fill {placeholders}. Unknown names are left as written; no other formatting is interpreted."""
    return PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def _http_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed https URL from config
        return resp.status, resp.read().decode("utf-8", "replace")


def credentials_set() -> bool:
    return bool(auth_headers())


def auth_headers() -> dict:
    """The provider login: an API key (WHATSAPP_API_KEY, sent as the x-api-key header - what the portal's Keys page issues), or
    the older username + password (WHATSAPP_API_USERNAME / _PASSWORD, sent as Basic auth). The key wins when both are set."""
    key = (os.environ.get("WHATSAPP_API_KEY") or "").strip()
    if key:
        return {"x-api-key": key}
    user, pwd = os.environ.get("WHATSAPP_API_USERNAME"), os.environ.get("WHATSAPP_API_PASSWORD")
    if user and pwd:
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}
    return {}


def send_text(key: str, text: str, post=_http_post, ignore_switch: bool = False) -> dict:
    """Send `text` to the group set for this alert type. {"skipped": True, "reason": ...} when it cannot go out."""
    settings = get_alert(key)
    if not settings["enabled"] and not ignore_switch:
        return {"skipped": True, "reason": "This alert is switched off in Setup."}
    headers = auth_headers()
    if not headers:
        return {"skipped": True, "reason": "WhatsApp credentials not set."}
    if not settings["group_id"]:
        return {"skipped": True, "reason": "WhatsApp group not set."}
    body = json.dumps({"recipientIds": [settings["group_id"]], "message": [text]}).encode()
    headers = {"Content-Type": "application/json", **headers}
    code, reply = post(API_URL, body, headers)
    return {"code": code, "body": reply}


def send_alert(key: str, values: dict, prefix: str = "", post=_http_post) -> dict:
    return send_text(key, prefix + render(get_alert(key)["template"], values), post=post)


# ------------------------------------------------------------------ Setup > WhatsApp (admin)
@router.get("")
def list_alerts(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        cur.execute("SELECT alert_key, group_id, enabled, template, updated_at FROM whatsapp_alert")
        saved = {r["alert_key"]: r for r in cur.fetchall()}
    out = []
    for key, spec in ALERTS.items():
        row = saved.get(key) or {}
        out.append({"key": key, "label": spec["label"], "when": spec["when"], "placeholders": spec["placeholders"],
                    "default_template": spec["template"], "template": row.get("template") or spec["template"],
                    "enabled": True if not row else bool(row["enabled"]),
                    "group_id": (row.get("group_id") or "").strip(), "group_from_environment": bool(not (row.get("group_id") or "").strip() and _legacy_group(key)),
                    "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None})
    return {"alerts": out, "credentials_set": credentials_set()}


class AlertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str = Field(default="", max_length=100)
    enabled: bool = True
    template: str = Field(min_length=1, max_length=2000)


@router.put("/{key}")
def save_alert(key: str, body: AlertIn, admin=Depends(ADMIN)):
    spec = ALERTS.get(key)
    if not spec:
        raise HTTPException(404, "Unknown alert")
    group = body.group_id.strip()
    if re.search(r"\s", group):
        raise HTTPException(422, "The group ID must not contain spaces or line breaks.")
    unknown = sorted(set(PLACEHOLDER.findall(body.template)) - set(spec["placeholders"]))
    if unknown:
        raise HTTPException(422, "Unknown placeholder(s) in the message: " + ", ".join("{" + u + "}" for u in unknown) +
                            ". You can use: " + ", ".join("{" + p + "}" for p in spec["placeholders"]))
    with db.cursor() as cur:
        cur.execute("INSERT INTO whatsapp_alert (alert_key, label, group_id, enabled, template, updated_by_user_key, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, now()) ON CONFLICT (alert_key) DO UPDATE SET group_id = EXCLUDED.group_id, "
                    "enabled = EXCLUDED.enabled, template = EXCLUDED.template, updated_by_user_key = EXCLUDED.updated_by_user_key, "
                    "updated_at = now()", (key, spec["label"], group or None, body.enabled, body.template.strip(), admin["user_key"]))
        audit(cur, admin, "whatsapp.save", key, {"group_set": bool(group), "enabled": body.enabled})
    return {"ok": True}


@router.post("/{key}/test")
def send_test(key: str, admin=Depends(ADMIN)):
    """Send a clearly marked test message to the saved group, so a new group ID can be checked without a real order."""
    spec = ALERTS.get(key)
    if not spec:
        raise HTTPException(404, "Unknown alert")
    try:
        out = send_text(key, f"TEST message from the Vardhman portal ({spec['label']}). Please ignore.", ignore_switch=True)
    except Exception as exc:
        log.exception("WhatsApp test message failed")
        return {"ok": True, "success": False, "message": f"The provider did not accept the message ({exc.__class__.__name__})."}
    if out.get("skipped"):
        return {"ok": True, "success": False, "message": out["reason"]}
    with db.cursor() as cur:
        audit(cur, admin, "whatsapp.test", key)
    return {"ok": True, "success": True, "message": "Test message sent. Check the group."}
