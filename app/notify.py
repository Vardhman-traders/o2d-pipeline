"""Notifications for Delegation and Checklist: when a task / checklist is given to someone, or a task is completed or a checklist item is
marked, the people the alert is set to reach hear about it twice - on their own WhatsApp number and on the web (the bell on the Delegation
screen).

Who hears is decided per alert under Setup > WhatsApp (whatsapp.ALERTS, the four personal alerts): the person it concerns, the EA (an
employee chosen there) and the admin (every admin's web screen, plus any admin WhatsApp numbers listed there). A WhatsApp number is the
`phone_number` on the attendance employee (Setup > Dropdown values > Attendance employees); a member and an employee are the same person
(dim_att_employee.user_key). The message goes through the same provider call, login and answer check as every other WhatsApp message
(whatsapp.send_personal / reply_ok).

A notification never makes the action it follows fail: it is sent after the work is saved, in the background, and every attempt - sent, or
why not - is written to notify_log.
"""
import logging
import os
import threading

from fastapi import APIRouter, Depends, HTTPException

from . import auth, db, roles, whatsapp
from .admin import ADMIN

log = logging.getLogger("notify")
router = APIRouter(prefix="/notifications", tags=["notifications"])
ANY = auth.require_roles(*roles.ALL_ROLES)
BACKGROUND = os.environ.get("VT_NOTIFY_INLINE") != "1"      # the tests send inline so they can look at the result
MODULE_OF = {"deleg": "delegation", "chk": "checklist"}


def _log(alert_key: str, channel: str, recipient: str, ok: bool, detail: str = "") -> None:
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO notify_log (alert_key, channel, recipient, ok, detail) VALUES (%s, %s, %s, %s, %s)",
                        (alert_key, channel, recipient[:150], ok, (detail or "")[:500]))
    except Exception:
        log.exception("could not write the notification log")


def _employee_by_user(cur, user_key: int) -> dict | None:
    cur.execute("SELECT employee_name, phone_number, user_key FROM dim_att_employee WHERE user_key = %s", (user_key,))
    return cur.fetchone()


def recipients(settings: dict, assignee_user_key: int | None, actor_user_key: int | None = None) -> list[dict]:
    """[{label, user_key, phone}] - one entry per person, whatever roles they hold (a person who is both the EA and the assignee hears once).
    Whoever did the thing (gave the task, marked the item) is not told about their own action."""
    out: dict = {}

    def add(label: str, user_key, phone):
        key = ("u", user_key) if user_key else ("p", phone)
        cur = out.setdefault(key, {"label": label, "user_key": user_key, "phone": None})
        cur["phone"] = cur["phone"] or whatsapp.normalize_phone(phone)

    with db.cursor() as cur:
        if settings["to_assignee"] and assignee_user_key:
            cur.execute("SELECT display_name FROM dim_user WHERE user_key = %s", (assignee_user_key,))
            u = cur.fetchone()
            e = _employee_by_user(cur, assignee_user_key)
            add(u["display_name"] if u else "assignee", assignee_user_key, e["phone_number"] if e else None)
        if settings["to_ea"] and settings["ea_employee_key"]:
            cur.execute("SELECT employee_name, phone_number, user_key FROM dim_att_employee WHERE employee_key = %s AND is_active",
                        (settings["ea_employee_key"],))
            e = cur.fetchone()
            if e:
                add("EA " + e["employee_name"], e["user_key"], e["phone_number"])
        if settings["to_admin"]:
            cur.execute("SELECT user_key, display_name FROM dim_user WHERE role = 'admin' AND password_hash <> %s", (auth.DISABLED_HASH,))
            for u in cur.fetchall():
                add("admin " + u["display_name"], u["user_key"], None)
            for n in whatsapp.split_numbers(settings["admin_numbers"]) or []:
                add("admin " + n, None, n)
    return [r for r in out.values() if not (actor_user_key and r["user_key"] == actor_user_key)]


def _deliver(key: str, values: dict, assignee_user_key: int | None, actor_user_key: int | None) -> None:
    spec = whatsapp.ALERTS[key]
    settings = whatsapp.get_alert(key)
    if not settings["enabled"]:
        return
    text = whatsapp.render(settings["template"], values)
    title = whatsapp.render(spec["app_title"], values)[:200]
    module = MODULE_OF[key.split("_", 1)[0]]
    sent_to: set = set()
    for r in recipients(settings, assignee_user_key, actor_user_key):
        if r["user_key"]:
            try:
                with db.cursor() as cur:
                    cur.execute("INSERT INTO app_notification (user_key, module, title, body) VALUES (%s, %s, %s, %s)",
                                (r["user_key"], module, title, text))
                _log(key, "app", r["label"], True)
            except Exception as exc:
                log.exception("in-app notification failed")
                _log(key, "app", r["label"], False, f"EXCEPTION: {exc}")
        if not r["phone"]:
            if r["user_key"] and not str(r["label"]).startswith("admin"):   # an admin has the web screen; anyone else without a number is worth knowing about
                _log(key, "whatsapp", r["label"], False, "No WhatsApp number on the employee (Setup > Dropdown values > Attendance employees).")
            continue
        if r["phone"] in sent_to:
            continue
        sent_to.add(r["phone"])
        try:
            out = whatsapp.send_personal(key, r["phone"], text, post=whatsapp._http_post, ignore_switch=True)   # looked up now, so tests can swap the sender
        except Exception as exc:
            log.exception("WhatsApp personal message failed")
            _log(key, "whatsapp", r["label"], False, f"EXCEPTION: {exc}")
            continue
        if out.get("skipped"):
            _log(key, "whatsapp", r["label"], False, out["reason"])
        else:
            _log(key, "whatsapp", r["label"], whatsapp.reply_ok(out.get("code"), out.get("body", "")), out.get("body", ""))


def fire(key: str, values: dict, assignee_user_key: int | None = None, actor_user_key: int | None = None) -> None:
    """Tell the people alert `key` is set to reach. Call it after the work is saved; it never raises."""
    def work():
        try:
            _deliver(key, values, assignee_user_key, actor_user_key)
        except Exception:
            log.exception("notification %s failed", key)

    if BACKGROUND:
        threading.Thread(target=work, name="notify-" + key, daemon=True).start()
    else:
        work()


# ------------------------------------------------------------------ the bell (web notifications)
@router.get("")
def mine(user=Depends(ANY)):
    with db.cursor() as cur:
        cur.execute("SELECT notification_key, module, title, body, created_at, read_at FROM app_notification WHERE user_key = %s "
                    "ORDER BY created_at DESC, notification_key DESC LIMIT 40", (user["user_key"],))
        rows = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM app_notification WHERE user_key = %s AND read_at IS NULL", (user["user_key"],))
        unread = cur.fetchone()["n"]
    return {"unread": unread, "items": [{"id": r["notification_key"], "module": r["module"], "title": r["title"], "body": r["body"] or "",
                                         "at": r["created_at"].isoformat(), "read": r["read_at"] is not None} for r in rows]}


@router.post("/read-all")
def read_all(user=Depends(ANY)):
    with db.cursor() as cur:
        cur.execute("UPDATE app_notification SET read_at = now() WHERE user_key = %s AND read_at IS NULL", (user["user_key"],))
    return {"ok": True}


@router.post("/{notification_key}/read")
def read_one(notification_key: int, user=Depends(ANY)):
    with db.cursor() as cur:
        cur.execute("UPDATE app_notification SET read_at = COALESCE(read_at, now()) WHERE notification_key = %s AND user_key = %s RETURNING 1",
                    (notification_key, user["user_key"]))
        if not cur.fetchone():
            raise HTTPException(404, "Notification not found.")
    return {"ok": True}


@router.get("/log")
def delivery_log(limit: int = 100, admin=Depends(ADMIN)):
    """Admin: what was sent to whom, and why a message did not go out."""
    with db.cursor() as cur:
        cur.execute("SELECT at, alert_key, channel, recipient, ok, detail FROM notify_log ORDER BY log_key DESC LIMIT %s", (max(1, min(limit, 500)),))
        return [{"at": r["at"].isoformat(), "alert": r["alert_key"], "channel": r["channel"], "recipient": r["recipient"], "ok": r["ok"],
                 "detail": r["detail"] or ""} for r in cur.fetchall()]
