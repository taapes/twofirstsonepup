"""Shared Jinja2 templates instance (used by main.py and ui.py)."""

import os

from fastapi.templating import Jinja2Templates
from starlette.requests import Request


def _identity(request: Request) -> dict:
    """Inject the logged-in identity into every template so the nav (and any page)
    can show who you are without each route passing it. Read purely from the signed
    session — no DB hit."""
    try:
        session = request.session
    except (AssertionError, AttributeError):
        session = {}
    return {
        "is_admin": bool(session.get("admin")),
        "current_fpl": session.get("manager_id"),
        "current_name": session.get("manager_name"),
    }


def _phase(request: Request) -> dict:
    """Inject the current league phase + feature flags into every template so the nav
    can show/hide features by phase. Read-only DB lookup; degrades to None on any
    error so rendering never breaks."""
    try:
        from db import SessionLocal
        import services

        with SessionLocal() as db:
            league = services.current_league(db)
            return {"phase": services.phase_context(db, league) if league else None}
    except Exception:
        return {"phase": None}


templates = Jinja2Templates(directory="templates", context_processors=[_identity, _phase])
# Escape HTML by default (defense-in-depth against XSS; explicit so a config change
# can't silently disable it).
templates.env.autoescape = True
# APP_ENV (e.g. "test" on a Neon test branch) drives a banner in base.html so a
# test environment is never mistaken for production.
templates.env.globals["app_env"] = os.getenv("APP_ENV", "prod")
