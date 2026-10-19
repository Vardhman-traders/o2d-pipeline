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
    "att_checkin": {
        "label": "Attendance - check-in message",
        "when": "When an employee checks in (the attendance page), unless the employee is set to 'no WhatsApp' in Setup > Dropdown values > Attendance employees.",
        "placeholders": {"icon": "Status colour dot", "name": "Employee", "time": "Check-in time (12-hour)", "status": "Present / Late / Half Day",
                         "site_icon": "Place icon", "site": "Godown / Shop", "dist": "Distance, like ' (12m)'", "date": "Date (dd Mon yyyy)"},
        "template": ("{icon} *CHECK-IN* — Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\n👤 *Naam:* {name}\n🕐 *Time:* {time}\n📊 *Status:* {status}\n"
                     "{site_icon} *Location:* {site}{dist}\n📅 *Date:* {date}\n━━━━━━━━━━━━━━━━━━━━")},
    "att_checkout": {
        "label": "Attendance - check-out message",
        "when": "When an employee checks out, or the owner checks someone out by hand.",
        "placeholders": {"name": "Employee", "check_in": "Check-in time", "check_out": "Check-out time", "hours": "Hours worked",
                         "final_status": "Present / Late / Half Day / Absent", "by_line": "'Checkout by: Owner (Manual)' line, empty otherwise",
                         "date": "Date (dd Mon yyyy)"},
        "template": ("🔴 *CHECK-OUT* — Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\n👤 *Naam:* {name}\n🕐 *Check-in:* {check_in}\n"
                     "🏁 *Check-out:* {check_out}\n⏱️ *Hours:* {hours}\n📊 *Final Status:* {final_status}\n{by_line}📅 *Date:* {date}\n"
                     "━━━━━━━━━━━━━━━━━━━━")},
    "att_auto_summary": {
        "label": "Attendance - auto check-out summary",
        "when": "Once a day after the auto check-out runs: one message listing everyone who forgot to check out.",
        "placeholders": {"auto_time": "The auto check-out time (12-hour)", "list": "Numbered list: name, check-in time, status", "date": "Date (dd Mon yyyy)"},
        "template": ("🌙 *AUTO CHECK-OUT* — Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\nInhone checkout nahi kiya tha — system ne {auto_time} pe kar diya:\n\n"
                     "{list}\n\n📅 {date}\n━━━━━━━━━━━━━━━━━━━━")},
    "delivery_day_summary": {
        "label": "Daily delivery summary (7:30 PM)",
        "when": "Every day at 7:30 PM IST, after the per-person delivery reports are built: one message listing each delivery person's orders.",
        "placeholders": {"date": "The day (dd-mm-yyyy)", "summary": "One line per delivery person: name, number of orders, cartage"},
        "template": "Delivery summary - {date}\n\n{summary}"},
    # ---- sent to people (their own WhatsApp number and the Delegation screen's bell), not to a group
    "deleg_assigned": {
        "label": "Delegation - task given", "personal": True, "recipients": ["assignee", "ea", "admin"],
        "when": "As soon as a task is given (or handed to someone else): to the person it is for, and - if switched on below - the EA and the admin.",
        "placeholders": {"name": "Who the task is for", "task": "The task", "due": "Deadline (dd-mm-yyyy)", "assigned_by": "Who gave it",
                         "task_id": "Task number"},
        "app_title": "New task: {task}",
        "template": ("*New task* - Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\n👤 {name}\n📝 {task}\n📅 Deadline: {due}\n🙋 Given by: {assigned_by}\n"
                     "🔖 {task_id}\n━━━━━━━━━━━━━━━━━━━━")},
    "deleg_done": {
        "label": "Delegation - task completed", "personal": True, "recipients": ["ea", "admin"],
        "when": "When a person marks a task complete: to the EA and the admin (each can be switched off below).",
        "placeholders": {"name": "Who did the task", "task": "The task", "due": "Deadline (dd-mm-yyyy)", "timing": "'on time' or 'late'",
                         "score": "Points earned", "notes": "The note they wrote (or a dash)", "task_id": "Task number"},
        "app_title": "Task completed by {name}: {task}",
        "template": ("*Task completed* - Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\n👤 {name}\n📝 {task}\n📅 Deadline: {due} ({timing})\n"
                     "⭐ Points: {score}\n🗒️ {notes}\n🔖 {task_id}\n━━━━━━━━━━━━━━━━━━━━")},
    "chk_assigned": {
        "label": "Checklist - checklist given", "personal": True, "recipients": ["assignee", "ea", "admin"],
        "when": "When a checklist is created for someone (or handed to someone else): to that person, and - if switched on below - the EA and the admin.",
        "placeholders": {"name": "Who the checklist is for", "title": "The checklist", "when": "How often, like 'Every Saturday'", "assigned_by": "Who set it"},
        "app_title": "New checklist: {title}",
        "template": ("*New checklist* - Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\n👤 {name}\n✅ {title}\n🔁 {when}\n🙋 Set by: {assigned_by}\n"
                     "━━━━━━━━━━━━━━━━━━━━")},
    "chk_done": {
        "label": "Checklist - item marked", "personal": True, "recipients": ["ea", "admin"],
        "when": "Each time a checklist item is marked Done or Not Done: to the EA and the admin. Off to start with - a daily checklist sends one message per item.",
        "placeholders": {"name": "Who the item belongs to", "title": "The checklist", "due": "Due date (dd-mm-yyyy)", "status": "Done / Not Done",
                         "remark": "The reason (or a dash)", "marked_by": "Who marked it"},
        "app_title": "Checklist {status}: {title} ({name})",
        "template": ("*Checklist item {status}* - Vardhman Traders\n━━━━━━━━━━━━━━━━━━━━\n👤 {name}\n✅ {title}\n📅 Due: {due}\n🗒️ {remark}\n"
                     "━━━━━━━━━━━━━━━━━━━━")},
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
            cur.execute("SELECT group_id, enabled, template, to_assignee, to_ea, to_admin, ea_employee_key, admin_numbers "
                        "FROM whatsapp_alert WHERE alert_key = %s", (key,))
            row = cur.fetchone()
    except Exception:
        log.warning("Could not read the WhatsApp settings for %s - using defaults", key)
    row = row or {}
    return {"group_id": (row.get("group_id") or "").strip() or _legacy_group(key),
            "enabled": True if not row else bool(row["enabled"]),
            "template": row.get("template") or spec["template"],
            "to_assignee": bool(row.get("to_assignee", True)), "to_ea": bool(row.get("to_ea", True)), "to_admin": bool(row.get("to_admin", True)),
            "ea_employee_key": row.get("ea_employee_key"), "admin_numbers": (row.get("admin_numbers") or "").strip()}


def render(template: str, values: dict) -> str:
    """Fill {placeholders}. Unknown names are left as written; no other formatting is interpreted."""
    return PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def _http_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed https URL from config
        return resp.status, resp.read().decode("utf-8", "replace")


def reply_ok(code, body: str) -> bool:
    """Did the provider really accept the message? An HTTP 2xx is not enough: some providers answer 200 with an error in the body."""
    if not 200 <= (code or 0) < 300:
        return False
    try:
        j = json.loads(body)
    except (ValueError, TypeError):
        return True
    if isinstance(j, dict):
        if j.get("success") is False or j.get("error") or str(j.get("status", "")).lower() in ("error", "failed", "false", "fail"):
            return False
    return True


def _snippet(body: str) -> str:
    return " ".join(str(body or "").split())[:300]


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
    payload = {"recipientIds": [settings["group_id"]], "message": [text]}
    if os.environ.get("WHATSAPP_CHANNEL_ID", "").strip().isdigit():   # only when the account has more than one WhatsApp number
        payload["channelId"] = int(os.environ["WHATSAPP_CHANNEL_ID"].strip())
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **headers}
    code, reply = post(API_URL, body, headers)
    return {"code": code, "body": reply}


def normalize_phone(raw) -> str | None:
    """A WhatsApp number as the provider wants it: country code + number, digits only. A bare 10-digit Indian number gets 91 in front.
    None when it cannot be a phone number (so a typo is caught when it is saved, not when a message silently fails)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    return digits if 11 <= len(digits) <= 15 and not digits.startswith("0") else None


def split_numbers(raw) -> list[str] | None:
    """'98765 43210, +91 99999 11111' -> ['919876543210', '919999911111']; None when any entry is not a phone number."""
    out = []
    for part in re.split(r"[,;\n]", str(raw or "")):
        if part.strip():
            n = normalize_phone(part)
            if n is None:
                return None
            if n not in out:
                out.append(n)
    return out


def send_personal(key: str, phone: str, text: str, post=_http_post, ignore_switch: bool = False) -> dict:
    """Send `text` to one person's WhatsApp number with the same provider call, login and checks as a group message
    ({"skipped": True, "reason": ...} when it cannot go out). The caller reads the provider's answer with reply_ok()."""
    if not ignore_switch and not get_alert(key)["enabled"]:
        return {"skipped": True, "reason": "This alert is switched off in Setup."}
    headers = auth_headers()
    if not headers:
        return {"skipped": True, "reason": "WhatsApp credentials not set."}
    number = normalize_phone(phone)
    if not number:
        return {"skipped": True, "reason": "No valid WhatsApp number."}
    payload = {"recipientIds": [number], "message": [text]}
    if os.environ.get("WHATSAPP_CHANNEL_ID", "").strip().isdigit():
        payload["channelId"] = int(os.environ["WHATSAPP_CHANNEL_ID"].strip())
    code, reply = post(API_URL, json.dumps(payload).encode(), {"Content-Type": "application/json", **headers})
    return {"code": code, "body": reply}


def send_alert(key: str, values: dict, prefix: str = "", post=_http_post) -> dict:
    return send_text(key, prefix + render(get_alert(key)["template"], values), post=post)


# ------------------------------------------------------------------ Setup > WhatsApp (admin)
@router.get("")
def list_alerts(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        cur.execute("SELECT alert_key, group_id, enabled, template, updated_at, to_assignee, to_ea, to_admin, ea_employee_key, admin_numbers FROM whatsapp_alert")
        saved = {r["alert_key"]: r for r in cur.fetchall()}
        cur.execute("SELECT employee_key, employee_name, phone_number FROM dim_att_employee WHERE is_active ORDER BY lower(employee_name)")
        employees = [{"key": r["employee_key"], "name": r["employee_name"], "has_phone": bool(r["phone_number"])} for r in cur.fetchall()]
    out = []
    for key, spec in ALERTS.items():
        row = saved.get(key) or {}
        out.append({"key": key, "label": spec["label"], "when": spec["when"], "placeholders": spec["placeholders"],
                    "default_template": spec["template"], "template": row.get("template") or spec["template"],
                    "enabled": True if not row else bool(row["enabled"]),
                    "group_id": (row.get("group_id") or "").strip(), "group_from_environment": bool(not (row.get("group_id") or "").strip() and _legacy_group(key)),
                    "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
                    "personal": bool(spec.get("personal")), "recipients": spec.get("recipients", []),
                    "to_assignee": bool(row.get("to_assignee", True)), "to_ea": bool(row.get("to_ea", True)), "to_admin": bool(row.get("to_admin", True)),
                    "ea_employee_key": row.get("ea_employee_key"), "admin_numbers": row.get("admin_numbers") or ""})
    return {"alerts": out, "credentials_set": credentials_set(), "employees": employees}


class AlertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str = Field(default="", max_length=100)
    enabled: bool = True
    template: str = Field(min_length=1, max_length=2000)
    # who a personal alert reaches (ignored by group alerts)
    to_assignee: bool = True
    to_ea: bool = True
    to_admin: bool = True
    ea_employee_key: int | None = None
    admin_numbers: str = Field(default="", max_length=200)


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
    admin_numbers = split_numbers(body.admin_numbers)
    if admin_numbers is None:
        raise HTTPException(422, "Admin WhatsApp numbers: each must be a phone number with the country code, like 919876543210 "
                                 "(10-digit Indian numbers get 91 added). Separate several with commas.")
    with db.cursor() as cur:
        if body.ea_employee_key is not None:
            cur.execute("SELECT 1 FROM dim_att_employee WHERE employee_key = %s AND is_active", (body.ea_employee_key,))
            if not cur.fetchone():
                raise HTTPException(422, "Choose the EA from the employee list.")
        cur.execute("INSERT INTO whatsapp_alert (alert_key, label, group_id, enabled, template, updated_by_user_key, updated_at, "
                    "to_assignee, to_ea, to_admin, ea_employee_key, admin_numbers) "
                    "VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s) ON CONFLICT (alert_key) DO UPDATE SET group_id = EXCLUDED.group_id, "
                    "enabled = EXCLUDED.enabled, template = EXCLUDED.template, updated_by_user_key = EXCLUDED.updated_by_user_key, "
                    "updated_at = now(), to_assignee = EXCLUDED.to_assignee, to_ea = EXCLUDED.to_ea, to_admin = EXCLUDED.to_admin, "
                    "ea_employee_key = EXCLUDED.ea_employee_key, admin_numbers = EXCLUDED.admin_numbers",
                    (key, spec["label"], group or None, body.enabled, body.template.strip(), admin["user_key"], body.to_assignee, body.to_ea,
                     body.to_admin, body.ea_employee_key, ", ".join(admin_numbers) or None))
        audit(cur, admin, "whatsapp.save", key, {"group_set": bool(group), "enabled": body.enabled,
                                                 "to": [w for w, on in (("assignee", body.to_assignee), ("ea", body.to_ea), ("admin", body.to_admin)) if on],
                                                 "ea": body.ea_employee_key})
    return {"ok": True}


@router.post("/{key}/test")
def send_test(key: str, admin=Depends(ADMIN)):
    """Send a clearly marked test message to the saved group, so a new group ID can be checked without a real order."""
    spec = ALERTS.get(key)
    if not spec:
        raise HTTPException(404, "Unknown alert")
    if spec.get("personal"):
        return _test_personal(key, spec, admin)
    try:
        out = send_text(key, f"TEST message from the Vardhman portal ({spec['label']}). Please ignore.", ignore_switch=True)
    except Exception as exc:
        log.exception("WhatsApp test message failed")
        return {"ok": True, "success": False, "message": f"The provider did not accept the message ({exc.__class__.__name__})."}
    if out.get("skipped"):
        return {"ok": True, "success": False, "message": out["reason"]}
    shown = _snippet(out.get("body"))
    ok = reply_ok(out.get("code"), out.get("body", ""))
    with db.cursor() as cur:
        audit(cur, admin, "whatsapp.test", key, {"ok": ok, "code": out.get("code")})
    if not ok:   # the provider answered, but did not take the message: say so and show what it said
        return {"ok": True, "success": False, "message": f"The provider did not accept the message (HTTP {out.get('code')}): {shown or 'no reply'}"}
    return {"ok": True, "success": True,
            "message": "The provider accepted the test message. Check the group" + (f" (provider reply: {shown})." if shown else ".") +
                       " If nothing arrives there, the group ID is not the one the provider knows - copy it again from the provider's group list."}


def _test_personal(key: str, spec: dict, admin) -> dict:
    """A marked test to the saved EA and admin numbers (the person a task is for has no fixed number to test)."""
    a = get_alert(key)
    numbers = {}
    with db.cursor() as cur:
        if a["ea_employee_key"]:
            cur.execute("SELECT employee_name, phone_number FROM dim_att_employee WHERE employee_key = %s", (a["ea_employee_key"],))
            e = cur.fetchone()
            if e and normalize_phone(e["phone_number"]):
                numbers[normalize_phone(e["phone_number"])] = "the EA " + e["employee_name"]
    for n in split_numbers(a["admin_numbers"]) or []:
        numbers.setdefault(n, "admin " + n)
    if not numbers:
        return {"ok": True, "success": False, "message": "No number to test: choose the EA (and add the EA's WhatsApp number under Dropdown values > "
                                                         "Attendance employees) or type an admin number, then Save."}
    results = []
    for n, who in numbers.items():
        try:
            out = send_personal(key, n, f"TEST message from the Vardhman portal ({spec['label']}). Please ignore.", post=_http_post, ignore_switch=True)   # looked up now, so tests can swap the sender
        except Exception as exc:
            log.exception("WhatsApp test message failed")
            results.append((who, False, exc.__class__.__name__))
            continue
        if out.get("skipped"):
            return {"ok": True, "success": False, "message": out["reason"]}
        results.append((who, reply_ok(out.get("code"), out.get("body", "")), _snippet(out.get("body"))))
    with db.cursor() as cur:
        audit(cur, admin, "whatsapp.test", key, {"to": [w for w, _, _ in results], "ok": [ok for _, ok, _ in results]})
    bad = [f"{w} ({d or 'no reply'})" for w, ok, d in results if not ok]
    if bad:
        return {"ok": True, "success": False, "message": "The provider did not accept the message for: " + "; ".join(bad)}
    return {"ok": True, "success": True, "message": "The provider accepted the test message for " + ", ".join(w for w, _, _ in results) + ". Check their WhatsApp."}
