"""Admin (commissioner) auth.

Two flavors share one secret:
- `require_admin` — header `X-Auth-Token` == SYNC_AUTH_TOKEN, for the cron and
  programmatic JSON `/admin` endpoints.
- session login (`check_admin_password` + `is_admin`) — a signed cookie for the
  browser UI, so the commissioner logs in once and the edit controls appear.

Login accepts ADMIN_PASSWORD if set, else falls back to SYNC_AUTH_TOKEN so it
works immediately with the existing secret.
"""

import os

from fastapi import Header, HTTPException, Request


def require_admin(x_auth_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("SYNC_AUTH_TOKEN")
    if not expected or x_auth_token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def check_admin_password(password: str) -> bool:
    expected = os.getenv("ADMIN_PASSWORD") or os.getenv("SYNC_AUTH_TOKEN")
    return bool(expected) and password == expected


def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin"))


def require_admin_session(request: Request) -> None:
    """Guard for UI write routes. 403 if not logged in (the route can catch and
    redirect to /admin/login)."""
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="login required")
