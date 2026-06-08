"""Auth: commissioner (admin) + per-manager identity.

Admin flavors share one secret:
- `require_admin` — header `X-Auth-Token` == SYNC_AUTH_TOKEN, for the cron and
  programmatic JSON `/admin` endpoints.
- session login (`check_admin_password` + `is_admin`) — a signed cookie for the
  browser UI; admin acts as a bypass for all per-manager checks.

Per-manager identity: a visitor logs in as themselves (`session["manager_id"]` =
their `fpl_manager_id`) with a per-manager password. Edits are then scoped to
their own team via `can_act_as`. Passwords are hashed with stdlib PBKDF2 (no new
deps) and stored on `managers.password_hash`; NULL = first-time set flow.

Login accepts ADMIN_PASSWORD if set, else falls back to SYNC_AUTH_TOKEN so it
works immediately with the existing secret.
"""

import hashlib
import hmac
import os
import secrets

from fastapi import Header, HTTPException, Request

_PBKDF2_ITERATIONS = 240_000


def require_admin(x_auth_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("SYNC_AUTH_TOKEN")
    if not expected or not x_auth_token or not hmac.compare_digest(x_auth_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def check_admin_password(password: str) -> bool:
    expected = os.getenv("ADMIN_PASSWORD") or os.getenv("SYNC_AUTH_TOKEN")
    return bool(expected) and hmac.compare_digest(password, expected)


def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin"))


def is_demo() -> bool:
    """Demo sandbox mode (APP_ENV=demo): enables passwordless 'pick any manager'
    login. Never true in prod, so the live site always requires a password."""
    return os.getenv("APP_ENV", "prod") == "demo"


def require_admin_session(request: Request) -> None:
    """Guard for UI write routes. 403 if not logged in (the route can catch and
    redirect to /admin/login)."""
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="login required")


# ---- per-manager passwords (stdlib PBKDF2, no extra deps) ----
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _algo, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---- per-manager session identity ----
def current_manager_id(request: Request) -> str | None:
    return request.session.get("manager_id")


def is_logged_in(request: Request) -> bool:
    """Passes the gate: a manager identity OR admin (keeps admin-only sessions valid)."""
    return bool(request.session.get("manager_id")) or is_admin(request)


def can_act_as(request: Request, *fpl_manager_ids: str | None) -> bool:
    """True if the request may write on behalf of any of the given managers. Admin
    bypasses; otherwise the logged-in manager must be one of them."""
    if is_admin(request):
        return True
    me = current_manager_id(request)
    return bool(me) and me in {str(x) for x in fpl_manager_ids if x is not None}
