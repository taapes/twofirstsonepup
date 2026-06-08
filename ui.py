"""Server-rendered UI (Jinja2 + HTMX). Public read; write routes need the
commissioner session login. Renders HTML and calls services.py directly."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

import services
from auth import check_admin_password, is_admin
from db import get_db
from models import Manager
from rules import RuleViolation
from settings import LEAGUE_ID
from templating import templates

router = APIRouter()


def _league_or_404(db: Session):
    league = services.resolve_league(db, LEAGUE_ID) if LEAGUE_ID else None
    if not league:
        raise HTTPException(status_code=404, detail="league not configured")
    return league


def _board_ctx(request: Request, db: Session, league, year: int, draft_type: str = "main") -> dict:
    board = services.get_draft_board(db, league, year, draft_type)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.name).all()
    )
    return {
        "request": request,
        "league": league,
        "year": year,
        "draft_type": draft_type,
        "board": board,
        "on_clock": services.next_open_pick(board),
        "managers": [{"name": m.name, "fpl": m.fpl_manager_id} for m in managers],
        "is_admin": is_admin(request),
    }


def _board_response(request, db, league, year, draft_type="main"):
    """Render the board partial + tell HTMX the draft changed (so search refreshes)."""
    resp = templates.TemplateResponse("_board.html", _board_ctx(request, db, league, year, draft_type))
    resp.headers["HX-Trigger"] = "draftChanged"
    return resp


# ---- commissioner login ----
@router.get("/admin/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(
        "login.html", {"request": request, "next": next, "error": None, "is_admin": is_admin(request)}
    )


@router.post("/admin/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if check_admin_password(password):
        request.session["admin"] = True
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": "Incorrect password", "is_admin": False},
        status_code=401,
    )


@router.get("/admin/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ---- draft board ----
@router.get("/draft/{year}", response_class=HTMLResponse)
def draft_page(year: int, request: Request, draft_type: str = "main", db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse("draft.html", _board_ctx(request, db, league, year, draft_type))


@router.get("/draft/{year}/search", response_class=HTMLResponse)
def draft_search(year: int, request: Request, q: str = "", position: str = "", db: Session = Depends(get_db)):
    league = _league_or_404(db)
    results = []
    if q.strip() or position:
        results = services.search_players(
            db, league, q=q.strip() or None, position=position or None,
            available_year=year, limit=25,
        )
    return templates.TemplateResponse(
        "_search_results.html", {"request": request, "results": results, "year": year, "is_admin": is_admin(request)}
    )


@router.post("/draft/{year}/pick", response_class=HTMLResponse)
def draft_pick(
    year: int, request: Request, player_fpl_id: int = Form(...),
    pick_number: int | None = Form(None), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    if not is_admin(request):
        return HTMLResponse("login required", status_code=403)
    board = services.get_draft_board(db, league, year)
    slot = (
        services.next_open_pick(board)
        if pick_number is None
        else next((b for b in board if b["pick"] == pick_number), None)
    )
    if slot and slot.get("owner_fpl"):
        try:
            services.record_pick(
                db, league, season_year=year, pick_number=slot["pick"],
                owner_fpl=slot["owner_fpl"], player_fpl_id=player_fpl_id, round=slot["round"],
            )
        except RuleViolation:
            pass
    return _board_response(request, db, league, year)


@router.post("/draft/{year}/trade-pick", response_class=HTMLResponse)
def draft_trade_pick(
    year: int, request: Request, from_fpl: str = Form(...), to_fpl: str = Form(...),
    original_fpl: str = Form(...), round: int = Form(...),
    draft_type: str = Form("main"), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    if not is_admin(request):
        return HTMLResponse("login required", status_code=403)
    try:
        services.trade_pick(
            db, league, from_fpl=from_fpl, to_fpl=to_fpl, original_fpl=original_fpl,
            round=round, season_year=year, draft_type=draft_type,
        )
    except RuleViolation as e:
        return HTMLResponse(f"error: {e}", status_code=400)
    return _board_response(request, db, league, year, draft_type)


@router.post("/draft/{year}/trade-player", response_class=HTMLResponse)
def draft_trade_player(
    year: int, request: Request, from_fpl: str = Form(...), to_fpl: str = Form(...),
    player_fpl_id: int = Form(...), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    if not is_admin(request):
        return HTMLResponse("login required", status_code=403)
    try:
        services.trade_player(db, league, from_fpl=from_fpl, to_fpl=to_fpl, player_fpl_id=player_fpl_id)
    except RuleViolation as e:
        return HTMLResponse(f"error: {e}", status_code=400)
    return _board_response(request, db, league, year)


@router.post("/draft/{year}/order", response_class=HTMLResponse)
def draft_set_order(year: int, request: Request, order: str = Form(...), db: Session = Depends(get_db)):
    """`order` is a comma-separated list of fpl_manager_ids in round-1 pick order."""
    league = _league_or_404(db)
    if not is_admin(request):
        return HTMLResponse("login required", status_code=403)
    ids = [s.strip() for s in order.split(",") if s.strip()]
    try:
        services.set_draft_order(db, league, ids)
    except RuleViolation as e:
        return HTMLResponse(f"error: {e}", status_code=400)
    return _board_response(request, db, league, year)
