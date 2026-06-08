import asyncio
import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

import services
from admin import router as admin_router
from api import router as v1_router
from auth import is_admin, is_logged_in, require_admin
from db import get_db
from settings import LEAGUE_ID
from sync import sync_all
from templating import templates
from ui import router as ui_router

# Paths viewable without an identity. Everything else (all HTML pages) is gated
# behind the "Who are you?" screen at /who.
_GATE_EXEMPT_PREFIXES = ("/v1", "/static")
_GATE_EXEMPT_PATHS = {
    "/health", "/who", "/login", "/logout", "/set-password",
    "/admin/login", "/admin/logout", "/favicon.ico",
}


class GateMiddleware(BaseHTTPMiddleware):
    """Hard gate: redirect un-identified browsers to /who. Exempts the JSON API,
    token-authenticated calls (cron + programmatic admin), static assets, and the
    login surface itself. HTMX requests get an HX-Redirect (full nav, not a swap)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        token = request.headers.get("X-Auth-Token")
        if (
            (token and token == os.getenv("SYNC_AUTH_TOKEN"))
            or path.startswith(_GATE_EXEMPT_PREFIXES)
            or path in _GATE_EXEMPT_PATHS
            or is_logged_in(request)
        ):
            return await call_next(request)
        if request.headers.get("HX-Request") == "true":
            resp = Response(status_code=204)
            resp.headers["HX-Redirect"] = "/who"
            return resp
        return RedirectResponse("/who", status_code=303)


app = FastAPI()
# Order matters: add the gate FIRST so SessionMiddleware wraps it (Starlette runs
# middleware in reverse add-order), guaranteeing request.session is populated when
# the gate reads it.
app.add_middleware(GateMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "dev-insecure-change-me"),
    https_only=False,
)
app.include_router(v1_router)
app.include_router(admin_router)
app.include_router(ui_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    """Minimal public homepage: standings, anti-tanking flags, injury list."""
    league = services.resolve_league(db, LEAGUE_ID) if LEAGUE_ID else None
    ctx = {"request": request, "league": league, "is_admin": is_admin(request)}
    if league:
        ctx.update(
            standings=services.get_standings(db, league),
            flags=services.get_flags(db, league),
            injury_list=services.get_injury_list(db, league),
            cups=services.get_cups(db, league),
            payouts=services.get_payouts(db, league),
            adjustments=services.get_standing_adjustments(db, league),
        )
    return templates.TemplateResponse("home.html", ctx)


@app.post("/admin/sync", dependencies=[Depends(require_admin)])
def admin_sync():
    asyncio.run(sync_all())
    return {"ok": True}
