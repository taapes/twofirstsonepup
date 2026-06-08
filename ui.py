"""Server-rendered UI (Jinja2 + HTMX). Public read; write routes need the
commissioner session login. Renders HTML and calls services.py directly."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

import services
from auth import (
    can_act_as,
    check_admin_password,
    current_manager_id,
    hash_password,
    is_admin,
    verify_password,
)
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
    mgr_opts = [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers]
    fpl_by_person = {m.display: m.fpl_manager_id for m in managers}

    # current round-1 order (commissioner-set) for the visual reorder control,
    # falling back to alphabetical so there's always something to drag
    r1 = services.get_draft_order(db, league)
    r1_order = r1 if r1 else mgr_opts

    # tradeable pick slots, grouped by round, with current holder derived
    pick_slots: dict = {}
    for b in board:
        key = (b["round"], b["original_owner"])
        if key in pick_slots:
            continue
        pick_slots[key] = {
            "round": b["round"],
            "original_owner": b["original_owner"],
            "original_fpl": fpl_by_person.get(b["original_owner"]),
            "current_owner": b["owner"],
            "current_fpl": b["owner_fpl"],
        }
    picks_by_round: dict = {}
    for slot in pick_slots.values():
        picks_by_round.setdefault(slot["round"], []).append(slot)
    pick_rounds = [
        {"round": r, "picks": sorted(picks_by_round[r], key=lambda s: s["original_owner"])}
        for r in sorted(picks_by_round)
    ]

    on_clock = services.next_open_pick(board)
    return {
        "request": request,
        "league": league,
        "year": year,
        "draft_type": draft_type,
        "board": board,
        "on_clock": on_clock,
        "can_pick": bool(on_clock) and can_act_as(request, on_clock.get("owner_fpl")),
        "managers": mgr_opts,
        "r1_order": r1_order,
        "pick_rounds": pick_rounds,
        "players": services.list_players(db, league),
        "is_admin": is_admin(request),
    }


def _writes_allowed(request: Request, league) -> bool:
    """Public writes (picks/trades) allowed unless the commissioner has locked
    editing; the logged-in commissioner can always write."""
    return (not league.writes_locked) or is_admin(request)


def _locked_response(what="Editing"):
    return PlainTextResponse(f"{what} is locked by the commissioner.", status_code=423)


def _err(msg, status_code: int = 400):
    """Error response as plain text (text/plain ⇒ never rendered as HTML, so an
    error message can't carry markup into the page)."""
    return PlainTextResponse(f"error: {msg}", status_code=status_code)


def _safe_int(value, lo: int, hi: int, *, field: str = "value") -> int:
    """Parse a bounded integer from form input; raise RuleViolation (→ 400) on
    non-numeric or out-of-range, instead of letting int() throw a 500."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise RuleViolation(f"{field} must be a whole number")
    if n < lo or n > hi:
        raise RuleViolation(f"{field} must be between {lo} and {hi}")
    return n


def _keepers_allowed(request: Request, league) -> bool:
    return (not league.keepers_locked) or is_admin(request)


def _board_response(request, db, league, year, draft_type="main"):
    """Render the board partial + tell HTMX the draft changed (so search refreshes)."""
    resp = templates.TemplateResponse("_board.html", _board_ctx(request, db, league, year, draft_type))
    resp.headers["HX-Trigger"] = "draftChanged"
    return resp


# ---- identity / per-manager auth ----
def _current_manager(request: Request, db: Session, league) -> Manager | None:
    """The logged-in manager row (None for admin-only or anonymous)."""
    fpl = current_manager_id(request)
    if not fpl:
        return None
    return (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl))
        .one_or_none()
    )


def _identity_ctx(request: Request, db: Session, league) -> dict:
    """Identity bits shared by every rendered template (DRY)."""
    me = _current_manager(request, db, league)
    return {
        "is_admin": is_admin(request),
        "current_fpl": me.fpl_manager_id if me else None,
        "current_name": me.display if me else None,
    }


def _forbidden(request: Request, what: str = "You can only edit your own team."):
    return HTMLResponse(what, status_code=403)


# ---- "who are you?" gate + per-manager login ----
@router.get("/who", response_class=HTMLResponse)
def who(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    )
    return templates.TemplateResponse("who.html", {
        "request": request, "league": league, "is_admin": is_admin(request), "hide_nav": True,
        "managers": [
            {"name": m.display, "fpl": m.fpl_manager_id, "needs_password": m.password_hash is None}
            for m in managers
        ],
    })


@router.get("/login", response_class=HTMLResponse)
def manager_login_form(manager_id: str, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    m = (
        db.query(Manager).filter_by(league_id=league.id, fpl_manager_id=str(manager_id)).one_or_none()
    )
    if not m:
        raise HTTPException(status_code=404, detail="manager not found")
    return templates.TemplateResponse("manager_login.html", {
        "request": request, "is_admin": is_admin(request), "hide_nav": True,
        "manager": {"name": m.display, "fpl": m.fpl_manager_id},
        "first_time": m.password_hash is None, "error": None,
    })


@router.post("/login")
def manager_login(
    request: Request, db: Session = Depends(get_db),
    manager_id: str = Form(...), password: str = Form(...),
):
    league = _league_or_404(db)
    m = (
        db.query(Manager).filter_by(league_id=league.id, fpl_manager_id=str(manager_id)).one_or_none()
    )
    if not m:
        raise HTTPException(status_code=404, detail="manager not found")
    if m.password_hash is None:
        return RedirectResponse(f"/login?manager_id={manager_id}", status_code=303)
    if not verify_password(password, m.password_hash):
        return templates.TemplateResponse("manager_login.html", {
            "request": request, "is_admin": False, "hide_nav": True,
            "manager": {"name": m.display, "fpl": m.fpl_manager_id},
            "first_time": False, "error": "Incorrect password",
        }, status_code=401)
    request.session.clear()
    request.session["manager_id"] = m.fpl_manager_id
    request.session["manager_name"] = m.display
    return RedirectResponse("/", status_code=303)


@router.post("/set-password")
def set_password(
    request: Request, db: Session = Depends(get_db),
    manager_id: str = Form(...), password: str = Form(...), confirm: str = Form(...),
):
    league = _league_or_404(db)
    m = (
        db.query(Manager).filter_by(league_id=league.id, fpl_manager_id=str(manager_id)).one_or_none()
    )
    if not m:
        raise HTTPException(status_code=404, detail="manager not found")
    # Only settable when no password exists yet (an admin reset clears it). Prevents takeover.
    if m.password_hash is not None:
        return RedirectResponse(f"/login?manager_id={manager_id}", status_code=303)
    if password != confirm or len(password) < 6:
        return templates.TemplateResponse("manager_login.html", {
            "request": request, "is_admin": False, "hide_nav": True,
            "manager": {"name": m.display, "fpl": m.fpl_manager_id},
            "first_time": True,
            "error": "Passwords must match and be at least 6 characters.",
        }, status_code=400)
    m.password_hash = hash_password(password)
    db.commit()
    request.session.clear()
    request.session["manager_id"] = m.fpl_manager_id
    request.session["manager_name"] = m.display
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout_any(request: Request):
    request.session.clear()
    return RedirectResponse("/who", status_code=303)


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
    return RedirectResponse("/who", status_code=303)


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
        "locked": league.keepers_locked and not is_admin(request),
    })


@router.get("/keepers/candidates", response_class=HTMLResponse)
def keepers_candidates(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    fpl = request.query_params.get("fpl_manager_id")
    cands = services.keeper_candidates(db, league, fpl) if fpl else None
    return templates.TemplateResponse(
        "_keeper_candidates.html", {"request": request, "candidates": cands}
    )


@router.get("/keepers/discovery-search", response_class=HTMLResponse)
def keepers_discovery_search(request: Request, db: Session = Depends(get_db)):
    """Search ALL players for the discovery (bonus 6th) keeper slot — not limited
    to the manager's roster."""
    league = _league_or_404(db)
    q = (request.query_params.get("q") or "").strip()
    results = services.search_players(db, league, q=q, sort="points", limit=25) if q else []
    return templates.TemplateResponse(
        "_discovery_search.html", {"request": request, "results": results}
    )


@router.post("/keepers")
def keepers_submit(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), season_year: int = Form(...),
    keeper_fpl_ids: list[int] = Form(default=[]), discovery_fpl_id: str = Form(""),
):
    league = _league_or_404(db)
    if not _keepers_allowed(request, league):
        return _locked_response("Keeper selection")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only set keepers for your own team.")
    try:
        services.submit_keepers(
            db, league, fpl_manager_id=fpl_manager_id, keeper_fpl_ids=keeper_fpl_ids,
            season_year=season_year,
            discovery_fpl_id=int(discovery_fpl_id) if discovery_fpl_id.strip() else None,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/teams", status_code=303)


def _resolve_my_fpl(request: Request, db: Session, league) -> str | None:
    """Whose 'my team' to show: a manager sees their own; admin may pass ?fpl=."""
    if is_admin(request):
        return request.query_params.get("fpl") or current_manager_id(request)
    return current_manager_id(request)


@router.get("/my-team", response_class=HTMLResponse)
def my_team_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    fpl = _resolve_my_fpl(request, db, league)
    team = services.get_my_team(db, league, fpl) if fpl else None
    return templates.TemplateResponse("my_team.html", {
        "request": request, "league": league, "team": team,
    })


@router.get("/my-team/upcoming", response_class=HTMLResponse)
def my_team_upcoming_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    fpl = _resolve_my_fpl(request, db, league)
    matchups = services.get_upcoming_matchups(db, league, fpl) if fpl else []
    me = services.get_my_team(db, league, fpl) if fpl else None
    return templates.TemplateResponse("my_team_upcoming.html", {
        "request": request, "league": league,
        "matchups": matchups, "me_name": me["manager"] if me else None,
    })


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
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    )
    return templates.TemplateResponse("admin_health.html", {
        "request": request, "league": league, "is_admin": True,
        "checks": services.data_health(db, league),
        "writes_locked": league.writes_locked,
        "keepers_locked": league.keepers_locked,
        "managers": [
            {"name": m.display, "fpl": m.fpl_manager_id, "has_password": m.password_hash is not None}
            for m in managers
        ],
    })


@router.post("/admin/lock")
def admin_lock(
    request: Request, db: Session = Depends(get_db),
    lock: str = Form(""), keepers_lock: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    league.writes_locked = lock == "on"
    league.keepers_locked = keepers_lock == "on"
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
        "fines": services.get_fines(db, league),
    })


@router.post("/admin/standings/adjust")
def admin_standings_adjust(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), total_delta: str = Form(""),
    points_for_delta: str = Form(""), gameweek: str = Form(""), note: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    try:
        services.adjust_standing(
            db, league, fpl_manager_id=fpl_manager_id,
            total_delta=_safe_int(total_delta, -10000, 10000, field="H2H change") if total_delta.strip() else 0,
            points_for_delta=_safe_int(points_for_delta, -10000, 10000, field="total change") if points_for_delta.strip() else 0,
            gameweek=_safe_int(gameweek, 1, 38, field="gameweek") if gameweek.strip() else None,
            note=note or None,
        )
    except (RuleViolation, ValueError) as e:
        return _err(e)
    return RedirectResponse("/admin/standings", status_code=303)


@router.post("/admin/standings/delete")
def admin_standings_delete(
    request: Request, db: Session = Depends(get_db), adjustment_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    try:
        services.delete_standing_adjustment(db, league, adjustment_id)
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/standings", status_code=303)


@router.post("/admin/managers/reset-password")
def admin_reset_password(
    request: Request, db: Session = Depends(get_db), fpl_manager_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    services.reset_manager_password(db, league, fpl_manager_id)
    return RedirectResponse("/admin/health", status_code=303)


# ---- anti-tanking flags (admin clear/restore) ----
@router.post("/admin/flags/clear")
def admin_clear_flag(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), window: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/", status_code=303)
    league = _league_or_404(db)
    try:
        services.clear_flag(db, league, fpl_manager_id, window)
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/", status_code=303)


@router.post("/admin/flags/restore")
def admin_restore_flag(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), window: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/", status_code=303)
    league = _league_or_404(db)
    try:
        services.restore_flag(db, league, fpl_manager_id, window)
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/", status_code=303)


# ---- fines (admin) ----
@router.post("/admin/fines/add")
def admin_add_fine(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), amount: str = Form(...),
    reason: str = Form(""), gameweek: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    try:
        services.add_fine(
            db, league, fpl_manager_id=fpl_manager_id,
            amount=_safe_int(amount, 1, 100000, field="amount"),
            reason=reason or None,
            gameweek=_safe_int(gameweek, 1, 38, field="gameweek") if gameweek.strip() else None,
        )
    except (RuleViolation, ValueError) as e:
        return _err(e)
    return RedirectResponse("/admin/standings", status_code=303)


@router.post("/admin/fines/delete")
def admin_delete_fine(
    request: Request, db: Session = Depends(get_db), fine_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    try:
        services.delete_fine(db, league, fine_id)
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/standings", status_code=303)


# ---- cups (admin: generate + score the auto-bracket) ----
@router.get("/admin/cups", response_class=HTMLResponse)
def admin_cups(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/cups", status_code=303)
    league = _league_or_404(db)
    return templates.TemplateResponse("admin_cups.html", {
        "request": request, "league": league, "is_admin": True,
        "cups": services.get_cups(db, league),
    })


@router.post("/admin/cups/generate")
def admin_cups_generate(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/cups", status_code=303)
    league = _league_or_404(db)
    try:
        services.generate_cups(db, league)
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/cups", status_code=303)


@router.post("/admin/cups/score-round")
def admin_cups_score_round(
    request: Request, db: Session = Depends(get_db),
    round: str = Form(...), gw1: str = Form(...), gw2: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/cups", status_code=303)
    league = _league_or_404(db)
    try:
        services.score_cup_round(
            db, league,
            _safe_int(round, 1, 3, field="round"),
            _safe_int(gw1, 1, 38, field="gw1"),
            _safe_int(gw2, 1, 38, field="gw2"),
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/cups", status_code=303)


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
    if not can_act_as(request, a_manager, b_manager):
        return _forbidden(request, "You must be one of the two managers in the trade.")
    try:
        services.record_trade(
            db, league, a_fpl=a_manager, b_fpl=b_manager,
            a_players=a_players, a_picks=a_picks, b_players=b_players, b_picks=b_picks,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/trades", status_code=303)


@router.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        "trades.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "trades": services.get_trades(db, league),
         "trade_notes": services.get_trade_notes(db, league)},
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
            sort=sort or None, available_year=year, include_taken=True, limit=50,
        )
    on_clock = services.next_open_pick(services.get_draft_board(db, league, year))
    can_pick = bool(on_clock) and can_act_as(request, on_clock.get("owner_fpl"))
    return templates.TemplateResponse(
        "_search_results.html", {"request": request, "results": results, "year": year,
                                 "is_admin": is_admin(request), "can_pick": can_pick}
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
        if not can_act_as(request, slot["owner_fpl"]):
            return _forbidden(request, "It's not your pick to make.")
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
    year: int, request: Request, pick: str = Form(...), to_fpl: str = Form(...),
    draft_type: str = Form("main"), db: Session = Depends(get_db),
):
    """`pick` is the combined "<original_fpl>:<round>" slot id; the current holder
    (from) is derived from live pick ownership so the form only needs pick + to."""
    league = _league_or_404(db)
    if not _writes_allowed(request, league):
        return _locked_response()
    try:
        original_fpl, round_str = pick.rsplit(":", 1)
        round = int(round_str)
    except ValueError:
        return HTMLResponse("error: malformed pick", status_code=400)
    # current holder of this (original owner, round) slot
    orig_person = services._resolve_manager(db, league, original_fpl).display
    board = services.get_draft_board(db, league, year, draft_type)
    cur = next(
        (b for b in board if b["round"] == round and b["original_owner"] == orig_person),
        None,
    )
    from_fpl = cur["owner_fpl"] if cur else original_fpl
    if not can_act_as(request, from_fpl, to_fpl):
        return _forbidden(request, "You must be one of the two managers in the pick trade.")
    try:
        services.trade_pick(
            db, league, from_fpl=from_fpl, to_fpl=to_fpl, original_fpl=original_fpl,
            round=round, season_year=year, draft_type=draft_type,
        )
    except RuleViolation as e:
        return _err(e)
    return _board_response(request, db, league, year, draft_type)


@router.post("/draft/{year}/trade-player", response_class=HTMLResponse)
def draft_trade_player(
    year: int, request: Request, from_fpl: str = Form(...), to_fpl: str = Form(...),
    player_fpl_id: int = Form(...), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    if not _writes_allowed(request, league):
        return _locked_response()
    if not can_act_as(request, from_fpl, to_fpl):
        return _forbidden(request, "You must be one of the two managers in the player trade.")
    try:
        services.trade_player(db, league, from_fpl=from_fpl, to_fpl=to_fpl, player_fpl_id=player_fpl_id)
    except RuleViolation as e:
        return _err(e)
    return _board_response(request, db, league, year)


@router.post("/draft/{year}/order", response_class=HTMLResponse)
def draft_set_order(year: int, request: Request, order: str = Form(...), db: Session = Depends(get_db)):
    """`order` is a comma-separated list of fpl_manager_ids in round-1 pick order."""
    league = _league_or_404(db)
    if not is_admin(request):
        return _forbidden(request, "Only the commissioner can set the draft order.")
    ids = [s.strip() for s in order.split(",") if s.strip()]
    try:
        services.set_draft_order(db, league, ids)
    except RuleViolation as e:
        return _err(e)
    return _board_response(request, db, league, year)
