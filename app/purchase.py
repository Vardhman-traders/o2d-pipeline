"""Purchase report module: what was bought from which vendor, with the invoice and photos of the bill / material.
Two sites keep their own lists (Godown and Shop); an admin sees both together and may delete.

How it works:
  - an entry has a permanent id, so a delete can never hit a different entry after others are removed;
  - vendors are the shared party list (Setup > Dropdown values > Vendors); a vendor typed for the first time joins it and waits
    there for an admin to review it;
  - who sees Godown, Shop or everything is page access (Setup > Access); deleting is checked on the server;
  - photos go to private storage with short-lived links.
"""
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import errors as pgerr
from psycopg2.extras import Json
from pydantic import BaseModel, ConfigDict

from . import access, attachments, auth, db, modcommon as mc, roles

router = APIRouter(prefix="/purchase", tags=["purchase"])
ANY = auth.require_roles(*roles.ALL_ROLES)
SITE_PAGE = {"godown": "po_godown", "shop": "po_shop"}

SELECT = """
SELECT e.purchase_key, s.site_slug, p.party_name AS vendor, md.full_date AS material_date, idt.full_date AS invoice_date,
       e.invoice_no, e.invoice_amount, e.remarks
FROM fact_purchase_entry e
JOIN dim_purchase_site s ON s.site_key = e.site_key
JOIN dim_party p ON p.party_key = e.vendor_party_key
LEFT JOIN dim_date md ON md.date_key = e.material_received_date_key
LEFT JOIN dim_date idt ON idt.date_key = e.invoice_date_key
"""


def _dashboard(user) -> str:
    """'admin' (everything), 'godown' or 'shop' - what the old login screen decided from the username."""
    if user["role"] == "admin":
        return "admin"
    held = access.effective_access(user)
    if "po_all" in held:
        return "admin"
    for site, page in SITE_PAGE.items():
        if page in held:
            return site
    raise HTTPException(403, access.NO_ACCESS_MESSAGE)


def _gate(user) -> str:
    if user.get("must_change_password"):
        raise HTTPException(403, "Password change required. POST /auth/change-password first.")
    return _dashboard(user)


def _site_allowed(user, dash: str, site: str):
    if site not in SITE_PAGE:
        raise HTTPException(422, "Unknown site.")
    if dash != "admin" and dash != site:
        raise HTTPException(403, "You do not have access to that list.")


def _write_gate(user, site: str):
    held = access.effective_access(user)
    pages = {"po_all", SITE_PAGE[site]} if user["role"] != "admin" else set()
    if pages and set(held) & pages:
        access.require_edit(user, *(pages & set(held)))
    # admin: never view-only


def _photo_refs(photos: str) -> tuple[list[int], list[str]]:
    """The 'photos' text a form sends: attachments just uploaded ('vtfile:<id>') and links already on the entry."""
    ids, keys = [], []
    for part in (photos or "").split(" | "):
        part = part.strip()
        if m := re.fullmatch(r"vtfile:(\d+)", part):
            ids.append(int(m.group(1)))
        elif part.startswith("http"):
            path = urlparse(part).path.lstrip("/")
            bucket = attachments._bucket() + "/" if attachments.configured() else ""
            keys.append(path[len(bucket):] if bucket and path.startswith(bucket) else path)
    return ids, keys


def _link_photos(cur, purchase_key: int, photos: str, user):
    """Attach the new photos to the entry and drop the ones the person removed in the form."""
    ids, keys = _photo_refs(photos)
    if ids:
        cur.execute("UPDATE file_attachment SET record_key = %s WHERE module = 'purchase' AND record_key = 0 "
                    "AND attachment_key = ANY(%s) AND uploaded_by_key = %s", (purchase_key, ids, user["user_key"]))
    cur.execute("SELECT attachment_key, object_key FROM file_attachment WHERE module = 'purchase' AND record_key = %s", (purchase_key,))
    for r in cur.fetchall():
        if r["attachment_key"] in ids or r["object_key"] in keys:
            continue
        cur.execute("DELETE FROM file_attachment WHERE attachment_key = %s", (r["attachment_key"],))
        try:
            if attachments.configured():
                attachments.storage().delete_object(Bucket=attachments._bucket(), Key=r["object_key"])
        except Exception:  # the row is gone either way; an orphaned object is harmless
            pass


def _entry(r: dict, photos: dict) -> dict:
    return {"rowId": r["purchase_key"], "source": r["site_slug"], "uid": f"{r['site_slug']}_{r['purchase_key']}",
            "vendorName": r["vendor"], "materialReceivedDate": mc.iso(r["material_date"]) or "",
            "invoiceDate": mc.iso(r["invoice_date"]) or "", "invoiceNumber": r["invoice_no"] or "",
            "invoiceAmount": float(r["invoice_amount"]) if r["invoice_amount"] is not None else "",
            "remarks": r["remarks"] or "", "photos": " | ".join(photos.get(r["purchase_key"], []))}


# ------------------------------------------------------------------ reading
@router.get("/me")
def me(user=Depends(ANY)):
    dash = _gate(user)
    return {"ok": True, "success": True, "dashboard": dash, "username": user["username"], "fullName": user["display_name"],
            "photosEnabled": attachments.configured()}


@router.get("/vendors")
def vendors(user=Depends(ANY)):
    _gate(user)
    with db.cursor() as cur:
        cur.execute("SELECT party_name FROM dim_party WHERE party_kind = 'vendor' ORDER BY lower(party_name)")
        return [r["party_name"] for r in cur.fetchall()]


@router.get("/entries")
def entries(site: str = "godown", user=Depends(ANY)):
    dash = _gate(user)
    _site_allowed(user, dash, site)
    with db.cursor() as cur:
        cur.execute(SELECT + " WHERE s.site_slug = %s ORDER BY e.purchase_key DESC LIMIT 5000", (site,))
        rows = cur.fetchall()
    photos = attachments.file_urls("purchase", [r["purchase_key"] for r in rows])
    return [_entry(r, photos) for r in rows]


# ------------------------------------------------------------------ writing
class EntryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    vendorName: str = ""
    materialReceivedDate: str = ""
    invoiceDate: str = ""
    invoiceNumber: str = ""
    invoiceAmount: float | str = ""
    remarks: str = ""
    photos: str = ""


def _clean(body: EntryIn, cur, user) -> dict:
    vendor = " ".join(body.vendorName.split())
    if not vendor:
        raise HTTPException(422, "Vendor name is required.")
    amount = None
    if str(body.invoiceAmount).strip() != "":
        try:
            amount = round(float(body.invoiceAmount), 2)
        except ValueError:
            raise HTTPException(422, "Invoice amount must be a number.")
        if amount < 0:
            raise HTTPException(422, "Invoice amount cannot be negative.")
    cur.execute("SELECT party_key FROM dim_party WHERE party_kind = 'vendor' AND lower(btrim(party_name)) = lower(btrim(%s)) "
                "AND company_key IS NULL AND txn_type_key IS NULL", (vendor,))
    row = cur.fetchone()
    if row:
        vendor_key = row["party_key"]
    else:   # a vendor typed for the first time joins the list, as before
        cur.execute("INSERT INTO dim_party (party_name, party_kind, review_status, created_by_user_key) VALUES (%s, 'vendor', 'pending', %s) "
                    "RETURNING party_key", (vendor, user["user_key"]))
        vendor_key = cur.fetchone()["party_key"]
    return {"vendor_key": vendor_key, "material": mc.date_key(mc.parse_date(body.materialReceivedDate, "Material received date")),
            "invoice_date": mc.date_key(mc.parse_date(body.invoiceDate, "Invoice date")),
            "invoice_no": body.invoiceNumber.strip()[:80] or None, "amount": amount, "remarks": body.remarks.strip() or None}


@router.post("/entries/{site}")
def save_entry(site: str, body: EntryIn, user=Depends(ANY)):
    dash = _gate(user)
    _site_allowed(user, dash, site)
    _write_gate(user, site)
    try:
        with db.cursor() as cur:
            c = _clean(body, cur, user)
            cur.execute("INSERT INTO fact_purchase_entry (site_key, vendor_party_key, material_received_date_key, invoice_date_key, "
                        "invoice_no, invoice_amount, remarks, created_by_user_key) "
                        "SELECT site_key, %s, %s, %s, %s, %s, %s, %s FROM dim_purchase_site WHERE site_slug = %s RETURNING purchase_key",
                        (c["vendor_key"], c["material"], c["invoice_date"], c["invoice_no"], c["amount"], c["remarks"], user["user_key"], site))
            key = cur.fetchone()["purchase_key"]
            _link_photos(cur, key, body.photos, user)
    except HTTPException as e:
        return {"success": False, "message": e.detail}
    except pgerr.ForeignKeyViolation as e:
        return {"success": False, "message": mc.date_fk_error(e).detail}
    return {"success": True, "rowId": key}


@router.put("/entries/{site}/{purchase_key}")
def update_entry(site: str, purchase_key: int, body: EntryIn, user=Depends(ANY)):
    dash = _gate(user)
    _site_allowed(user, dash, site)
    _write_gate(user, site)
    try:
        with db.cursor() as cur:
            c = _clean(body, cur, user)
            cur.execute("UPDATE fact_purchase_entry e SET vendor_party_key = %s, material_received_date_key = %s, invoice_date_key = %s, "
                        "invoice_no = %s, invoice_amount = %s, remarks = %s, updated_by_user_key = %s, updated_at = now() "
                        "FROM dim_purchase_site s WHERE e.purchase_key = %s AND s.site_key = e.site_key AND s.site_slug = %s "
                        "RETURNING e.purchase_key",
                        (c["vendor_key"], c["material"], c["invoice_date"], c["invoice_no"], c["amount"], c["remarks"],
                         user["user_key"], purchase_key, site))
            if not cur.fetchone():
                return {"success": False, "message": "Entry not found — it may have been removed."}
            _link_photos(cur, purchase_key, body.photos, user)
    except HTTPException as e:
        return {"success": False, "message": e.detail}
    except pgerr.ForeignKeyViolation as e:
        return {"success": False, "message": mc.date_fk_error(e).detail}
    return {"success": True}


@router.delete("/entries/{site}/{purchase_key}")
def delete_entry(site: str, purchase_key: int, user=Depends(ANY)):
    """Admin (or whoever holds the 'all entries' page) only. The row goes; a note of what it was stays in the audit log."""
    dash = _gate(user)
    if dash != "admin":
        return {"success": False, "message": "Not authorized."}
    if user["role"] != "admin":
        access.require_edit(user, "po_all")
    with db.cursor() as cur:
        cur.execute(SELECT + " WHERE e.purchase_key = %s AND s.site_slug = %s", (purchase_key, site))
        row = cur.fetchone()
        if not row:
            return {"success": False, "message": "Entry not found — it may have been removed."}
        cur.execute("INSERT INTO admin_audit_log (admin_user_key, admin_username, action, target, details) VALUES (%s, %s, 'purchase.delete', %s, %s)",
                    (user["user_key"], user["username"], str(purchase_key),
                     Json({"vendor": row["vendor"], "invoice": row["invoice_no"],
                          "amount": float(row["invoice_amount"]) if row["invoice_amount"] is not None else None})))
        cur.execute("DELETE FROM file_attachment WHERE module = 'purchase' AND record_key = %s", (purchase_key,))
        cur.execute("DELETE FROM fact_purchase_entry WHERE purchase_key = %s", (purchase_key,))
    return {"success": True}


class UploadIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: str = ""   # data:image/jpeg;base64,...


@router.post("/photos/{site}")
def upload_photo(site: str, body: UploadIn, user=Depends(ANY)):
    """Keep one photo before the entry is saved; the screen puts the returned 'vtfile:<id>' in the entry's photos."""
    dash = _gate(user)
    _site_allowed(user, dash, site)
    _write_gate(user, site)
    if not attachments.configured():
        return {"success": False, "message": "Photo storage is not set up yet. Ask the administrator."}
    try:
        key = attachments.put_data_url("purchase", 0, "photo", user, body.data)
    except HTTPException as e:
        return {"success": False, "message": e.detail}
    return {"success": True, "url": f"vtfile:{key}"}
