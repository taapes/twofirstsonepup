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
        "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers],
        "is_admin": is_admin(request),
    }


def _writes_allowed(request: Request, league) -> bool:
    """Public writes (picks/trades) allowed unless the commissioner has locked
    editing; the logged-in commissioner can always write."""
    return (not league.writes_locked) or is_admin(request)


def _locked_response():
    return HTMLResponse("Editing is locked by the commissioner.", status_code=423)


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


# ---- public league views ----
@router.get("/teams", response_class=HTMLResponse)
def teams_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        "teams.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "teams": services.get_keepers(db, league)},
    )


@router.get("/team/{fpl_manager_id}", response_class=HTMLResponse)
def team_page(fpl_manager_id: str, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    m = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not m:
        raise HTTPException(status_code=404, detail="team not found")
    team = next((t for t in services.get_keepers(db, league) if t["manager"] == m.display), None)
    return templates.TemplateResponse(
        "team.html",
        {"request": request, "league": league, "is_admin": is_admin(request), "team": team, "manager": m.display},
    )


@router.get("/keepers", response_class=HTMLResponse)
def keepers_page(request: Request, db: Session = Depends(get_db)):
    """Keeper selection — a manager (or admin) picks their keepers for next season."""
    league = _league_or_404(db)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    )
    return templates.TemplateResponse("keepers_select.html", {
        "request": request, "league": league, "is_admin": is_admin(request),
        "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers],
        "season": (league.season_year or 0) + 1,
    })


@router.get("/keepers/candidates", response_class=HTMLResponse)
def keepers_candidates(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    fpl = request.query_params.get("fpl_manager_id")
    cands = services.keeper_candidates(db, league, fpl) if fpl else None
    return templates.TemplateResponse(
        "_keeper_candidates.html", {"request": request, "candidates": cands}
    )


@router.post("/keepers")
def keepers_submit(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), season_year: int = Form(...),
    keeper_fpl_ids: list[int] = Form(default=[]), discovery_fpl_id: str = Form(""),
):
    league = _league_or_404(db)
    if not _writes_allowed(request, league):
        return _locked_response()
    try:
        services.submit_keepers(
            db, league, fpl_manager_id=fpl_manager_id, keeper_fpl_ids=keeper_fpl_ids,
            season_year=season_year,
            discovery_fpl_id=int(discovery_fpl_id) if discovery_fpl_id.strip() else None,
        )
    except RuleViolation as e:
        return HTMLResponse(f"error: {e}", status_code=400)
    return RedirectResponse("/teams", status_code=303)


@router.get("/picks", response_class=HTMLResponse)
def picks_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        "picks.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "future_picks": services.get_future_picks(db, league)},
    )


@router.get("/admin/health", response_class=HTMLResponse)
def admin_health(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    return templates.TemplateResponse("admin_health.html", {
        "request": request, "league": league, "is_admin": True,
        "checks": services.data_health(db, league),
        "writes_locked": league.writes_locked,
    })


@router.post("/admin/lock")
def admin_lock(request: Request, db: Session = Depends(get_db), lock: str = Form("")):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    league.writes_locked = lock == "on"
    db.commit()
    return RedirectResponse("/admin/health", status_code=303)


@router.get("/admin/standings", response_class=HTMLResponse)
def admin_standings(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    )
    return templates.TemplateResponse("admin_standings.html", {
        "request": request, "league": league, "is_admin": True,
        "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers],
        "standings": services.get_standings(db, league),
        "adjustments": services.get_standing_adjustments(db, league),
    })


@router.post("/admin/standings/adjust")
def admin_standings_adjust(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), total: str = Form(""),
    points_for: str = Form(""), note: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    try:
        services.adjust_standing(
            db, league, fpl_manager_id=fpl_manager_id,
            total=int(total) if total.strip() else None,
            points_for=int(points_for) if points_for.strip() else None,
            note=note or None,
        )
    except (RuleViolation, ValueError) as e:
        return HTMLResponse(f"error: {e}", status_code=400)
    return RedirectResponse("/admin/standings", status_code=303)


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        "history.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "history": services.get_history(db, league)},
    )


@router.get("/trade", response_class=HTMLResponse)
def trade_page(request: Request, db: Session = Depends(get_db)):
    """Public trade entry — any manager can record a trade (players + picks, no cap)."""
    league = _league_or_404(db)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    )
    return templates.TemplateResponse(
        "trade.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers]},
    )


@router.get("/trade/assets/{side}", response_class=HTMLResponse)
def trade_assets(side: str, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    fpl = request.query_params.get(f"{side}_manager")
    assets = services.manager_assets(db, league, fpl) if fpl else None
    return templates.TemplateResponse(
        "_trade_assets.html", {"request": request, "side": side, "assets": assets}
    )


@router.post("/trade")
def trade_submit(
    request: Request,
    db: Session = Depends(get_db),
    a_manager: str = Form(...),
    b_manager: str = Form(...),
    a_players: list[str] = Form(default=[]),
    b_players: list[str] = Form(default=[]),
    a_picks: list[str] = Form(default=[]),
    b_picks: list[str] = Form(default=[]),
):
    league = _league_or_404(db)
    if not _writes_allowed(request, league):
        return _locked_response()
    try:
        services.record_trade(
            db, league, a_fpl=a_manager, b_fpl=b_manager,
            a_players=a_players, a_picks=a_picks, b_players=b_players, b_picks=b_picks,
        )
    except RuleViolation as e:
        return HTMLResponse(f"error: {e}", status_code=400)
    return RedirectResponse("/trades", status_code=303)


@router.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        "trades.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "trades": services.get_trades(db, league)},
    )


# ---- draft board ----
@router.get("/draft/{year}", response_class=HTMLResponse)
def draft_page(year: int, request: Request, draft_type: str = "main", db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse("draft.html", _board_ctx(request, db, league, year, draft_type))


@router.get("/draft/{year}/search", response_class=HTMLResponse)
def draft_search(
    year: int, request: Request, q: str = "", position: str = "", sort: str = "",
    db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    results = []
    if q.strip() or position or sort:
        results = services.search_players(
            db, league, q=q.strip() or None, position=position or None,
            sort=sort or None, available_year=year, limit=50,
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
    if not _writes_allowed(request, league):
        return _locked_response()
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
    if not _writes_allowed(request, league):
        return _locked_response()
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
    if not _writes_allowed(request, league):
        return _locked_response()
    try:
        services.trade_player(db, league, from_fpl=from_fpl, to_fpl=to_fpl, player_fpl_id=player_fpl_id)
    except RuleViolation as e:
        return HTMLResponse(f"error: {e}", status_code=400)
    return _board_response(request, db, league, year)


@router.post("/draft/{year}/order", response_class=HTMLResponse)
def draft_set_order(year: int, request: Request, order: str = Form(...), db: Session = Depends(get_db)):
    """`order` is a comma-separated list of fpl_manager_ids in round-1 pick order."""
    league = _league_or_404(db)
    if not _writes_allowed(request, league):
        return _locked_response()
    ids = [s.strip() for s in order.split(",") if s.strip()]
    try:
        services.set_draft_order(db, league, ids)
    except RuleViolation as e:
        return HTMLResponse(f"error: {e}", status_code=400)
    return _board_response(request, db, league, year)
