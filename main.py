import asyncio
import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
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
    "/health", "/who", "/login", "/demo-login", "/logout", "/set-password",
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


_DEV_SECRET = "dev-insecure-change-me"
SECRET_KEY = os.getenv("SECRET_KEY", _DEV_SECRET)
APP_ENV = os.getenv("APP_ENV", "prod")
# Fail fast rather than run production on the insecure default signing key.
if APP_ENV == "prod" and SECRET_KEY == _DEV_SECRET:
    raise RuntimeError(
        "SECRET_KEY must be set to a stable secret in production. "
        "Set SECRET_KEY in the environment (Render dashboard)."
    )
# Secure cookies require HTTPS; gate on an explicit flag (set SESSION_HTTPS_ONLY=1
# on Render) so local dev over http still works.
_SESSION_HTTPS_ONLY = os.getenv("SESSION_HTTPS_ONLY", "0") == "1"

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Standard hardening headers on every response (incl. gate redirects)."""

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        if request.url.scheme == "https":
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return resp


app = FastAPI()
# Add order is reverse of execution order. We want, outer→inner:
#   SessionMiddleware (populates request.session) → SecurityHeaders (wraps every
#   response, including the gate's redirects) → GateMiddleware → app.
app.add_middleware(GateMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    https_only=_SESSION_HTTPS_ONLY,
    same_site="lax",
)
app.include_router(v1_router)
app.include_router(admin_router)
app.include_router(ui_router)


# ---- friendly error pages ----
_ERROR_MESSAGES = {
    400: "That request couldn't be processed.",
    401: "Please sign in to continue.",
    403: "You don't have access to that.",
    404: "We couldn't find that page.",
    423: "That action is locked right now.",
}


def _wants_machine_response(request: Request) -> bool:
    """JSON for the public API + token/cron callers; everything else is a browser."""
    return request.url.path.startswith("/v1") or bool(request.headers.get("X-Auth-Token"))


def _error_response(request: Request, status_code: int, message: str):
    if _wants_machine_response(request):
        return JSONResponse({"detail": message}, status_code=status_code)
    if request.headers.get("HX-Request") == "true":
        return PlainTextResponse(message, status_code=status_code)
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "status_code": status_code, "message": message},
        status_code=status_code,
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception(request: Request, exc: StarletteHTTPException):
    message = _ERROR_MESSAGES.get(exc.status_code) or (
        exc.detail if isinstance(exc.detail, str) else "Something went wrong."
    )
    resp = _error_response(request, exc.status_code, message)
    # preserve auth challenge headers (e.g. WWW-Authenticate) if any
    if getattr(exc, "headers", None):
        for k, v in exc.headers.items():
            resp.headers.setdefault(k, v)
    return resp


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    # Don't leak internals; just a clean 500 with a way home.
    return _error_response(request, 500, "Something went wrong on our end.")


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
            international_list=services.get_international_list(db, league),
            cups=services.get_cups(db, league),
            payouts=services.get_payouts(db, league),
            adjustments=services.get_standing_adjustments(db, league),
            ineligible=services.ineligible_players(db, league),
        )
    return templates.TemplateResponse("home.html", ctx)


@app.post("/admin/sync", dependencies=[Depends(require_admin)])
def admin_sync(force: bool = False):
    """Heartbeat: advance the time/GW-driven phase, then sync per the fixture-aligned
    cadence plan (full | live | skip). `?force=1` always does a full sync (manual)."""
    from db import SessionLocal
    from sync import sync_fixtures, sync_gameweek_points, sync_rosters

    advanced = False
    plan = "full"
    with SessionLocal() as db:
        league = services.current_league(db)
        if league:
            advanced = services.advance_phase_if_due(db, league)
            plan = "full" if force else services.sync_plan(db, league)

    if plan == "full":
        asyncio.run(sync_all())
        # after a full pull, re-evaluate post-draft (non-DEF) additions
        with SessionLocal() as db:
            league = services.current_league(db)
            if league:
                services.flag_ineligible(db, league)
    elif plan == "live":
        # only the GW-changing pulls while matches are live
        async def _live():
            await sync_rosters()
            await sync_gameweek_points()
            await sync_fixtures()

        asyncio.run(_live())
    # plan == "skip": nothing to do (phase advance already ran)
    return {"ok": True, "plan": plan, "phase_advanced": advanced}
