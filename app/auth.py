"""Username/password login (bcrypt hashes in dim_user) issuing short-lived JWTs."""
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db

_bearer = HTTPBearer(auto_error=False)
ALGORITHM = "HS256"
DISABLED_HASH = "!"  # password_hash value meaning "login disabled"

# ---- brute-force protection (in-memory: per process; fine for a single small instance)
MAX_FAILURES, LOCK_SECONDS = 5, 15 * 60
_fails: dict = {}
_fails_lock = threading.Lock()


def _keys(username, ip):
    return [("u", username.strip().lower()), ("ip", ip or "?")]


def check_not_locked(username, ip):
    now = time.time()
    with _fails_lock:
        for k in _keys(username, ip):
            count, first = _fails.get(k, (0, now))
            if count >= MAX_FAILURES and now - first < LOCK_SECONDS:
                raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
            if now - first >= LOCK_SECONDS:
                _fails.pop(k, None)


def record_failure(username, ip):
    now = time.time()
    with _fails_lock:
        for k in _keys(username, ip):
            count, first = _fails.get(k, (0, now))
            _fails[k] = (count + 1, first if count else now)


def clear_failures(username, ip):
    with _fails_lock:
        for k in _keys(username, ip):
            _fails.pop(k, None)


# ---- tokens
def _secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET must be set to a random string of at least 32 characters")
    return secret


def check_config():
    _secret()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except ValueError:  # '!' (disabled) or malformed hash
        return False


def authenticate(username: str, password: str):
    with db.cursor() as cur:
        cur.execute(
            "SELECT user_key, username, password_hash, role, display_name, must_change_password "
            "FROM dim_user WHERE lower(username) = lower(%s)", (username.strip(),))
        user = cur.fetchone()
    # Always run a bcrypt check so response time doesn't reveal whether the user exists.
    stored = user["password_hash"] if user else "$2b$12$" + "." * 53
    ok = verify_password(password, stored)
    return user if (user and ok) else None


def make_token(user) -> str:
    hours = int(os.environ.get("TOKEN_HOURS", "12"))
    payload = {"sub": str(user["user_key"]), "exp": datetime.now(timezone.utc) + timedelta(hours=hours)}
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


# ---- request dependencies
def current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    """Valid token + user still exists and is not disabled. Role etc. are re-read on every request."""
    if not creds:
        raise HTTPException(401, "Missing bearer token")
    try:
        payload = jwt.decode(creds.credentials, _secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    with db.cursor() as cur:
        cur.execute("SELECT user_key, username, role, display_name, must_change_password, password_hash "
                    "FROM dim_user WHERE user_key = %s", (int(payload["sub"]),))
        user = cur.fetchone()
    if not user or user["password_hash"] == DISABLED_HASH:
        raise HTTPException(401, "Unknown or disabled user")
    user.pop("password_hash")
    return user


# ---- single sign-on tickets (portal -> Apps Script hand-off, no token in browser history)
# A ticket only gets minted for someone who already holds a valid JWT, is good for one
# exchange, and expires fast — it is not a second, longer-lived credential.
SSO_TICKET_SECONDS = 90
_tickets: dict = {}
_tickets_lock = threading.Lock()


def _prune_tickets(now):
    for t, (_, expires) in list(_tickets.items()):
        if now >= expires:
            _tickets.pop(t, None)


def create_sso_ticket(user_key: int) -> str:
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with _tickets_lock:
        _prune_tickets(now)
        _tickets[ticket] = (user_key, now + SSO_TICKET_SECONDS)
    return ticket


def redeem_sso_ticket(ticket: str):
    """Single use: valid once, then gone, regardless of outcome."""
    now = time.time()
    with _tickets_lock:
        _prune_tickets(now)
        entry = _tickets.pop(ticket, None)
    if not entry:
        return None
    user_key, expires = entry
    if now >= expires:
        return None
    return user_key


# ---- Apps Script client key: proves the caller is the one authorized Apps Script
# deployment, not a copy-pasted duplicate. A copy of Code.gs's *text* does not carry
# this over - Script Properties live per Apps Script project, so a duplicate has to be
# deliberately configured with the real secret before it can touch order data.
def require_client_key(request: Request):
    expected = os.environ.get("APPS_SCRIPT_CLIENT_KEY")
    if not expected:
        return  # not configured: leave order endpoints open (e.g. local dev)
    got = request.headers.get("X-Client-Key", "")
    if not hmac.compare_digest(got, expected):
        raise HTTPException(401, "Invalid or missing client key")


def require_roles(*roles):
    """Dependency: user must have finished the forced password change and hold one of `roles`."""
    allowed = set(roles)

    def dep(user=Depends(current_user)):
        if user["must_change_password"]:
            raise HTTPException(403, "Password change required. POST /auth/change-password first.")
        if user["role"] not in allowed:
            raise HTTPException(403, "Your role is not allowed to do this")
        return user
    return dep
