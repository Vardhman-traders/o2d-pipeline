"""Upload Challan, in the Payments module: a plain register of challan photos. Someone uploads the picture (or a PDF), says which
date the challan is for and optionally its number; the list shows the picture with the date, who uploaded it and when.

  - the file goes to the same private R2 storage as every other photo (file_attachment, module 'challan'); nothing here is public,
    the list hands out short-lived signed links;
  - who may look is the page "Payments - Upload challan"; "View only" can look but not upload (Setup > Access);
  - uploads are not edited or removed here: a wrong one is simply uploaded again (the register is the record).
"""
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
        cur.execute("""SELECT c.challan_key, d.full_date AS challan_date, c.reference, c.note, c.uploaded_at, u.display_name AS uploaded_by,
                              f.object_key, f.content_type
                       FROM fact_challan_upload c
                       JOIN dim_date d ON d.date_key = c.challan_date_key
                       JOIN file_attachment f ON f.attachment_key = c.attachment_key
                       LEFT JOIN dim_user u ON u.user_key = c.uploaded_by_user_key
                       WHERE (%s::date IS NULL OR d.full_date >= %s) AND (%s::date IS NULL OR d.full_date <= %s)
                       ORDER BY d.full_date DESC, c.challan_key DESC LIMIT %s""", (d_from, d_from, d_to, d_to, MAX_ROWS + 1))
        rows = cur.fetchall()
    enabled = attachments.configured()
    out = []
    for r in rows[:MAX_ROWS]:
        url = ""
        if enabled:
            url = attachments.storage().generate_presigned_url(
                "get_object", Params={"Bucket": attachments._bucket(), "Key": r["object_key"]}, ExpiresIn=attachments.LINK_SECONDS)
        out.append({"key": r["challan_key"], "date": r["challan_date"].isoformat(), "reference": r["reference"] or "", "note": r["note"] or "",
                    "uploadedBy": r["uploaded_by"] or "", "uploadedAt": r["uploaded_at"].astimezone(mc.IST).strftime("%d-%m-%Y %H:%M"),
                    "url": url, "isPdf": r["content_type"] == "application/pdf"})
    return {"ok": True, "enabled": enabled, "today": mc.today_ist().isoformat(), "canWrite": not access.is_view_only(user, PAGE),
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
