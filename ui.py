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
from models import InjuryList, Manager
from rules import RuleViolation, SEASON_LAST_GW
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


def _feature_allowed(
    request: Request, db: Session, league, flag: str, *, lock_attr: str = "writes_locked"
) -> bool:
    """Is a phase-gated feature available for a write? Admin always bypasses; else the
    manual lock (`writes_locked`/`keepers_locked`) must be off AND the current phase
    must enable `flag` (see services.phase_context / rules.phase_features)."""
    if is_admin(request):
        return True
    if getattr(league, lock_attr, False):
        return False
    return bool(services.phase_context(db, league).get(flag, False))


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
    # keepers are only editable in the offseason phase (and not when manually locked);
    # admin can always edit.
    editable = _feature_allowed(request, db, league, "keepers_editable", lock_attr="keepers_locked")
    return templates.TemplateResponse("keepers_select.html", {
        "request": request, "league": league, "is_admin": is_admin(request),
        "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers],
        "season": (league.season_year or 0) + 1,
        "locked": not editable,
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
    if not _feature_allowed(request, db, league, "keepers_editable", lock_attr="keepers_locked"):
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
    cur = services.current_gameweek(db, league)
    return templates.TemplateResponse("my_team.html", {
        "request": request, "league": league, "team": team,
        # IL self-service controls (only when viewing your own team / admin)
        "can_edit_il": bool(team) and (is_admin(request) or fpl == current_manager_id(request)),
        "players": services.list_players(db, league),
        "current_gw": cur,
        "season_last_gw": SEASON_LAST_GW,
        "season_over": cur is not None and cur >= SEASON_LAST_GW,
    })


# ---- injury list (manager self-service: place / return / release) ----
def _il_entry_or_403(db, league, fpl_manager_id: str, il_id: str):
    """Resolve an IL entry and confirm it belongs to fpl_manager_id (admin bypass)."""
    manager = services._resolve_manager(db, league, fpl_manager_id)
    entry = db.get(InjuryList, il_id)
    if not entry or entry.manager_id != manager.id:
        return None
    return entry


@router.post("/il/place")
def il_place(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), injured_fpl_id: str = Form(...),
    replacement_fpl_id: str = Form(...),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "gw_logic_active"):
        return _locked_response("The injury list")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only manage your own team's injury list.")
    try:
        services.place_on_il(
            db, league, fpl_manager_id=fpl_manager_id,
            injured_fpl_id=_safe_int(injured_fpl_id, 1, 10_000_000, field="injured player"),
            replacement_fpl_id=_safe_int(replacement_fpl_id, 1, 10_000_000, field="replacement"),
            start_gw=services.current_gameweek(db, league) or 1,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/my-team", status_code=303)


@router.post("/il/return")
def il_return(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), il_id: str = Form(...),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "gw_logic_active"):
        return _locked_response("The injury list")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only manage your own team's injury list.")
    if not _il_entry_or_403(db, league, fpl_manager_id, il_id):
        return _forbidden(request, "That injury-list entry isn't yours.")
    try:
        services.return_from_il(
            db, league, il_id, services.current_gameweek(db, league) or SEASON_LAST_GW,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/my-team", status_code=303)


@router.post("/il/release")
def il_release(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), il_id: str = Form(...),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "gw_logic_active"):
        return _locked_response("The injury list")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only manage your own team's injury list.")
    if not _il_entry_or_403(db, league, fpl_manager_id, il_id):
        return _forbidden(request, "That injury-list entry isn't yours.")
    try:
        services.return_from_il(
            db, league, il_id, services.current_gameweek(db, league) or SEASON_LAST_GW,
            via="waiver",
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/my-team", status_code=303)


# ---- international list (AFCON / Asia Cup) self-service ----
def _intl_entry_or_403(db, league, fpl_manager_id: str, intl_id: str):
    from models import InternationalList
    manager = services._resolve_manager(db, league, fpl_manager_id)
    entry = db.get(InternationalList, intl_id)
    if not entry or entry.manager_id != manager.id:
        return None
    return entry


@router.post("/intl/place")
def intl_place(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), away_fpl_id: str = Form(...),
    replacement_fpl_id: str = Form(...), tournament: str = Form(""),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "gw_logic_active"):
        return _locked_response("The international list")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only manage your own team's international list.")
    try:
        services.place_on_intl(
            db, league, fpl_manager_id=fpl_manager_id,
            away_fpl_id=_safe_int(away_fpl_id, 1, 10_000_000, field="away player"),
            replacement_fpl_id=_safe_int(replacement_fpl_id, 1, 10_000_000, field="replacement"),
            start_gw=services.current_gameweek(db, league) or 1,
            tournament=tournament or None,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/my-team", status_code=303)


@router.post("/intl/return")
def intl_return(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), intl_id: str = Form(...),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "gw_logic_active"):
        return _locked_response("The international list")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only manage your own team's international list.")
    if not _intl_entry_or_403(db, league, fpl_manager_id, intl_id):
        return _forbidden(request, "That international-list entry isn't yours.")
    try:
        services.return_from_intl(
            db, league, intl_id, services.current_gameweek(db, league) or SEASON_LAST_GW,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/my-team", status_code=303)


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
    from rules import PHASES

    return templates.TemplateResponse("admin_health.html", {
        "request": request, "league": league, "is_admin": True,
        "checks": services.data_health(db, league),
        "writes_locked": league.writes_locked,
        "keepers_locked": league.keepers_locked,
        "phase_ctx": services.phase_context(db, league),
        "phase_manual": league.phase_manual,
        "discovery_open": league.discovery_open,
        "phases": PHASES,
        "managers": [
            {"name": m.display, "fpl": m.fpl_manager_id, "has_password": m.password_hash is not None}
            for m in managers
        ],
    })


@router.post("/admin/phase/draft")
def admin_phase_draft(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    services.enter_draft_phase(db, league)
    return RedirectResponse("/admin/health", status_code=303)


@router.post("/admin/phase/set")
def admin_phase_set(
    request: Request, db: Session = Depends(get_db),
    phase: str = Form(...), pin: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    try:
        services.set_phase(db, league, phase, manual=(pin == "on"))
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/health", status_code=303)


@router.post("/admin/phase/unpin")
def admin_phase_unpin(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    services.set_phase_pin(db, league, False)
    return RedirectResponse("/admin/health", status_code=303)


@router.post("/admin/phase/close-discovery")
def admin_phase_close_discovery(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    services.close_discovery(db, league)
    return RedirectResponse("/admin/health", status_code=303)


# ---- season rollover (Preseason: sync the new FPL league + carry forward) ----
@router.get("/admin/season", response_class=HTMLResponse)
def admin_season(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/season", status_code=303)
    from models import League as _League

    current = services.current_league(db)
    leagues = [
        {"name": lg.name, "fpl": lg.fpl_league_id, "season": lg.season_year,
         "is_current": lg.is_current, "phase": lg.phase}
        for lg in db.query(_League).order_by(_League.season_year)
    ]
    return templates.TemplateResponse("admin_season.html", {
        "request": request, "league": current, "is_admin": True,
        "current": {"name": current.name, "season": current.season_year,
                    "fpl": current.fpl_league_id} if current else None,
        "leagues": leagues,
    })


@router.post("/admin/season/advance")
def admin_season_advance(
    request: Request, db: Session = Depends(get_db), new_fpl_league_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/season", status_code=303)
    new_id = new_fpl_league_id.strip()
    if not new_id:
        return _err("enter the new season's FPL draft league id")
    old_league = services.current_league(db)
    if old_league and str(old_league.fpl_league_id) == new_id:
        return _err("that's already the current league")
    # 1. sync the new league id (creates the new league row + managers + schedule)
    import asyncio
    import sync as _sync

    try:
        asyncio.run(_sync.sync_all(fpl_league_id=new_id))
    except Exception as e:  # network / bad id
        return _err(f"sync of new league failed: {e}", status_code=502)
    new_league = services.resolve_league(db, new_id)
    if not new_league:
        return _err("new league did not sync (check the id)", status_code=502)
    # 2. carry forward + flip current + preseason
    try:
        services.advance_season(db, old_league, new_league)
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/season", status_code=303)


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
    managers = db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    sug_cup, sug_pup = services.prior_season_shield_participants(db, league)
    return templates.TemplateResponse("admin_cups.html", {
        "request": request, "league": league, "is_admin": True,
        "cups": services.get_cups(db, league),
        "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers],
        "shield": services.get_shield(db, league),
        "suggest_cup": sug_cup, "suggest_pup": sug_pup,
    })


@router.post("/admin/shield/set")
def admin_shield_set(
    request: Request, db: Session = Depends(get_db),
    cup_winner_fpl: str = Form(...), pup_winner_fpl: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/cups", status_code=303)
    league = _league_or_404(db)
    try:
        services.set_shield(db, league, cup_winner_fpl=cup_winner_fpl, pup_winner_fpl=pup_winner_fpl)
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/cups", status_code=303)


@router.post("/admin/shield/score")
def admin_shield_score(request: Request, db: Session = Depends(get_db), gw: str = Form("1")):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/cups", status_code=303)
    league = _league_or_404(db)
    try:
        services.score_shield(db, league, _safe_int(gw, 1, 38, field="gameweek"))
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/cups", status_code=303)


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


@router.post("/admin/cups/override")
def admin_cups_override(
    request: Request, db: Session = Depends(get_db),
    match_id: str = Form(...), score_a: str = Form(...), score_b: str = Form(...),
):
    """Hand-set a cup match's two scores (e.g. DGW 'first game only') + recompute winner."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/cups", status_code=303)
    league = _league_or_404(db)
    try:
        services.override_cup_match(
            db, league, match_id,
            _safe_int(score_a, 0, 100000, field="home score"),
            _safe_int(score_b, 0, 100000, field="away score"),
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/cups", status_code=303)


@router.get("/cups", response_class=HTMLResponse)
def cups_page(request: Request, db: Session = Depends(get_db)):
    """Public, read-only cup brackets."""
    league = _league_or_404(db)
    return templates.TemplateResponse("cups.html", {
        "request": request, "league": league, "cups": services.get_cups(db, league),
        "shield": services.get_shield(db, league),
    })


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        "history.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "history": services.get_history(db, league)},
    )


@router.get("/seasons", response_class=HTMLResponse)
def seasons_page(request: Request, db: Session = Depends(get_db)):
    """Every season the app has data for (one league row per season). Each links to a
    read-only summary; the current season is marked."""
    from models import League as _League

    league = _league_or_404(db)
    rows = db.query(_League).order_by(_League.season_year.desc()).all()
    return templates.TemplateResponse("seasons.html", {
        "request": request, "league": league,
        "seasons": [
            {"fpl": lg.fpl_league_id, "season": lg.season_year, "name": lg.name,
             "is_current": lg.is_current, "phase": lg.phase}
            for lg in rows
        ],
    })


@router.get("/season/{fpl_league_id}", response_class=HTMLResponse)
def season_detail(fpl_league_id: str, request: Request, db: Session = Depends(get_db)):
    """Read-only summary of one season (standings, winnings, cups) — reuses the same
    read services with that season's league row."""
    season = services.resolve_league(db, fpl_league_id)
    if not season:
        raise HTTPException(status_code=404, detail="season not found")
    return templates.TemplateResponse("season_detail.html", {
        "request": request, "league": _league_or_404(db), "season_league": season,
        "season": season.season_year, "is_current": season.is_current,
        "standings": services.get_standings(db, season),
        "payouts": services.get_payouts(db, season),
        "cups": services.get_cups(db, season),
    })


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
    if not _feature_allowed(request, db, league, "trades_allowed"):
        return _locked_response("Trading")
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


@router.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        "transactions.html",
        {"request": request, "league": league, "weeks": services.get_transactions(db, league)},
    )


@router.get("/scoreboard", response_class=HTMLResponse)
def scoreboard_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    gw = request.query_params.get("gw")
    return templates.TemplateResponse("scoreboard.html", {
        "request": request, "league": league,
        "board": services.get_scoreboard(db, league, int(gw) if gw and gw.isdigit() else None),
    })


# ---- draft board ----
@router.get("/draft/{year}", response_class=HTMLResponse)
def draft_page(year: int, request: Request, draft_type: str = "main", db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse("draft.html", _board_ctx(request, db, league, year, draft_type))


@router.get("/draft/{year}/board", response_class=HTMLResponse)
def draft_board_partial(year: int, request: Request, draft_type: str = "main", db: Session = Depends(get_db)):
    """Board partial for the every-7s poll, so all devices see picks live."""
    league = _league_or_404(db)
    return templates.TemplateResponse("_board.html", _board_ctx(request, db, league, year, draft_type))


# ---- draft autodraft queue (manager) + admin approve ----
def _queue_ctx(request: Request, db: Session, league, year: int, draft_type: str) -> dict:
    fpl = current_manager_id(request)
    queue = (
        services.get_draft_queue(db, league, fpl, year, draft_type) if fpl else []
    )
    return {"request": request, "year": year, "draft_type": draft_type, "queue": queue,
            "current_fpl": fpl}


@router.get("/draft/{year}/queue", response_class=HTMLResponse)
def draft_queue_partial(year: int, request: Request, draft_type: str = "main", db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse("_queue.html", _queue_ctx(request, db, league, year, draft_type))


@router.post("/draft/{year}/queue/add", response_class=HTMLResponse)
def draft_queue_add(
    year: int, request: Request, player_fpl_id: int = Form(...),
    draft_type: str = Form("main"), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    fpl = current_manager_id(request)
    if not fpl:
        return _forbidden(request, "Log in to queue picks.")
    try:
        services.add_to_queue(db, league, fpl_manager_id=fpl, player_fpl_id=player_fpl_id,
                              season_year=year, draft_type=draft_type)
    except RuleViolation as e:
        return _err(e)
    return templates.TemplateResponse("_queue.html", _queue_ctx(request, db, league, year, draft_type))


@router.post("/draft/{year}/queue/remove", response_class=HTMLResponse)
def draft_queue_remove(
    year: int, request: Request, player_fpl_id: int = Form(...),
    draft_type: str = Form("main"), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    fpl = current_manager_id(request)
    if not fpl:
        return _forbidden(request, "Log in to manage your queue.")
    services.remove_from_queue(db, league, fpl_manager_id=fpl, player_fpl_id=player_fpl_id,
                              season_year=year, draft_type=draft_type)
    return templates.TemplateResponse("_queue.html", _queue_ctx(request, db, league, year, draft_type))


@router.post("/draft/{year}/approve-queued", response_class=HTMLResponse)
def draft_approve_queued(year: int, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    if not is_admin(request):
        return _forbidden(request, "Only the commissioner can approve a queued pick.")
    try:
        services.approve_queued_pick(db, league, season_year=year, draft_type="main")
    except RuleViolation as e:
        return _err(e)
    return _board_response(request, db, league, year)


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
    if not _feature_allowed(request, db, league, "draft_available"):
        return _locked_response("The draft")
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
    if not _feature_allowed(request, db, league, "draft_available"):
        return _locked_response("The draft")
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
    if not _feature_allowed(request, db, league, "draft_available"):
        return _locked_response("The draft")
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


# ---- discovery draft (snake, 2 picks/manager; gated by discovery_open) ----
def _discovery_ctx(request: Request, db: Session, league, year: int) -> dict:
    board = services.get_discovery_board(db, league, year)
    on_clock = services.next_open_pick(board)
    return {
        "request": request, "league": league, "year": year, "board": board,
        "on_clock": on_clock,
        "can_pick": bool(on_clock) and can_act_as(request, on_clock.get("owner_fpl")),
        "discovery_available": services.phase_context(db, league)["discovery_available"] or is_admin(request),
        "is_admin": is_admin(request),
    }


def _discovery_board_response(request, db, league, year):
    resp = templates.TemplateResponse("_discovery_board.html", _discovery_ctx(request, db, league, year))
    resp.headers["HX-Trigger"] = "discoveryChanged"
    return resp


@router.get("/discovery/{year}", response_class=HTMLResponse)
def discovery_page(year: int, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse("discovery.html", _discovery_ctx(request, db, league, year))


@router.get("/discovery/{year}/board", response_class=HTMLResponse)
def discovery_board_partial(year: int, request: Request, db: Session = Depends(get_db)):
    """Discovery board partial for the every-7s poll (live multi-device)."""
    league = _league_or_404(db)
    return templates.TemplateResponse("_discovery_board.html", _discovery_ctx(request, db, league, year))


@router.get("/discovery/{year}/queue", response_class=HTMLResponse)
def discovery_queue_partial(year: int, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse("_queue.html", _queue_ctx(request, db, league, year, "discovery"))


@router.post("/discovery/{year}/queue/add", response_class=HTMLResponse)
def discovery_queue_add(
    year: int, request: Request, player_fpl_id: int = Form(...), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    fpl = current_manager_id(request)
    if not fpl:
        return _forbidden(request, "Log in to queue picks.")
    try:
        services.add_to_queue(db, league, fpl_manager_id=fpl, player_fpl_id=player_fpl_id,
                              season_year=year, draft_type="discovery")
    except RuleViolation as e:
        return _err(e)
    return templates.TemplateResponse("_queue.html", _queue_ctx(request, db, league, year, "discovery"))


@router.post("/discovery/{year}/queue/remove", response_class=HTMLResponse)
def discovery_queue_remove(
    year: int, request: Request, player_fpl_id: int = Form(...), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    fpl = current_manager_id(request)
    if not fpl:
        return _forbidden(request, "Log in to manage your queue.")
    services.remove_from_queue(db, league, fpl_manager_id=fpl, player_fpl_id=player_fpl_id,
                              season_year=year, draft_type="discovery")
    return templates.TemplateResponse("_queue.html", _queue_ctx(request, db, league, year, "discovery"))


@router.post("/discovery/{year}/approve-queued", response_class=HTMLResponse)
def discovery_approve_queued(year: int, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    if not is_admin(request):
        return _forbidden(request, "Only the commissioner can approve a queued pick.")
    try:
        services.approve_queued_pick(db, league, season_year=year, draft_type="discovery")
    except RuleViolation as e:
        return _err(e)
    return _discovery_board_response(request, db, league, year)


@router.get("/discovery/{year}/search", response_class=HTMLResponse)
def discovery_search(year: int, request: Request, q: str = "", db: Session = Depends(get_db)):
    league = _league_or_404(db)
    results = (
        services.search_players(
            db, league, q=q.strip() or None, available_year=year,
            include_taken=True, draft_type="discovery", sort="points", limit=50,
        )
        if q.strip() else []
    )
    on_clock = services.next_open_pick(services.get_discovery_board(db, league, year))
    can_pick = bool(on_clock) and can_act_as(request, on_clock.get("owner_fpl"))
    return templates.TemplateResponse(
        "_discovery_results.html",
        {"request": request, "results": results, "year": year, "can_pick": can_pick},
    )


@router.post("/discovery/{year}/pick", response_class=HTMLResponse)
def discovery_pick(
    year: int, request: Request, player_fpl_id: int = Form(...),
    db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "discovery_available"):
        return _locked_response("The discovery draft")
    board = services.get_discovery_board(db, league, year)
    slot = services.next_open_pick(board)
    if slot and slot.get("owner_fpl"):
        if not can_act_as(request, slot["owner_fpl"]):
            return _forbidden(request, "It's not your discovery pick to make.")
        try:
            services.record_pick(
                db, league, season_year=year, pick_number=slot["pick"],
                owner_fpl=slot["owner_fpl"], player_fpl_id=player_fpl_id,
                draft_type="discovery", round=slot["round"],
            )
        except RuleViolation:
            pass
    return _discovery_board_response(request, db, league, year)
