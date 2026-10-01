"""Page access: who may open which page, how people ask for access, and how admins decide.

A page is anything an admin can switch on or off for someone. Admin always opens every page, and Setup is
admin-only (never listed here). Everyone else opens a page if their ROLE has it by default (page_access)
unless a PERSON-level exception (user_page_access) says otherwise: allowed = true grants it, false blocks it.

Opening a page is not the same as editing through it: what a person may see and change inside the O2D
screens still follows their role's rules (roles.py and Setup > Permissions).
"""
from fastapi import APIRouter, Depends, HTTPException
from psycopg2.extras import Json
from pydantic import BaseModel, ConfigDict, Field

from . import auth, db, roles

# key -> (group, label). The group is the module it lives under on the home screen.
PAGES: dict[str, tuple[str, str]] = {
    "dashboard_overview": ("Dashboard", "Dashboard - Overview"),
    "dashboard_orders": ("Dashboard", "Dashboard - All orders"),
    "o2d_overview": ("O2D Portal", "O2D - Overview board"),
    "o2d_shop": ("O2D Portal", "O2D - Shop screen"),
    "o2d_godown": ("O2D Portal", "O2D - Godown screen"),
    "o2d_shop_dispatch": ("O2D Portal", "O2D - Shop dispatch screen"),
    "o2d_godown_dispatch": ("O2D Portal", "O2D - Godown dispatch screen"),
    "o2d_receiving": ("O2D Portal", "O2D - Receiving screen"),
}
DASHBOARD_PAGES = ("dashboard_overview", "dashboard_orders")
O2D_PAGES = tuple(k for k in PAGES if k.startswith("o2d_"))
# Roles that can be given pages in the matrix (admin has everything, legacy cannot sign in).
MATRIX_ROLES = sorted(roles.ALL_ROLES - {"admin", "legacy"})

NO_ACCESS_MESSAGE = "Access not provided for this page. Use “Request access” on the home screen to ask an admin."


def page_list() -> list[dict]:
    return [{"key": k, "group": g, "label": lbl} for k, (g, lbl) in PAGES.items()]


def effective_pages(user) -> set[str]:
    if user["role"] == "admin":
        return set(PAGES)
    with db.cursor() as cur:
        cur.execute("SELECT page_key FROM page_access WHERE role = %s", (user["role"],))
        pages = {r["page_key"] for r in cur.fetchall()}
        cur.execute("SELECT page_key, allowed FROM user_page_access WHERE user_key = %s", (user["user_key"],))
        for r in cur.fetchall():
            (pages.add if r["allowed"] else pages.discard)(r["page_key"])
    return pages & set(PAGES)


def require_page(*page_keys: str):
    """Dependency: signed in, password change done, and allowed to open at least one of page_keys."""
    wanted = set(page_keys)
    unknown = wanted - set(PAGES)
    if unknown:
        raise RuntimeError(f"unknown page key(s): {sorted(unknown)}")

    def dep(user=Depends(auth.current_user)):
        if user["must_change_password"]:
            raise HTTPException(403, "Password change required. POST /auth/change-password first.")
        if not (effective_pages(user) & wanted):
            raise HTTPException(403, NO_ACCESS_MESSAGE)
        return user
    return dep


ANY_LOGGED_IN = auth.require_roles(*roles.ALL_ROLES)
ADMIN = auth.require_roles("admin")


def _audit(cur, admin, action, target=None, details=None):
    cur.execute("INSERT INTO admin_audit_log (admin_user_key, admin_username, action, target, details) "
                "VALUES (%s, %s, %s, %s, %s)",
                (admin["user_key"], admin["username"], action, target, Json(details) if details else None))


# ------------------------------------------------------------------ for every signed-in person
public_router = APIRouter(prefix="/access", tags=["access"])


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_key: str
    reason: str = Field(min_length=5, max_length=500)


@public_router.get("/pages")
def my_pages(user=Depends(auth.current_user)):
    """What I can open, every page that exists, and my own requests (newest first)."""
    mine = effective_pages(user)
    with db.cursor() as cur:
        cur.execute("SELECT request_key, page_key, reason, status, created_at, decided_at, decision_note "
                    "FROM access_request WHERE user_key = %s ORDER BY created_at DESC LIMIT 20", (user["user_key"],))
        requests = cur.fetchall()
    return {"pages": sorted(mine), "all_pages": page_list(), "requests": requests}


@public_router.post("/request", status_code=201)
def request_access(body: RequestIn, user=Depends(auth.current_user)):
    if body.page_key not in PAGES:
        raise HTTPException(422, "Unknown page.")
    if body.page_key in effective_pages(user):
        raise HTTPException(409, "You already have access to that page.")
    reason = " ".join(body.reason.split())
    if len(reason) < 5:
        raise HTTPException(422, "Please say briefly why you need access.")
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM access_request WHERE user_key = %s AND page_key = %s AND status = 'pending'",
                    (user["user_key"], body.page_key))
        if cur.fetchone():
            raise HTTPException(409, "You already have a request waiting for that page.")
        cur.execute("INSERT INTO access_request (user_key, page_key, reason) VALUES (%s, %s, %s) RETURNING request_key",
                    (user["user_key"], body.page_key, reason))
        return {"ok": True, "request_key": cur.fetchone()["request_key"]}


# ------------------------------------------------------------------ admin: matrix, per-person exceptions, requests
router = APIRouter(prefix="/admin/access", tags=["access-admin"])


@router.get("/matrix")
def get_matrix(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        cur.execute("SELECT page_key, role FROM page_access")
        granted = [{"page_key": r["page_key"], "role": r["role"]} for r in cur.fetchall()]
    return {"pages": page_list(), "roles": MATRIX_ROLES, "granted": granted}


class MatrixIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_key: str
    role: str
    allowed: bool


@router.put("/matrix")
def set_matrix(body: MatrixIn, admin=Depends(ADMIN)):
    if body.page_key not in PAGES:
        raise HTTPException(422, "Unknown page.")
    if body.role not in MATRIX_ROLES:
        raise HTTPException(422, "Unknown role.")
    with db.cursor() as cur:
        if body.allowed:
            cur.execute("INSERT INTO page_access (page_key, role) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (body.page_key, body.role))
        else:
            cur.execute("DELETE FROM page_access WHERE page_key = %s AND role = %s", (body.page_key, body.role))
        _audit(cur, admin, "access.role", f"{body.role}:{body.page_key}", {"allowed": body.allowed})
    return {"ok": True}


@router.get("/users/{user_key}")
def get_user_access(user_key: int, admin=Depends(ADMIN)):
    with db.cursor() as cur:
        cur.execute("SELECT user_key, username, display_name, role FROM dim_user WHERE user_key = %s", (user_key,))
        person = cur.fetchone()
        if not person:
            raise HTTPException(404, "Member not found")
        cur.execute("SELECT page_key, allowed FROM user_page_access WHERE user_key = %s", (user_key,))
        overrides = {r["page_key"]: r["allowed"] for r in cur.fetchall()}
        cur.execute("SELECT page_key FROM page_access WHERE role = %s", (person["role"],))
        role_pages = {r["page_key"] for r in cur.fetchall()}
    return {"user": person, "pages": [
        {**p, "role_default": p["key"] in role_pages or person["role"] == "admin",
         "override": overrides.get(p["key"])} for p in page_list()]}


class UserAccessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_key: str
    allowed: bool | None  # true = grant, false = block, null = back to the role's default


@router.put("/users/{user_key}")
def set_user_access(user_key: int, body: UserAccessIn, admin=Depends(ADMIN)):
    if body.page_key not in PAGES:
        raise HTTPException(422, "Unknown page.")
    with db.cursor() as cur:
        cur.execute("SELECT username FROM dim_user WHERE user_key = %s", (user_key,))
        person = cur.fetchone()
        if not person:
            raise HTTPException(404, "Member not found")
        if body.allowed is None:
            cur.execute("DELETE FROM user_page_access WHERE user_key = %s AND page_key = %s", (user_key, body.page_key))
        else:
            cur.execute("INSERT INTO user_page_access (user_key, page_key, allowed, set_by_user_key) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (user_key, page_key) DO UPDATE "
                        "SET allowed = EXCLUDED.allowed, set_by_user_key = EXCLUDED.set_by_user_key, set_at = now()",
                        (user_key, body.page_key, body.allowed, admin["user_key"]))
        _audit(cur, admin, "access.user", f"{person['username']}:{body.page_key}", {"allowed": body.allowed})
    return {"ok": True}


@router.get("/requests")
def list_requests(status: str = "pending", admin=Depends(ADMIN)):
    if status not in ("pending", "approved", "rejected", "all"):
        raise HTTPException(422, "status must be pending, approved, rejected or all")
    with db.cursor() as cur:
        cur.execute(
            "SELECT r.request_key, r.page_key, r.reason, r.status, r.created_at, r.decided_at, r.decision_note, "
            "       u.user_key, u.username, u.display_name, u.role, a.display_name AS decided_by "
            "FROM access_request r JOIN dim_user u ON u.user_key = r.user_key "
            "LEFT JOIN dim_user a ON a.user_key = r.decided_by_user_key "
            "WHERE (%s = 'all' OR r.status = %s) ORDER BY r.created_at DESC LIMIT 200", (status, status))
        rows = cur.fetchall()
    for r in rows:
        r["page_label"] = PAGES.get(r["page_key"], ("", r["page_key"]))[1]
    return rows


@router.get("/requests/count")
def pending_count(admin=Depends(ADMIN)):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM access_request WHERE status = 'pending'")
        return {"pending": cur.fetchone()["n"]}


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=300)


def _decide(request_key: int, admin, approve: bool, note: str | None):
    with db.cursor() as cur:
        cur.execute("SELECT r.*, u.username FROM access_request r JOIN dim_user u ON u.user_key = r.user_key "
                    "WHERE r.request_key = %s FOR UPDATE OF r", (request_key,))
        req = cur.fetchone()
        if not req:
            raise HTTPException(404, "Request not found")
        if req["status"] != "pending":
            raise HTTPException(409, f"Already {req['status']}.")
        cur.execute("UPDATE access_request SET status = %s, decided_by_user_key = %s, decided_at = now(), "
                    "decision_note = %s WHERE request_key = %s",
                    ("approved" if approve else "rejected", admin["user_key"],
                     (note or "").strip() or None, request_key))
        if approve:  # the person gets the page; their colleagues in the same role do not
            cur.execute("INSERT INTO user_page_access (user_key, page_key, allowed, set_by_user_key) "
                        "VALUES (%s, %s, TRUE, %s) ON CONFLICT (user_key, page_key) DO UPDATE "
                        "SET allowed = TRUE, set_by_user_key = EXCLUDED.set_by_user_key, set_at = now()",
                        (req["user_key"], req["page_key"], admin["user_key"]))
        action = "access.approve" if approve else "access.reject"
        _audit(cur, admin, action, f"{req['username']}:{req['page_key']}",
               {"request_key": request_key, "reason": req["reason"], "note": note})
    return {"ok": True}


@router.post("/requests/{request_key}/approve")
def approve_request(request_key: int, body: DecisionIn | None = None, admin=Depends(ADMIN)):
    return _decide(request_key, admin, True, body.note if body else None)


@router.post("/requests/{request_key}/reject")
def reject_request(request_key: int, body: DecisionIn | None = None, admin=Depends(ADMIN)):
    return _decide(request_key, admin, False, body.note if body else None)
