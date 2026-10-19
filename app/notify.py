"""Notifications for Delegation and Checklist: when a task / checklist is given, a task is completed or a checklist item is marked, the
message goes to the WhatsApp GROUP set for that alert (Setup > WhatsApp - one group for Delegation, one for Checklist) and shows on the
web bell of the person it concerns and of every admin.

The WhatsApp message uses the same provider call, login and answer check as every other alert (whatsapp.send_alert / reply_ok). A
notification never makes the action it follows fail: it is sent after the work is saved, in the background, and every attempt - sent, or
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


def bell_recipients(assignee_user_key: int | None, actor_user_key: int | None = None) -> list[dict]:
    """Who sees the web notification: the person it concerns and every admin (whoever did the thing is not told about their own action)."""
    out: dict[int, str] = {}
    with db.cursor() as cur:
        if assignee_user_key:
            cur.execute("SELECT display_name FROM dim_user WHERE user_key = %s", (assignee_user_key,))
            u = cur.fetchone()
            out[assignee_user_key] = u["display_name"] if u else "assignee"
        cur.execute("SELECT user_key, display_name FROM dim_user WHERE role = 'admin' AND password_hash <> %s", (auth.DISABLED_HASH,))
        for u in cur.fetchall():
            out.setdefault(u["user_key"], "admin " + u["display_name"])
    return [{"user_key": k, "label": v} for k, v in out.items() if k != actor_user_key]


def _deliver(key: str, values: dict, assignee_user_key: int | None, actor_user_key: int | None) -> None:
    spec = whatsapp.ALERTS[key]
    settings = whatsapp.get_alert(key)
    if not settings["enabled"]:
        return
    text = whatsapp.render(settings["template"], values)
    title = whatsapp.render(spec["app_title"], values)[:200]
    module = MODULE_OF[key.split("_", 1)[0]]
    for r in bell_recipients(assignee_user_key, actor_user_key):
        try:
            with db.cursor() as cur:
                cur.execute("INSERT INTO app_notification (user_key, module, title, body) VALUES (%s, %s, %s, %s)", (r["user_key"], module, title, text))
            _log(key, "app", r["label"], True)
        except Exception as exc:
            log.exception("in-app notification failed")
            _log(key, "app", r["label"], False, f"EXCEPTION: {exc}")
    try:
        out = whatsapp.send_text(key, text, post=whatsapp._http_post)       # looked up now, so tests can swap the sender
    except Exception as exc:
        log.exception("WhatsApp group message failed")
        _log(key, "whatsapp", settings["group_id"] or "(no group)", False, f"EXCEPTION: {exc}")
        return
    if out.get("skipped"):
        _log(key, "whatsapp", settings["group_id"] or "(no group)", False, out["reason"])
    else:
        _log(key, "whatsapp", settings["group_id"], whatsapp.reply_ok(out.get("code"), out.get("body", "")), out.get("body", ""))


def fire(key: str, values: dict, assignee_user_key: int | None = None, actor_user_key: int | None = None) -> None:
    """Tell the group alert `key` is set for (and the web bells). Call it after the work is saved; it never raises."""
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
    """Admin: what was sent, and why a message did not go out."""
    with db.cursor() as cur:
        cur.execute("SELECT at, alert_key, channel, recipient, ok, detail FROM notify_log ORDER BY log_key DESC LIMIT %s", (max(1, min(limit, 500)),))
        return [{"at": r["at"].isoformat(), "alert": r["alert_key"], "channel": r["channel"], "recipient": r["recipient"], "ok": r["ok"],
                 "detail": r["detail"] or ""} for r in cur.fetchall()]
