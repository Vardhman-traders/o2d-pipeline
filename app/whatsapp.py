# ruff: noqa: E501
"""WhatsApp alerts. Each kind of alert (an "alert type") is one row in `whatsapp_alert`: which group it goes to, an on/off
switch and the message template. Admins edit them under Setup > WhatsApp; the provider login (WHATSAPP_API_KEY, or the older
WHATSAPP_API_USERNAME / _PASSWORD, and WHATSAPP_API_URL if it differs) is the only thing kept in the environment.

Every alert goes to a WhatsApp GROUP. An alert whose own group is empty can fall back to the group of a sibling alert (`group_from`:
Delegation "task completed" uses the "task given" group, the Porter dispatch uses the VT Dispatch group ...), so one group ID per
channel is enough. Each alert carries sample values: Setup shows the message filled with them and can send that sample to the group.

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
RULE = "━━━━━━━━━━━━━━━━━━━━"

# The dispatch message, as in the "WhatsApp Template" document: Cartage and the detail line only appear when they apply (the
# {cartage_line} / {details_line} placeholders are the whole line, or nothing), and Delivered By carries the delivery person's phone.
DISPATCH_TEMPLATE = ("New Dispatch - Godown\n\nOrder Date: {order_date}\nDC No: {dc_no}\nReady By: {ready_by}\n"
                     "Delivery Status: {delivery_status}\nAddress: {address}\nRemarks: {remarks}\n{cartage_line}Delivered By: {delivered_by}\n"
                     "{details_line}Photo: {photo}")
OLD_DISPATCH_TEMPLATE = ("New Dispatch - Godown\n\nOrder Date: {order_date}\nDC No: {dc_no}\nReady By: {ready_by}\n"
                         "Delivery Status: {delivery_status}\nAddress: {address}\nRemarks: {remarks}\nDelivered By: {delivered_by}")
DISPATCH_PLACEHOLDERS = {"order_date": "Order date (dd/mm/yyyy)", "dc_no": "DC / Invoice no", "ready_by": "Ready by", "delivery_status": "Delivery status",
                         "address": "Address", "remarks": "Remarks", "delivered_by": "Delivered by - with the person's phone number",
                         "cartage": "Cartage amount (or a dash)", "cartage_line": "The whole 'Cartage: 50' line - empty for By Company / By Transport",
                         "details": "The extra text for By Porter / By Company / By Transport (or a dash)",
                         "details_line": "The whole 'Details: ...' line - empty when there is no extra text", "photo": "Link to the dispatch photo(s), or a dash"}
DISPATCH_SAMPLE = {"order_date": "10/10/2026", "dc_no": "94", "ready_by": "Jairam", "delivery_status": "Sent", "address": "Home: H-2/84,85 Sec-16 Rohini",
                   "remarks": "-", "delivered_by": "Ranjeet(E-R) 8796637988", "cartage": "50", "cartage_line": "Cartage: 50\n", "details": "-", "details_line": "",
                   "photo": "https://example.com/dispatch-photo-94.jpg"}

# key -> label, when it is sent, the group it is for, the {placeholders} its template may use, sample values, default template.
# group_from: when this alert's own group is empty it uses that alert's group.  app_title: the web (bell) notification, where there is one.
ALERTS: dict[str, dict] = {
    "dispatch_godown": {
        "label": "Godown dispatch alert", "group_label": "VT Dispatch group",
        "when": "When Godown Dispatch first fills in the delivery date/time and Delivered by on an order (and on 'Resend'). For a normal delivery person, By Company and By Transport. "
                "By Porter goes to the Porter group instead. Photos are added when they were taken in the same save.",
        "placeholders": DISPATCH_PLACEHOLDERS, "sample": DISPATCH_SAMPLE, "template": DISPATCH_TEMPLATE},
    "dispatch_porter": {
        "label": "Porter dispatch alert", "group_label": "Porter group", "group_from": "dispatch_godown",
        "when": "The same dispatch message, when Delivered by is By Porter (it goes to the Porter group, with the cartage and the porter's details). "
                "If this group is empty, it goes to the VT Dispatch group.",
        "placeholders": DISPATCH_PLACEHOLDERS,
        "sample": {**DISPATCH_SAMPLE, "delivered_by": "By Porter", "details": "Ramesh, 9811122233", "details_line": "Details: Ramesh, 9811122233\n"},
        "template": DISPATCH_TEMPLATE},
    "dispatch_receiving": {
        "label": "Receiving group dispatch alert", "group_label": "VT Receiving group",
        "when": "The same dispatch message, also sent to the Receiving group so the people on the Shop Dispatch & Receiving / Godown Receiving dashboards know what is coming.",
        "placeholders": DISPATCH_PLACEHOLDERS, "sample": DISPATCH_SAMPLE, "template": DISPATCH_TEMPLATE},
    "att_checkin": {
        "label": "Attendance - check-in message", "group_label": "Attendance group",
        "when": "When an employee checks in (the attendance page), unless the employee is set to 'no WhatsApp' in Setup > Dropdown values > Attendance employees.",
        "placeholders": {"icon": "Status colour dot", "name": "Employee", "time": "Check-in time (12-hour)", "status": "Present / Late / Half Day",
                         "site_icon": "Place icon", "site": "Godown / Shop", "dist": "Distance, like ' (12m)'", "date": "Date (dd Mon yyyy)"},
        "sample": {"icon": "🟢", "name": "Ramesh Kumar", "time": "09:12 AM", "status": "Present", "site_icon": "🏭", "site": "Godown", "dist": " (12m)", "date": "19 Oct 2026"},
        "template": ("{icon} *CHECK-IN* — Vardhman Traders\n" + RULE + "\n👤 *Naam:* {name}\n🕐 *Time:* {time}\n📊 *Status:* {status}\n"
                     "{site_icon} *Location:* {site}{dist}\n📅 *Date:* {date}\n" + RULE)},
    "att_checkout": {
        "label": "Attendance - check-out message", "group_label": "Attendance group", "group_from": "att_checkin",
        "when": "When an employee checks out, or the owner checks someone out by hand. Empty group = the check-in group.",
        "placeholders": {"name": "Employee", "check_in": "Check-in time", "check_out": "Check-out time", "hours": "Hours worked",
                         "final_status": "Present / Late / Half Day / Absent", "by_line": "'Checkout by: Owner (Manual)' line, empty otherwise",
                         "date": "Date (dd Mon yyyy)"},
        "sample": {"name": "Ramesh Kumar", "check_in": "09:12 AM", "check_out": "07:05 PM", "hours": "9h 53m", "final_status": "Present", "by_line": "", "date": "19 Oct 2026"},
        "template": ("🔴 *CHECK-OUT* — Vardhman Traders\n" + RULE + "\n👤 *Naam:* {name}\n🕐 *Check-in:* {check_in}\n"
                     "🏁 *Check-out:* {check_out}\n⏱️ *Hours:* {hours}\n📊 *Final Status:* {final_status}\n{by_line}📅 *Date:* {date}\n" + RULE)},
    "att_auto_summary": {
        "label": "Attendance - auto check-out summary", "group_label": "Attendance group", "group_from": "att_checkin",
        "when": "Once a day after the auto check-out runs: one message listing everyone who forgot to check out. Empty group = the check-in group.",
        "placeholders": {"auto_time": "The auto check-out time (12-hour)", "list": "Numbered list: name, check-in time, status", "date": "Date (dd Mon yyyy)"},
        "sample": {"auto_time": "8:00 PM", "list": "1. Ramesh Kumar - in 09:12 AM (Present)\n2. Sunita - in 10:45 AM (Late)", "date": "19 Oct 2026"},
        "template": ("🌙 *AUTO CHECK-OUT* — Vardhman Traders\n" + RULE + "\nInhone checkout nahi kiya tha — system ne {auto_time} pe kar diya:\n\n"
                     "{list}\n\n📅 {date}\n" + RULE)},
    "delivery_day_summary": {
        "label": "Daily delivery summary (7:30 PM)", "group_label": "Reports group", "group_from": "reports_evening",
        "when": "Every day at 7:30 PM IST, after the per-person delivery reports are built: one message listing each delivery person's orders. Empty group = the Reports group.",
        "placeholders": {"date": "The day (dd-mm-yyyy)", "summary": "One line per delivery person: name, number of orders, cartage"},
        "sample": {"date": "19-10-2026", "summary": "Ranjeet(E-R) - 4 orders, cartage Rs 200\nBy Porter - 2 orders, cartage Rs 150"},
        "template": "Delivery summary - {date}\n\n{summary}"},
    "reports_evening": {
        "label": "Daily reports - 7:30 PM", "group_label": "Reports group",
        "when": "Every day at 7:30 PM IST, when these reports are built: every delivery person's report, Godown / Shop received & payments (cash) and (other mode). "
                "One message with a download link for each PDF (links work without a login and expire after 3 days).",
        "placeholders": {"date": "The day (dd-mm-yyyy)", "time": "The time of this batch", "list": "One line per report with its link"},
        "sample": {"date": "19-10-2026", "time": "7:30 PM", "list": "1. Delivery - Ranjeet(E-R) (4 orders)\nhttps://example.com/r/1\n2. Godown received & payments - cash (6 orders)\nhttps://example.com/r/2"},
        "template": "*Reports - {time}* - Vardhman Traders\n" + RULE + "\n📅 {date}\n\n{list}\n" + RULE},
    "reports_night": {
        "label": "Daily reports - 9:00 PM", "group_label": "Reports group", "group_from": "reports_evening",
        "when": "Every day at 9:00 PM IST: Awaiting receiving, Godown pending orders, Shop Dispatch pending orders and Godown Dispatch pending orders, as download links. "
                "Empty group = the 7:30 PM reports group.",
        "placeholders": {"date": "The day (dd-mm-yyyy)", "time": "The time of this batch", "list": "One line per report with its link"},
        "sample": {"date": "19-10-2026", "time": "9:00 PM", "list": "1. Awaiting receiving (25 orders)\nhttps://example.com/r/3\n2. Godown - pending orders (38 orders)\nhttps://example.com/r/4"},
        "template": "*Reports - {time}* - Vardhman Traders\n" + RULE + "\n📅 {date}\n\n{list}\n" + RULE},
    "deleg_assigned": {
        "label": "Delegation - task given", "group_label": "Delegation group", "app_title": "New task: {task}",
        "when": "As soon as a task is given (or handed to someone else). The person it is for and every admin also get it on the web bell.",
        "placeholders": {"name": "Who the task is for", "task": "The task", "due": "Deadline (dd-mm-yyyy)", "assigned_by": "Who gave it", "task_id": "Task number"},
        "sample": {"name": "Asha Verma", "task": "Count and tag the new stock in rack B", "due": "25-10-2026", "assigned_by": "Meera Manager", "task_id": "T-00123"},
        "template": ("*New task* - Vardhman Traders\n" + RULE + "\n👤 {name}\n📝 {task}\n📅 Deadline: {due}\n🙋 Given by: {assigned_by}\n"
                     "🔖 {task_id}\n" + RULE)},
    "deleg_done": {
        "label": "Delegation - task completed", "group_label": "Delegation group", "group_from": "deleg_assigned", "app_title": "Task completed by {name}: {task}",
        "when": "When a person marks a task complete. Empty group = the 'task given' group. Every admin also gets it on the web bell.",
        "placeholders": {"name": "Who did the task", "task": "The task", "due": "Deadline (dd-mm-yyyy)", "timing": "'on time' or 'late'",
                         "score": "Points earned", "notes": "The note they wrote (or a dash)", "task_id": "Task number"},
        "sample": {"name": "Asha Verma", "task": "Count and tag the new stock in rack B", "due": "25-10-2026", "timing": "on time", "score": "10",
                   "notes": "Counted 142 rolls", "task_id": "T-00123"},
        "template": ("*Task completed* - Vardhman Traders\n" + RULE + "\n👤 {name}\n📝 {task}\n📅 Deadline: {due} ({timing})\n"
                     "⭐ Points: {score}\n🗒️ {notes}\n🔖 {task_id}\n" + RULE)},
    "chk_assigned": {
        "label": "Checklist - checklist given", "group_label": "Checklist group", "app_title": "New checklist: {title}",
        "when": "When a checklist is created for someone (or handed to someone else). The person and every admin also get it on the web bell.",
        "placeholders": {"name": "Who the checklist is for", "title": "The checklist", "when": "How often, like 'Every Saturday'", "assigned_by": "Who set it"},
        "sample": {"name": "Sunil Yadav", "title": "Close the cash register and lock the godown", "when": "Every day (except the weekly off)", "assigned_by": "Meera Manager"},
        "template": ("*New checklist* - Vardhman Traders\n" + RULE + "\n👤 {name}\n✅ {title}\n🔁 {when}\n🙋 Set by: {assigned_by}\n" + RULE)},
    "chk_done": {
        "label": "Checklist - item marked", "group_label": "Checklist group", "group_from": "chk_assigned", "app_title": "Checklist {status}: {title} ({name})",
        "when": "Each time a checklist item is marked Done or Not Done. Off to start with - a daily checklist sends one message per item. Empty group = the 'checklist given' group.",
        "placeholders": {"name": "Who the item belongs to", "title": "The checklist", "due": "Due date (dd-mm-yyyy)", "status": "Done / Not Done",
                         "remark": "The reason (or a dash)", "marked_by": "Who marked it"},
        "sample": {"name": "Sunil Yadav", "title": "Close the cash register and lock the godown", "due": "19-10-2026", "status": "Done", "remark": "-", "marked_by": "Sunil Yadav"},
        "template": ("*Checklist item {status}* - Vardhman Traders\n" + RULE + "\n👤 {name}\n✅ {title}\n📅 Due: {due}\n🗒️ {remark}\n" + RULE)},
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


def _read(key: str) -> dict:
    row = None
    try:
        with db.cursor() as cur:
            cur.execute("SELECT group_id, enabled, template FROM whatsapp_alert WHERE alert_key = %s", (key,))
            row = cur.fetchone()
    except Exception:
        log.warning("Could not read the WhatsApp settings for %s - using defaults", key)
    return row or {}


def get_alert(key: str) -> dict:
    """The saved settings of one alert type, or the built-in defaults when the database can't be read. `group_id` is the group the
    alert actually goes to: its own, else the old environment group (dispatch), else the group of the alert named in `group_from`."""
    spec = ALERTS[key]
    row = _read(key)
    group = (row.get("group_id") or "").strip() or _legacy_group(key)
    source = "own" if (row.get("group_id") or "").strip() else ("environment" if group else "none")
    if not group and spec.get("group_from"):
        other = spec["group_from"]
        group = ((_read(other).get("group_id") or "").strip() or _legacy_group(other))
        source = "fallback" if group else "none"
    return {"group_id": group, "group_source": source, "enabled": True if not row else bool(row["enabled"]),
            "template": row.get("template") or spec["template"]}


def render(template: str, values: dict) -> str:
    """Fill {placeholders}. Unknown names are left as written; no other formatting is interpreted."""
    return PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def sample_message(key: str, template: str | None = None) -> str:
    """The alert's message filled with its sample values (what Setup previews and what 'Send a sample' posts)."""
    return render(template or get_alert(key)["template"], ALERTS[key]["sample"])


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


def _post_to_group(group: str, text: str, post=None) -> dict:
    post = post or _http_post          # looked up when called, so a test (or a different sender) can replace it
    headers = auth_headers()
    if not headers:
        return {"skipped": True, "reason": "WhatsApp credentials not set."}
    if not group:
        return {"skipped": True, "reason": "WhatsApp group not set."}
    payload = {"recipientIds": [group], "message": [text]}
    if os.environ.get("WHATSAPP_CHANNEL_ID", "").strip().isdigit():   # only when the account has more than one WhatsApp number
        payload["channelId"] = int(os.environ["WHATSAPP_CHANNEL_ID"].strip())
    code, reply = post(API_URL, json.dumps(payload).encode(), {"Content-Type": "application/json", **headers})
    return {"code": code, "body": reply}


def send_text(key: str, text: str, post=None, ignore_switch: bool = False) -> dict:
    """Send `text` to the group set for this alert type. {"skipped": True, "reason": ...} when it cannot go out."""
    settings = get_alert(key)
    if not settings["enabled"] and not ignore_switch:
        return {"skipped": True, "reason": "This alert is switched off in Setup."}
    return _post_to_group(settings["group_id"], text, post)


def send_alert(key: str, values: dict, prefix: str = "", post=None) -> dict:
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
        eff = get_alert(key)
        out.append({"key": key, "label": spec["label"], "when": spec["when"], "placeholders": spec["placeholders"],
                    "default_template": spec["template"], "template": row.get("template") or spec["template"],
                    "enabled": True if not row else bool(row["enabled"]),
                    "group_id": (row.get("group_id") or "").strip(), "group_from_environment": bool(not (row.get("group_id") or "").strip() and _legacy_group(key)),
                    "group_label": spec.get("group_label", ""), "group_from": spec.get("group_from"),
                    "group_from_label": ALERTS[spec["group_from"]]["label"] if spec.get("group_from") else "",
                    "group_effective": bool(eff["group_id"]), "group_source": eff["group_source"],
                    "sample": spec["sample"], "web_bell": bool(spec.get("app_title")),
                    "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None})
    return {"alerts": out, "credentials_set": credentials_set()}


class AlertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str = Field(default="", max_length=100)
    enabled: bool = True
    template: str = Field(min_length=1, max_length=2000)


def _check(spec: dict, group: str, template: str) -> str:
    group = group.strip()
    if re.search(r"\s", group):
        raise HTTPException(422, "The group ID must not contain spaces or line breaks.")
    unknown = sorted(set(PLACEHOLDER.findall(template)) - set(spec["placeholders"]))
    if unknown:
        raise HTTPException(422, "Unknown placeholder(s) in the message: " + ", ".join("{" + u + "}" for u in unknown) +
                            ". You can use: " + ", ".join("{" + p + "}" for p in spec["placeholders"]))
    return group


@router.put("/{key}")
def save_alert(key: str, body: AlertIn, admin=Depends(ADMIN)):
    spec = ALERTS.get(key)
    if not spec:
        raise HTTPException(404, "Unknown alert")
    group = _check(spec, body.group_id, body.template)
    with db.cursor() as cur:
        cur.execute("INSERT INTO whatsapp_alert (alert_key, label, group_id, enabled, template, updated_by_user_key, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, now()) ON CONFLICT (alert_key) DO UPDATE SET group_id = EXCLUDED.group_id, "
                    "enabled = EXCLUDED.enabled, template = EXCLUDED.template, updated_by_user_key = EXCLUDED.updated_by_user_key, "
                    "updated_at = now()", (key, spec["label"], group or None, body.enabled, body.template.strip(), admin["user_key"]))
        audit(cur, admin, "whatsapp.save", key, {"group_set": bool(group), "enabled": body.enabled})
    return {"ok": True}


class TestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str | None = Field(default=None, max_length=100)     # what is typed on the screen, saved or not
    template: str | None = Field(default=None, max_length=2000)


@router.post("/{key}/test")
def send_test(key: str, body: TestIn | None = None, admin=Depends(ADMIN)):
    """Post the alert's message, filled with its sample values and clearly marked as a sample, to the group - the one typed on the screen
    (saved or not), else the saved one - so the group ID AND the wording can be checked without a real order."""
    spec = ALERTS.get(key)
    if not spec:
        raise HTTPException(404, "Unknown alert")
    body = body or TestIn()
    group = _check(spec, body.group_id, body.template or "") if body.group_id is not None else get_alert(key)["group_id"]
    if body.group_id is not None and not group:
        group = get_alert(key)["group_id"]                                     # a blank box tests the group already in use (own or fallback)
    template = body.template if body.template and body.template.strip() else get_alert(key)["template"]
    if body.template:
        _check(spec, "", template)
    text = "🧪 *SAMPLE - please ignore* (Setup > WhatsApp: " + spec["label"] + ")\n\n" + render(template, spec["sample"])
    try:
        out = _post_to_group(group, text, post=_http_post)                       # looked up now, so tests can swap the sender
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
            "message": "The provider accepted the sample message. Check the group" + (f" (provider reply: {shown})." if shown else ".") +
                       " If nothing arrives there, the group ID is not the one the provider knows - copy it again from the provider's group list."}
