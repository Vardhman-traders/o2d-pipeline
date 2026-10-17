"""Photos attached to an order at dispatch or receiving. The image bytes live in Cloudflare R2 (S3 compatible); the
database only keeps the object key and who/when. Nothing is public: the API checks the person may see the order
(same rule as the order screens), then hands out a short-lived signed link.

Switched on by four environment variables on the web service. Without them the screens simply hide the photo box:
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
"""
import logging
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from . import access, auth, db, roles

log = logging.getLogger("o2d.photos")
router = APIRouter(prefix="/o2d", tags=["o2d-photos"])
ANY_VIEWER = access.require_page(*access.ORDER_PAGES)   # whoever can see the order lists can see its photos

MAX_BYTES = 8 * 1024 * 1024          # the screens shrink photos first; this only stops abuse
MAX_PER_KIND = 5                     # photos per order and kind
LINK_SECONDS = 600
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic"}
PDF_MODULES = {"decor", "challan"}   # modules whose documents (challans) may also be a PDF
# which O2D pages may attach / see each kind of photo
UPLOAD_PAGES = {"dispatch": ("o2d_shop_dispatch", "o2d_godown_dispatch"),
                "receiving": ("o2d_receiving", "o2d_shop_dispatch")}

_client = None


def configured() -> bool:
    return all(os.environ.get(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"))


def storage():
    """The S3 client for R2 (created once). Tests replace it with a fake via set_storage()."""
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"], aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto", config=Config(signature_version="s3v4", retries={"max_attempts": 3}))
    return _client


def set_storage(client):
    global _client
    _client = client


def _bucket() -> str:
    return os.environ["R2_BUCKET"]


def _visible_order(user, sl_no: int) -> dict:
    from . import main
    return main.get_order(sl_no, archived=None, user=user)  # 404 when missing or not theirs


def _may_upload(user, kind: str) -> bool:
    return bool(access.effective_pages(user) & set(UPLOAD_PAGES[kind]))


def save_photo(user, sl_no: int, kind: str, data: bytes, content_type: str) -> dict:
    order = _visible_order(user, sl_no)
    key = f"orders/{order['order_key']}/{kind}/{uuid.uuid4().hex}.{EXT[content_type]}"
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM order_attachments WHERE order_key = %s AND kind = %s",
                    (order["order_key"], kind))
        if cur.fetchone()["n"] >= MAX_PER_KIND:
            raise HTTPException(409, f"At most {MAX_PER_KIND} {kind} photos per order.")
    storage().put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=content_type)
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO order_attachments (order_key, kind, object_key, content_type, size_bytes, "
                "uploaded_by_key, uploaded_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING attachment_key",
                (order["order_key"], kind, key, content_type, len(data), user["user_key"],
                 user.get("display_name") or user["username"]))
            return {"id": cur.fetchone()["attachment_key"]}
    except Exception:
        try:  # don't leave an orphan object behind
            storage().delete_object(Bucket=_bucket(), Key=key)
        except Exception:
            log.exception("could not remove orphaned photo %s", key)
        raise


def photo_counts(order_keys: list[int]) -> dict[int, int]:
    """order_key -> how many photos it has (for the camera badge on the lists). Needs no storage access."""
    if not order_keys:
        return {}
    with db.cursor() as cur:
        cur.execute("SELECT order_key, count(*) AS n FROM order_attachments WHERE order_key = ANY(%s) GROUP BY order_key", (list(order_keys),))
        return {r["order_key"]: r["n"] for r in cur.fetchall()}


def list_photos(user, sl_no: int) -> list[dict]:
    order = _visible_order(user, sl_no)
    with db.cursor() as cur:
        cur.execute("SELECT attachment_key, kind, object_key, content_type, size_bytes, uploaded_by, uploaded_at "
                    "FROM order_attachments WHERE order_key = %s ORDER BY uploaded_at", (order["order_key"],))
        rows = cur.fetchall()
    out = []
    for r in rows:
        url = storage().generate_presigned_url(
            "get_object", Params={"Bucket": _bucket(), "Key": r["object_key"]}, ExpiresIn=LINK_SECONDS)
        out.append({"id": r["attachment_key"], "kind": r["kind"], "url": url, "uploadedBy": r["uploaded_by"] or "",
                    "uploadedAt": r["uploaded_at"].isoformat(), "size": r["size_bytes"]})
    return out


@router.post("/orders/{sl_no}/photos")
async def upload_photo(sl_no: int, request: Request, kind: str = Query(pattern="^(dispatch|receiving)$"),
                       user=Depends(ANY_VIEWER)):
    """Body is the raw image (Content-Type image/jpeg|png|webp|heic) - no multipart needed."""
    if not configured():
        raise HTTPException(503, "Photo storage is not set up yet. Ask the administrator.")
    if not _may_upload(user, kind):
        raise HTTPException(403, access.NO_ACCESS_MESSAGE)
    access.require_edit(user, *UPLOAD_PAGES[kind])
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in EXT:
        raise HTTPException(415, "Photo must be a JPEG, PNG, WebP or HEIC image.")
    if int(request.headers.get("content-length") or 0) > MAX_BYTES:
        raise HTTPException(413, "Photo is too large (8 MB maximum).")
    data = await request.body()
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413 if data else 422, "Photo is too large (8 MB maximum)." if data else "Empty photo.")
    res = await run_in_threadpool(save_photo, user, sl_no, kind, data, ctype)
    return {"ok": True, "success": True, **res}


@router.post("/bulk/photos")
async def upload_bulk_photo(request: Request, kind: str = Query(pattern="^(dispatch|receiving)$"),
                            sl_nos: str = Query(max_length=1000), user=Depends(ANY_VIEWER)):
    """The bulk "Mark done": one raw image body saved on every listed order (`sl_nos` = 12,15,31) - one attachment row (and one stored object)
    per order, exactly as if it had been added on each order's own screen. Orders that cannot take it (not visible, already 5 photos) are
    listed in `failed`; the others are saved."""
    if not configured():
        raise HTTPException(503, "Photo storage is not set up yet. Ask the administrator.")
    if not _may_upload(user, kind):
        raise HTTPException(403, access.NO_ACCESS_MESSAGE)
    access.require_edit(user, *UPLOAD_PAGES[kind])
    try:
        slots = list(dict.fromkeys(int(x) for x in sl_nos.split(",") if x.strip()))
    except ValueError:
        raise HTTPException(422, "sl_nos must be order numbers separated by commas.")
    if not slots or len(slots) > 100:
        raise HTTPException(422, "Choose between 1 and 100 orders.")
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in EXT:
        raise HTTPException(415, "Photo must be a JPEG, PNG, WebP or HEIC image.")
    data = await request.body()
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413 if data else 422, "Photo is too large (8 MB maximum)." if data else "Empty photo.")

    def run():
        failed = []
        for sl in slots:
            try:
                save_photo(user, sl, kind, data, ctype)
            except HTTPException as e:
                failed.append({"slNo": sl, "message": str(e.detail)})
        return failed
    failed = await run_in_threadpool(run)
    msg = "; ".join(f"Sl {f['slNo']}: {f['message']}" for f in failed[:5])
    return {"ok": True, "success": not failed, "saved": len(slots) - len(failed), "failed": failed, "message": msg}


@router.get("/orders/{sl_no}/photos")
def order_photos(sl_no: int, user=Depends(ANY_VIEWER)):
    if not configured():
        return {"ok": True, "enabled": False, "photos": []}
    return {"ok": True, "enabled": True, "photos": list_photos(user, sl_no)}


# ------------------------------------------------------------------ generic files for the other modules
# (Delegation task proofs, Purchase invoice photos, ...). Same storage, same signed links, but any module/record.
def put_file(module: str, record_key: int, kind: str, user, data: bytes, content_type: str) -> int:
    ext = EXT.get(content_type) or ("pdf" if content_type == "application/pdf" and module in PDF_MODULES else None)
    if not ext:
        raise HTTPException(415, "Photo must be a JPEG, PNG, WebP or HEIC image." + (" (A PDF is also fine here.)" if module in PDF_MODULES else ""))
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "Photo is too large (8 MB maximum)." if data else "Empty photo.")
    key = f"{module}/{record_key}/{uuid.uuid4().hex}.{ext}"
    storage().put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=content_type)
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO file_attachment (module, record_key, kind, object_key, content_type, size_bytes, uploaded_by_key) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING attachment_key",
                        (module, record_key, kind, key, content_type, len(data), user["user_key"]))
            return cur.fetchone()["attachment_key"]
    except Exception:
        try:
            storage().delete_object(Bucket=_bucket(), Key=key)
        except Exception:
            log.exception("could not remove orphaned file %s", key)
        raise


def put_data_url(module: str, record_key: int, kind: str, user, data_url: str) -> int:
    """A browser 'data:image/jpeg;base64,...' string (what the old screens sent) -> stored file."""
    import base64
    import re
    m = re.match(r"^data:([\w/+.-]+);base64,(.*)$", data_url or "", re.S)
    if not m:
        raise HTTPException(422, "That is not a photo.")
    try:
        data = base64.b64decode(m.group(2), validate=False)
    except Exception:
        raise HTTPException(422, "That is not a photo.")
    return put_file(module, record_key, kind, user, data, m.group(1).lower())


def file_urls(module: str, record_keys: list[int], kind: str | None = None) -> dict[int, list[str]]:
    """record_key -> signed links (newest last). Empty when storage is not configured."""
    if not record_keys or not configured():
        return {}
    with db.cursor() as cur:
        cur.execute("SELECT record_key, object_key FROM file_attachment WHERE module = %s AND record_key = ANY(%s) "
                    "AND (%s::text IS NULL OR kind = %s) ORDER BY attachment_key", (module, list(record_keys), kind, kind))
        rows = cur.fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        url = storage().generate_presigned_url("get_object", Params={"Bucket": _bucket(), "Key": r["object_key"]}, ExpiresIn=LINK_SECONDS)
        out.setdefault(r["record_key"], []).append(url)
    return out
