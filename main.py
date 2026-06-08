import asyncio
import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

import services
from admin import router as admin_router
from api import router as v1_router
from auth import is_admin, require_admin
from db import get_db
from settings import LEAGUE_ID
from sync import sync_all
from templating import templates
from ui import router as ui_router

app = FastAPI()
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
    """Minimal public homepage: standings, infractions, injury list."""
    league = services.resolve_league(db, LEAGUE_ID) if LEAGUE_ID else None
    ctx = {"request": request, "league": league, "is_admin": is_admin(request)}
    if league:
        ctx.update(
            standings=services.get_standings(db, league),
            infractions=services.get_infractions(db, league),
            injury_list=services.get_injury_list(db, league),
            cups=services.get_cups(db, league),
            payouts=services.get_payouts(db, league),
        )
    return templates.TemplateResponse("home.html", ctx)


@app.post("/admin/sync", dependencies=[Depends(require_admin)])
def admin_sync():
    asyncio.run(sync_all())
    return {"ok": True}
