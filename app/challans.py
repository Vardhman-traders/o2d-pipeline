"""Upload Challan, in the Payments module: a plain register of challan photos. Someone uploads the picture (or a PDF), says which
date the challan is for and optionally its number; the list shows the picture with the date, who uploaded it and when.

  - the file goes to the same private R2 storage as every other photo (file_attachment, module 'challan'); nothing here is public,
    the list hands out short-lived signed links;
  - who may look is the page "Payments - Upload challan"; "View only" can look but not upload (Setup > Access);
  - an admin may edit (date, number, note, replace the file) and delete any challan; the person who uploaded one may edit their own (never delete);
    every edit and delete is written to the audit log.
"""
from psycopg2.extras import Json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from . import access, attachments, auth, db, modcommon as mc, roles

router = APIRouter(prefix="/payments/challans", tags=["challans"])
PAGE = "pay_challan"
ANY = auth.require_roles(*roles.ALL_ROLES)
MAX_ROWS = 2000


@router.get("")
def list_challans(date_from: str = Query(default=""), date_to: str = Query(default=""), user=Depends(access.require_page(PAGE))):
    d_from, d_to = mc.parse_date(date_from, "From date"), mc.parse_date(date_to, "To date")
    with db.cursor() as cur:
        cur.execute("""SELECT c.challan_key, d.full_date AS challan_date, c.reference, c.note, c.uploaded_at, u.display_name AS uploaded_by, c.uploaded_by_user_key,
                              f.object_key, f.content_type
                       FROM fact_challan_upload c
                       JOIN dim_date d ON d.date_key = c.challan_date_key
                       JOIN file_attachment f ON f.attachment_key = c.attachment_key
                       LEFT JOIN dim_user u ON u.user_key = c.uploaded_by_user_key
                       WHERE (%s::date IS NULL OR d.full_date >= %s) AND (%s::date IS NULL OR d.full_date <= %s)
                       ORDER BY d.full_date DESC, c.challan_key DESC LIMIT %s""", (d_from, d_from, d_to, d_to, MAX_ROWS + 1))
        rows = cur.fetchall()
    enabled = attachments.configured()
    can_write = not access.is_view_only(user, PAGE)
    is_admin = user["role"] == "admin"
    out = []
    for r in rows[:MAX_ROWS]:
        url = ""
        if enabled:
            url = attachments.storage().generate_presigned_url(
                "get_object", Params={"Bucket": attachments._bucket(), "Key": r["object_key"]}, ExpiresIn=attachments.LINK_SECONDS)
        out.append({"key": r["challan_key"], "date": r["challan_date"].isoformat(), "reference": r["reference"] or "", "note": r["note"] or "",
                    "uploadedBy": r["uploaded_by"] or "", "uploadedAt": r["uploaded_at"].astimezone(mc.IST).strftime("%d-%m-%Y %H:%M"),
                    "url": url, "isPdf": r["content_type"] == "application/pdf",
                    "canEdit": is_admin or (can_write and r["uploaded_by_user_key"] == user["user_key"]), "canDelete": is_admin})
    return {"ok": True, "enabled": enabled, "today": mc.today_ist().isoformat(), "canWrite": can_write,
            "truncated": len(rows) > MAX_ROWS, "rows": out}


class UploadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = ""
    reference: str = ""
    note: str = ""
    data: str          # a data: URL (photo or PDF)


@router.post("", status_code=201)
def upload(body: UploadIn, user=Depends(access.require_page(PAGE))):
    access.require_edit(user, PAGE)
    if not attachments.configured():
        raise HTTPException(503, "Photo storage is not set up yet. Ask the administrator.")
    today = mc.today_ist()
    day = mc.parse_date(body.date, "Challan date") or today
    if day > today:
        raise HTTPException(422, "The challan date cannot be in the future.")
    ref = " ".join(body.reference.split())[:100] or None
    note = " ".join(body.note.split())[:500] or None
    attachment = attachments.put_data_url("challan", 0, "challan", user, body.data)   # 415 / 413 / 422 say what is wrong
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO fact_challan_upload (challan_date_key, reference, note, attachment_key, uploaded_by_user_key) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING challan_key",
                        (mc.date_key(day), ref, note, attachment, user["user_key"]))
            key = cur.fetchone()["challan_key"]
    except Exception as exc:
        raise mc.date_fk_error(exc)
    return {"ok": True, "key": key}


class EditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = ""
    reference: str = ""
    note: str = ""
    data: str = ""     # a data: URL: only when the file itself is replaced


def _load(cur, key: int) -> dict:
    cur.execute("SELECT c.challan_key, c.challan_date_key, c.reference, c.note, c.attachment_key, c.uploaded_by_user_key, f.object_key "
                "FROM fact_challan_upload c JOIN file_attachment f ON f.attachment_key = c.attachment_key WHERE c.challan_key = %s FOR UPDATE OF c", (key,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "That challan was not found - it may have been deleted.")
    return row


def _drop_file(attachment_key: int, object_key: str) -> None:
    with db.cursor() as cur:
        cur.execute("DELETE FROM file_attachment WHERE attachment_key = %s", (attachment_key,))
    try:
        if attachments.configured():
            attachments.storage().delete_object(Bucket=attachments._bucket(), Key=object_key)
    except Exception:  # the row is gone either way; an orphaned object is harmless
        pass


def _audit(cur, user, action: str, key: int, details: dict) -> None:
    cur.execute("INSERT INTO admin_audit_log (admin_user_key, admin_username, action, target, details) VALUES (%s, %s, %s, %s, %s)",
                (user["user_key"], user["username"], action, f"challan {key}", Json(details)))


@router.put("/{key}")
def edit(key: int, body: EditIn, user=Depends(access.require_page(PAGE))):
    access.require_edit(user, PAGE)
    today = mc.today_ist()
    day = mc.parse_date(body.date, "Challan date") or today
    if day > today:
        raise HTTPException(422, "The challan date cannot be in the future.")
    ref = " ".join(body.reference.split())[:100] or None
    note = " ".join(body.note.split())[:500] or None
    new_file = None
    if body.data:
        if not attachments.configured():
            raise HTTPException(503, "Photo storage is not set up yet. Ask the administrator.")
        new_file = attachments.put_data_url("challan", 0, "challan", user, body.data)
    old = None
    try:
        with db.cursor() as cur:
            row = _load(cur, key)
            if user["role"] != "admin" and row["uploaded_by_user_key"] != user["user_key"]:
                raise HTTPException(403, "Only the admin or the person who uploaded this challan can edit it.")
            cur.execute("UPDATE fact_challan_upload SET challan_date_key = %s, reference = %s, note = %s, attachment_key = COALESCE(%s, attachment_key) "
                        "WHERE challan_key = %s", (mc.date_key(day), ref, note, new_file, key))
            _audit(cur, user, "challan.edit", key, {"before": {"date_key": row["challan_date_key"], "reference": row["reference"], "note": row["note"]},
                                                   "after": {"date_key": mc.date_key(day), "reference": ref, "note": note}, "file_replaced": bool(new_file)})
            if new_file:
                old = (row["attachment_key"], row["object_key"])
    except HTTPException:
        if new_file:   # nothing was saved: do not keep the orphaned upload
            with db.cursor() as cur:
                cur.execute("DELETE FROM file_attachment WHERE attachment_key = %s", (new_file,))
        raise
    except Exception as exc:
        raise mc.date_fk_error(exc)
    if old:
        _drop_file(*old)
    return {"ok": True}


@router.delete("/{key}")
def delete(key: int, user=Depends(auth.require_roles("admin"))):
    with db.cursor() as cur:
        row = _load(cur, key)
        cur.execute("DELETE FROM fact_challan_upload WHERE challan_key = %s", (key,))
        _audit(cur, user, "challan.delete", key, {"date_key": row["challan_date_key"], "reference": row["reference"], "note": row["note"]})
    _drop_file(row["attachment_key"], row["object_key"])
    return {"ok": True}
