import asyncio

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import services
from admin import router as admin_router
from api import router as v1_router
from auth import require_admin
from db import get_db
from settings import LEAGUE_ID
from sync import sync_all

app = FastAPI()
app.include_router(v1_router)
app.include_router(admin_router)
templates = Jinja2Templates(directory="templates")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    """Minimal public homepage: standings, infractions, injury list."""
    league = services.resolve_league(db, LEAGUE_ID) if LEAGUE_ID else None
    ctx = {"request": request, "league": league}
    if league:
        ctx.update(
            standings=services.get_standings(db, league),
            infractions=services.get_infractions(db, league),
            injury_list=services.get_injury_list(db, league),
        )
    return templates.TemplateResponse("home.html", ctx)


@app.post("/admin/sync", dependencies=[Depends(require_admin)])
def admin_sync():
    asyncio.run(sync_all())
    return {"ok": True}
