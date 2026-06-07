"""Admin (commissioner) auth. Reused by every write endpoint.

For now the commissioner secret is the same SYNC_AUTH_TOKEN used by /admin/sync;
split into a separate token here if commissioner access ever needs to differ.
"""

import os

from fastapi import Header, HTTPException


def require_admin(x_auth_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("SYNC_AUTH_TOKEN")
    if not expected or x_auth_token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
