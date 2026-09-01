"""Server-rendered UI (Jinja2 + HTMX). Public read; write routes need the
commissioner session login. Renders HTML and calls services.py directly."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

import draftprep
import services
from auth import (
    can_act_as,
    check_admin_password,
    current_manager_id,
    hash_password,
    is_admin,
    is_demo,
    is_owner,
    verify_password,
)
from db import get_db
from models import InjuryList, Manager, PlTeam
from rules import (
    GOALIE_TEAM_MODES,
    ROSTER_SIZE,
    RuleViolation,
    SEASON_LAST_GW,
    draft_picks_per_manager,
    goalie_teams_on,
)
from templating import templates

router = APIRouter()


def _league_or_404(db: Session):
    # is_current first — the rollover sets it, so a new season takes effect without a
    # redeploy. current_league() still falls back to FPL_DRAFT_LEAGUE_ID, then to the
    # only league row. Pinning to the env var meant a rollover looked like a no-op.
    league = services.current_league(db)
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
        "goalie_teams_on": goalie_teams_on(league.goalie_team_mode),
        # names the season the search panel's Pts column is showing, so the sort
        # option can't drift from what search_players actually sorts by
        "stats_season_label": services.season_label(services.stats_season(db, league)),
        "order_ctx": services.draft_order_context(db, league, year, draft_type),
    }


def _feature_allowed(
    request: Request, db: Session, league, flag: str, *, lock_attr: str = "writes_locked"
) -> bool:
    """Is a phase-gated feature available for a write? Admin (and the demo sandbox)
    always bypass; else the manual lock (`writes_locked`/`keepers_locked`) must be off
    AND the current phase must enable `flag` (see services.phase_context)."""
    if is_admin(request) or is_demo():
        return True
    if getattr(league, lock_attr, False):
        return False
    return bool(services.phase_context(db, league).get(flag, False))


def _condition_terms_from_form(
    db, *, metrics, manager_names, player_refs, comparisons, thresholds,
    season_years, notes, resolve_player,
) -> list[dict]:
    """Zip a condition sub-form's parallel arrays into term dicts.

    The sub-form repeats each field name once per term, so the browser posts seven
    equal-length lists rather than one object per term — the same repeated-key shape
    `/trade` already uses for `a_players[]`. A row whose metric is blank is a term the
    user added and left empty; it is dropped rather than validated, so an extra blank
    repeater row can never fail a submission.

    Every value arrives as a STRING (an empty input posts "", not null). Ints stay None
    when blank so the pure validator can say which field the chosen metric actually
    required, rather than this route guessing a default.

    `resolve_player` is passed in because the two call sites identify a player
    differently — the draft board by FPL element id, the corrections page by label.
    """
    def _opt(value, lo, hi, field):
        return _safe_int(value, lo, hi, field=field) if str(value or "").strip() else None

    def _at(seq, i):
        return seq[i] if i < len(seq) else ""

    out: list[dict] = []
    for i, metric in enumerate(metrics or []):
        metric = (metric or "").strip()
        if not metric:
            continue
        ref = (_at(player_refs, i) or "").strip()
        out.append(services.condition_term_from_flat(
            metric=metric,
            player_id=resolve_player(ref) if ref else None,
            manager_name=(_at(manager_names, i) or "").strip() or None,
            season_year=_opt(_at(season_years, i), 2000, 2100, "season"),
            comparison=(_at(comparisons, i) or "").strip() or None,
            threshold=_opt(_at(thresholds, i), 0, 10**6, "threshold"),
            note=(_at(notes, i) or "").strip() or None,
        ))
    return out


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


def _board_response(request, db, league, year, draft_type="main", *, error: str | None = None):
    """Render the board partial + tell HTMX the draft changed (so search refreshes).
    A refused pick (kept player, slot already made, ...) still renders this partial
    with `error` set, so the manager SEES why nothing changed instead of the board
    silently staying the same — htmx doesn't swap non-2xx responses, so a raised
    RuleViolation must reach the picker through a 200 like this one."""
    ctx = _board_ctx(request, db, league, year, draft_type)
    ctx["pick_error"] = error
    resp = templates.TemplateResponse(request, "_board.html", ctx)
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


def _viewer(request: Request) -> dict:
    """Who is looking, for services that redact per viewer (keeper selections are
    private until they lock). One token to splat, so it's hard to forget on a route
    and quietly fall back to showing nothing — or, worse, everything."""
    return {
        "viewer_fpl": current_manager_id(request),
        "viewer_is_admin": is_admin(request),
    }


# ---- "who are you?" gate + per-manager login ----
@router.get("/who", response_class=HTMLResponse)
def who(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    )
    return templates.TemplateResponse(request, "who.html", {
        "request": request, "league": league, "is_admin": is_admin(request), "hide_nav": True,
        "demo": is_demo(),
        "managers": [
            {"name": m.display, "fpl": m.fpl_manager_id, "needs_password": m.password_hash is None}
            for m in managers
        ],
    })


@router.post("/demo-login")
def demo_login(request: Request, db: Session = Depends(get_db), manager_id: str = Form(...)):
    """Demo sandbox only: log straight in as the chosen manager, no password. Returns
    404 in any non-demo environment so the live site always requires a password."""
    if not is_demo():
        raise HTTPException(status_code=404, detail="not found")
    league = _league_or_404(db)
    m = (
        db.query(Manager).filter_by(league_id=league.id, fpl_manager_id=str(manager_id)).one_or_none()
    )
    if not m:
        raise HTTPException(status_code=404, detail="manager not found")
    request.session.clear()
    request.session["manager_id"] = m.fpl_manager_id
    request.session["manager_name"] = m.display
    return RedirectResponse("/", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def manager_login_form(manager_id: str, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    m = (
        db.query(Manager).filter_by(league_id=league.id, fpl_manager_id=str(manager_id)).one_or_none()
    )
    if not m:
        raise HTTPException(status_code=404, detail="manager not found")
    return templates.TemplateResponse(request, "manager_login.html", {
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
        return templates.TemplateResponse(request, "manager_login.html", {
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
        return templates.TemplateResponse(request, "manager_login.html", {
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
        request, "login.html", {"request": request, "next": next, "error": None, "is_admin": is_admin(request)}
    )


@router.post("/admin/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if check_admin_password(password):
        request.session["admin"] = True
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"request": request, "next": next, "error": "Incorrect password", "is_admin": False},
        status_code=401,
    )


@router.get("/admin/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/who", status_code=303)


# ---- public league views ----
def _teams_data(db: Session, league, request: Request) -> list[dict]:
    """get_teams_in_progress while the draft is running or the new season hasn't
    synced any rosters yet (phase draft/preseason); get_keepers otherwise —
    unchanged for every other phase."""
    if league.phase in ("draft", "preseason"):
        return services.get_teams_in_progress(db, league)
    return services.get_keepers(db, league, **_viewer(request))


@router.get("/teams", response_class=HTMLResponse)
def teams_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        request, "teams.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "teams": _teams_data(db, league, request)},
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
    team = next(
        (t for t in _teams_data(db, league, request) if t["manager"] == m.display),
        None,
    )
    return templates.TemplateResponse(
        request, "team.html",
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
    return templates.TemplateResponse(request, "keepers_select.html", {
        "request": request, "league": league, "is_admin": is_admin(request),
        "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers],
        # The draft/keeper cycle this row is actually running — see
        # services._draft_year_for. Not season_year+1: once the draft data has been
        # migrated onto its own season's row, +1 names a draft that hasn't happened.
        "season": services._draft_year_for(league),
        "locked": not editable,
    })


@router.get("/keepers/candidates", response_class=HTMLResponse)
def keepers_candidates(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    fpl = request.query_params.get("fpl_manager_id")
    # A manager's own selection screen — and the ONLY protection on it. The response
    # carries their checked keepers and their off-roster discovery pick, neither of
    # which comes through _derive_keeper_status, so the redaction there doesn't reach
    # this route. Without the check any logged-in manager could read anyone's picks by
    # editing the query string.
    if fpl and not can_act_as(request, fpl):
        return _forbidden(request, "You can only view your own keeper options.")
    cands = services.keeper_candidates(db, league, fpl) if fpl else None
    return templates.TemplateResponse(
        request, "_keeper_candidates.html", {"request": request, "candidates": cands}
    )


@router.get("/keepers/discovery-search", response_class=HTMLResponse)
def keepers_discovery_search(request: Request, db: Session = Depends(get_db)):
    """Search ALL players for the discovery (bonus 6th) keeper slot — not limited
    to the manager's roster."""
    league = _league_or_404(db)
    q = (request.query_params.get("q") or "").strip()
    results = services.search_players(db, league, q=q, sort="points", limit=25) if q else []
    return templates.TemplateResponse(
        request, "_discovery_search.html", {"request": request, "results": results}
    )


@router.post("/keepers")
def keepers_submit(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), season_year: int = Form(...),
    keeper_fpl_ids: list[int] = Form(default=[]), discovery_fpl_id: str = Form(""),
    keeper_team_code: str = Form(""),
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
            keeper_team_code=(int(keeper_team_code) if keeper_team_code.strip()
                              else None),
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
    if not fpl:
        team = None
    elif league.phase in ("draft", "preseason"):
        team = services.get_my_team_in_progress(db, league, fpl)
    else:
        team = services.get_my_team(db, league, fpl)
    cur = services.current_gameweek(db, league)
    can_edit_il = bool(team) and (is_admin(request) or fpl == current_manager_id(request))
    manager = services._resolve_manager(db, league, fpl) if can_edit_il else None
    return templates.TemplateResponse(request, "my_team.html", {
        "request": request, "league": league, "team": team,
        # IL self-service controls (only when viewing your own team / admin)
        "can_edit_il": can_edit_il,
        "players": services.list_players(db, league),
        # Candidates for "he's already been dropped" -- a player this manager held
        # at some point this season but no longer does, and nobody else does either.
        "dropped_players": (
            services.dropped_players_for_manager(db, league, manager)
            if manager else []
        ),
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
    replacement_fpl_id: str = Form(...), start_gw: str = Form(""),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "gw_logic_active"):
        return _locked_response("The injury list")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only manage your own team's injury list.")
    cur = services.current_gameweek(db, league) or 1
    try:
        services.place_on_il(
            db, league, fpl_manager_id=fpl_manager_id,
            injured_fpl_id=_safe_int(injured_fpl_id, 1, 10_000_000, field="injured player"),
            replacement_fpl_id=_safe_int(replacement_fpl_id, 1, 10_000_000, field="replacement"),
            # Only the "he's already been dropped" case supplies this -- when he's
            # already on the roster, placement always starts now. Bounded at `cur`:
            # an injury can't be claimed before it happens.
            start_gw=_safe_int(start_gw, 1, cur, field="start GW") if start_gw.strip() else cur,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/my-team", status_code=303)


@router.post("/il/return")
def il_return(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), il_id: str = Form(...),
    released_fpl_id: str = Form(""),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "gw_logic_active"):
        return _locked_response("The injury list")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only manage your own team's injury list.")
    if not _il_entry_or_403(db, league, fpl_manager_id, il_id):
        return _forbidden(request, "That injury-list entry isn't yours.")
    try:
        # Only required at season end, when the frozen roster can't absorb the swap.
        services.return_from_il(
            db, league, il_id, services.current_gameweek(db, league) or SEASON_LAST_GW,
            released_fpl_id=_safe_int(released_fpl_id, 1, 10_000_000,
                                      field="released player")
            if released_fpl_id.strip() else None,
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
    start_gw: str = Form(""),
):
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "gw_logic_active"):
        return _locked_response("The international list")
    if not can_act_as(request, fpl_manager_id):
        return _forbidden(request, "You can only manage your own team's international list.")
    cur = services.current_gameweek(db, league) or 1
    try:
        services.place_on_intl(
            db, league, fpl_manager_id=fpl_manager_id,
            away_fpl_id=_safe_int(away_fpl_id, 1, 10_000_000, field="away player"),
            replacement_fpl_id=_safe_int(replacement_fpl_id, 1, 10_000_000, field="replacement"),
            start_gw=_safe_int(start_gw, 1, cur, field="start GW") if start_gw.strip() else cur,
            tournament=tournament or None,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/my-team", status_code=303)


@router.post("/intl/return")
def intl_return(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), intl_id: str = Form(...),
    released_fpl_id: str = Form(""),
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
            released_fpl_id=_safe_int(released_fpl_id, 1, 10_000_000,
                                      field="released player")
            if released_fpl_id.strip() else None,
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
    return templates.TemplateResponse(request, "my_team_upcoming.html", {
        "request": request, "league": league,
        "matchups": matchups, "me_name": me["manager"] if me else None,
    })


@router.get("/picks", response_class=HTMLResponse)
def picks_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    future_picks = services.get_future_picks(db, league)
    managers = sorted({
        name
        for season in future_picks
        for draft_type in ("main", "discovery")
        for p in season.get(draft_type, [])
        for name in (p["original_owner"], p["owner"])
    })
    return templates.TemplateResponse(
        request, "picks.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "future_picks": future_picks, "managers": managers},
    )


@router.post("/admin/sync/force")
def admin_force_sync(request: Request):
    """Session-authenticated twin of `POST /admin/sync?force=1` (that route is
    token-gated for the cron — a logged-in commissioner's browser session can't
    satisfy it). Calls the identical orchestration in sync.run_sync so the two
    paths can never drift apart."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    import sync as _sync

    try:
        _sync.run_sync(force=True)
    except _sync.LeagueIdentityError as e:
        return _err(str(e), status_code=409)
    return RedirectResponse("/admin/health", status_code=303)


@router.get("/admin/health", response_class=HTMLResponse)
def admin_health(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    )
    from rules import PHASES

    return templates.TemplateResponse(request, "admin_health.html", {
        "request": request, "league": league, "is_admin": True,
        "checks": services.data_health(db, league),
        "writes_locked": league.writes_locked,
        "keepers_locked": league.keepers_locked,
        "sync_locked": league.sync_locked,
        "goalie_team_mode": league.goalie_team_mode,
        "goalie_team_modes": GOALIE_TEAM_MODES,
        "goalie_team_picks": draft_picks_per_manager(league.goalie_team_mode),
        "goalie_team_clubs": db.query(PlTeam).filter_by(is_current_pl=True).count(),
        "phase_ctx": services.phase_context(db, league),
        "phase_manual": league.phase_manual,
        "discovery_open": league.discovery_open,
        "phases": PHASES,
        "managers": [
            {"name": m.display, "fpl": m.fpl_manager_id,
             "has_password": m.password_hash is not None,
             "discord_user_id": m.discord_user_id}
            for m in managers
        ],
        # Posters we've seen but can't identify. Each one is a real gap: every IL post
        # they write stages without a manager and needs one typed in by hand.
        "unmapped_discord": services.unmapped_discord_authors(db, league),
    })


@router.post("/admin/phase/draft")
def admin_phase_draft(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    # Start the draft FIRST and let it commit. The pool refresh below is a
    # best-effort nicety; a flaky FPL call must not leave the draft unstarted.
    services.enter_draft_phase(db, league)

    # Then pull a fresh pool: the weekly out-of-season drumbeat could otherwise
    # leave it six days stale at the exact moment people start picking. Network
    # lives in the route, not in services, per the two-truths boundary.
    import asyncio

    import sync as _sync

    try:
        asyncio.run(_sync.sync_players())
    except Exception as e:
        return _err(
            f"Draft started, but the player refresh failed: {e}. "
            "Run POST /admin/sync?force=1 before picking.",
            status_code=502,
        )
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
    return templates.TemplateResponse(request, "admin_season.html", {
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
    # Ordering matters here. sync_players is gated on a league being current AND
    # unfrozen, and it is what rekeys the pool on `code` and writes player_season.
    # Before the rollover no such league exists, so a full sync now would resolve
    # rosters through the OUTGOING season's element ids and leave the new league
    # with no player_season rows at all (blank squads everywhere). So: create the
    # league row first, flip it current, and only then run the full sync.
    import asyncio
    import sync as _sync

    # 1. create the new league row + managers + schedule only — no rosters yet.
    try:
        asyncio.run(_sync.sync_league_and_managers(fpl_league_id=new_id))
        asyncio.run(_sync.sync_gameweek_dates(fpl_league_id=new_id))
    except Exception as e:  # network / bad id
        return _err(f"sync of new league failed: {e}", status_code=502)
    new_league = services.resolve_league(db, new_id)
    if not new_league:
        return _err("new league did not sync (check the id)", status_code=502)
    # STOP HERE. Everything that follows needs to know which new manager is which
    # person, and nothing can work that out reliably: FPL reissues entry ids between
    # seasons and the team names change too. Deriving it silently is exactly what
    # broke the 26/27 rollover. Hand off to the mapping page.
    #
    # This intermediate state is safe and resumable: the new row exists but
    # `is_current` is still on the OLD row, so the site keeps working and abandoning
    # the rollover here changes nothing.
    return RedirectResponse(
        f"/admin/season/mapping?new={new_id}", status_code=303
    )


@router.get("/admin/season/mapping", response_class=HTMLResponse)
def admin_season_mapping(
    request: Request, db: Session = Depends(get_db), new: str = "",
):
    """Step 2 of the rollover: confirm which new manager is which person.

    Pre-filled with `services.suggest_manager_pairing`, which is a guess and is never
    applied on its own — the commissioner confirms every row.
    """
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/season", status_code=303)
    new_league = services.resolve_league(db, new.strip()) if new.strip() else None
    if not new_league:
        return _err("unknown new-season league id; start the rollover again")
    old_league = services.current_league(db)
    if not old_league or old_league.id == new_league.id:
        return _err("that league is already current")

    from models import Manager as _M

    suggested = services.suggest_manager_pairing(db, old_league, new_league)
    old_mgrs = db.query(_M).filter_by(league_id=old_league.id).order_by(_M.name).all()
    new_mgrs = db.query(_M).filter_by(league_id=new_league.id).order_by(_M.name).all()
    return templates.TemplateResponse(request, "admin_season_mapping.html", {
        "request": request, "league": old_league, "is_admin": True,
        "new_fpl": new_league.fpl_league_id,
        "new_season": new_league.season_year,
        "old_season": old_league.season_year,
        "old_managers": [
            {"id": str(m.id), "team": m.name, "person": m.display} for m in old_mgrs
        ],
        "rows": [
            {"id": str(m.id), "team": m.name,
             "suggested": str(suggested.get(m.id)) if suggested.get(m.id) else ""}
            for m in new_mgrs
        ],
        "unmatched": sum(1 for m in new_mgrs if not suggested.get(m.id)),
    })


@router.post("/admin/season/mapping")
async def admin_season_mapping_confirm(
    request: Request, db: Session = Depends(get_db),
):
    """Apply the confirmed mapping, then finish the rollover."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/season", status_code=303)
    form = await request.form()
    new_id = (form.get("new_fpl") or "").strip()
    force = bool(form.get("force"))
    new_league = services.resolve_league(db, new_id) if new_id else None
    if not new_league:
        return _err("unknown new-season league id; start the rollover again")
    old_league = services.current_league(db)
    if not old_league or old_league.id == new_league.id:
        return _err("that league is already current")

    # pair[<new manager uuid>] = <old manager uuid or "">
    pairing = {}
    for key, value in form.multi_items():
        if key.startswith("pair[") and key.endswith("]"):
            pairing[key[5:-1]] = (value or "").strip() or None
    dupes = [v for v in set(filter(None, pairing.values()))
             if list(pairing.values()).count(v) > 1]
    if dupes:
        return _err("each person may be mapped to only one new team")

    import asyncio
    import sync as _sync
    from models import Manager as _M

    by_uuid = {str(m.id): m.id for m in db.query(_M)}
    resolved = {
        by_uuid[k]: (by_uuid.get(v) if v else None)
        for k, v in pairing.items() if k in by_uuid
    }
    try:
        out = services.advance_season(
            db, old_league, new_league, pairing=resolved, force=force
        )
    except RuleViolation as e:
        return _err(e)
    # NOW a full sync: the first un-gated sync_players run rekeys the pool on `code`,
    # writes player_season for the new league, and resolves rosters against the new
    # season's element ids.
    try:
        # `await`, NOT asyncio.run — this route is async (it awaits request.form()),
        # and asyncio.run() inside a running event loop raises RuntimeError every
        # time. Caught by the rehearsal on a Neon branch: the rollover committed and
        # then ALWAYS returned 502 "post-rollover sync failed", so the new season
        # went current with no rosters or player_season until someone ran
        # /admin/sync?force=1 by hand. The sibling /admin/season/advance route is a
        # plain `def` and keeps asyncio.run correctly.
        await _sync.sync_all(fpl_league_id=new_id)
    except Exception as e:
        return _err(
            f"rollover completed but the post-rollover sync failed: {e}. "
            "Run POST /admin/sync?force=1 before using the site.",
            status_code=502,
        )
    # capture the draft-day pool from the REKEYED players table (see
    # services.snapshot_player_pool for why this can't live in advance_season).
    services.snapshot_player_pool(db, new_league)
    return RedirectResponse(
        f"/admin/season?carried={out['managers_carried']}"
        f"&seeded={out['keepers_seeded']}",
        status_code=303,
    )


@router.post("/admin/lock")
def admin_lock(
    request: Request, db: Session = Depends(get_db),
    lock: str = Form(""), keepers_lock: str = Form(""), sync_lock: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    league.writes_locked = lock == "on"
    league.keepers_locked = keepers_lock == "on"
    league.sync_locked = sync_lock == "on"
    db.commit()
    return RedirectResponse("/admin/health", status_code=303)


@router.post("/admin/goalie-team-mode")
def admin_goalie_team_mode(
    request: Request, db: Session = Depends(get_db), mode: str = Form(...),
):
    """Switch the goalie-team rule. Its own form, not folded into /admin/lock: the
    locks are transient operational switches and this changes the shape of a draft."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    try:
        services.set_goalie_team_mode(db, league, mode)
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/health", status_code=303)


@router.get("/admin/standings", response_class=HTMLResponse)
def admin_standings(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name).all()
    )
    return templates.TemplateResponse(request, "admin_standings.html", {
        "request": request, "league": league, "is_admin": True,
        "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers],
        "standings": services.get_standings(db, league),
        "adjustments": services.get_standing_adjustments(db, league),
        "fines": services.get_fines(db, league),
        "side_payouts": services.get_side_payouts(db, league),
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


@router.post("/admin/side-payouts/add")
def admin_add_side_payout(
    request: Request, db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...), label: str = Form(...), amount: str = Form(...),
    gameweek: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    try:
        services.add_side_payout(
            db, league, fpl_manager_id=fpl_manager_id, label=label,
            amount=_safe_int(amount, -100000, 100000, field="amount"),
            gameweek=_safe_int(gameweek, 1, 38, field="gameweek") if gameweek.strip() else None,
        )
    except (RuleViolation, ValueError) as e:
        return _err(e)
    return RedirectResponse("/admin/standings", status_code=303)


@router.post("/admin/side-payouts/delete")
def admin_delete_side_payout(
    request: Request, db: Session = Depends(get_db), side_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/standings", status_code=303)
    league = _league_or_404(db)
    try:
        services.delete_side_payout(db, league, side_id)
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
    return templates.TemplateResponse(request, "admin_cups.html", {
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


@router.get("/players", response_class=HTMLResponse)
def players_page(request: Request, db: Session = Depends(get_db)):
    """Every player + stat, sortable/filterable client-side, open to any logged-in
    manager or admin session. Projections are the one column group still owner-only
    (Tucker's per-manager identity, not the shared admin password — a co-commissioner
    with just the admin login still won't see them): player_portal's own
    viewer_is_owner default keeps that redaction in the service layer, not here."""
    if not current_manager_id(request) and not is_admin(request):
        return RedirectResponse("/login?next=/players", status_code=303)
    league = _league_or_404(db)
    owner = is_owner(request)
    players = services.player_portal(db, league, viewer_is_owner=owner)
    proj_year = services.projection_season_year(db) if owner else None
    return templates.TemplateResponse(request, "admin_players.html", {
        "request": request, "league": league, "is_admin": is_admin(request),
        "is_owner": owner,
        "players": players,
        # name the season the stats belong to, so it's never ambiguous on screen
        "stats_season_label": services.season_label(services.stats_season(db, league)),
        # None until the first projection import (or for a non-owner viewer); the
        # template hides the whole group on this one flag rather than rendering ten
        # em-dash columns for every player
        "projection_season_label": services.year_label(proj_year) if proj_year else None,
        "projection_count": sum(1 for p in players if p["proj_points"] is not None),
        "pool": services.player_pool_freshness(db),
    })


@router.get("/draft-prep", response_class=HTMLResponse)
def draft_prep(request: Request, db: Session = Depends(get_db)):
    """Owner-only draft preparation: predicted keepers, who that leaves available,
    and roughly when they go. Unlike /players, this stays owner-only end to end.

    Predictions are BLIND — nobody's submitted keepers are read, including the
    owner's. See services.draft_preparation.
    """
    if not is_owner(request):
        if current_manager_id(request):
            return _forbidden(request, "This page is restricted to the league owner.")
        return RedirectResponse("/login?next=/draft-prep", status_code=303)
    league = _league_or_404(db)
    year = services._draft_year_for(league)
    live_mode = services.keepers_revealed(league)
    if live_mode:
        prep = services.draft_preparation_live(db, league, year)
    else:
        prep = services.draft_preparation(db, league, year)

    me = _current_manager(request, db, league)
    mine, ledger = [], []
    if prep.get("available"):
        by_pick = {r["pick"]: r for r in prep["sim"]["picks"]}
        mine = [by_pick[s["pick"]] for s in prep["slots"]
                if me is not None and s["manager"] == me.id]
        held: dict = {}
        own: dict = {}
        for s in prep["slots"]:
            held[s["manager"]] = held.get(s["manager"], 0) + 1
            own[s["original"]] = own.get(s["original"], 0) + 1
        used: dict = {}
        lapsed: dict = {}
        for r in prep["sim"]["picks"]:
            if r["player"] is not None:
                used[r["manager"]] = used.get(r["manager"], 0) + 1
            elif r["reason"] == "forfeited":
                lapsed[r["manager"]] = lapsed.get(r["manager"], 0) + 1
        for mid, name in sorted(prep["names"].items(), key=lambda kv: kv[1]):
            keepers = len(prep["predictions"][mid]["keepers"])
            ledger.append({
                "manager": name, "keepers": keepers,
                "held": held.get(mid, 0),
                # net picks traded in or out — the ONLY thing that moves a squad off
                # 15, since your own slots are 15 - keepers and your keepers are
                # keepers, so the two always cancel
                "net": held.get(mid, 0) - own.get(mid, 0),
                "used": used.get(mid, 0), "lapsed": lapsed.get(mid, 0),
                "squad": len(prep["sim"]["squads"].get(mid, [])),
                "margin": prep["predictions"][mid]["margin"],
                "players": prep["predictions"][mid]["keepers"],
            })
    return templates.TemplateResponse(request, "draft_prep.html", {
        "request": request, "league": league, "is_admin": is_admin(request),
        "is_owner": True, "prep": prep, "year": year, "mine": mine,
        "ledger": ledger, "me": me.display if me else None,
        "live_mode": live_mode,
        # What a finished simulated squad should come to. The simulation only ever
        # holds the players it drafts, so under the goalie-team rule that's the
        # thirteen outfielders — the club is a slot, not a squad member.
        "squad_target": draftprep.shape_for(league.goalie_team_mode).squad_size,
    })


# ---- commissioner corrections (edit/delete historical records) ----
def _corrections_redirect():
    return RedirectResponse("/admin/corrections", status_code=303)


@router.get("/admin/corrections", response_class=HTMLResponse)
def admin_corrections(
    request: Request, db: Session = Depends(get_db), dq: str = "",
):
    """Fix historical records that are wrong: trades, imported discovery picks, and
    recorded draft picks. Every change is written to the audit log with the previous
    values, so a bad correction is traceable.

    `dq` is the manual player lookup for linking a discovery pick the matcher didn't
    solve. It goes through `search_players`, so it's accent-insensitive (unaccent on
    both sides) — typing "Sesko" finds "Šeško", which a browser-side `<datalist>`
    filter would not.
    """
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    dq = (dq or "").strip()
    return templates.TemplateResponse(request, "admin_corrections.html", {
        "request": request, "league": league, "is_admin": True,
        "dq": dq,
        "player_search": (
            services.search_players(db, league, q=dq, include_taken=True, limit=15)
            if dq else []
        ),
        **services.corrections_data(db, league),
    })


@router.post("/admin/corrections/trade/condition")
def admin_trade_condition(
    request: Request,
    db: Session = Depends(get_db),
    trade_id: str = Form(...),
    condition_logic: str = Form("all"),
    condition_effect: str = Form("escalate_round"),
    pick_round_if_met: str = Form(""),
    condition_metric: list[str] = Form(default=[]),
    condition_manager_name: list[str] = Form(default=[]),
    condition_player_name: list[str] = Form(default=[]),
    condition_comparison: list[str] = Form(default=[]),
    condition_threshold: list[str] = Form(default=[]),
    condition_season_year: list[str] = Form(default=[]),
    condition_note: list[str] = Form(default=[]),
):
    """Set or replace a pick trade's condition. Submitting with no term CLEARS it,
    which is the only way to make a conditional pick ordinary again.

    Unlike the draft board's form, this one names the condition's player subject by
    LABEL rather than FPL element id — the corrections page has no board context to
    resolve an id against, and `resolve_player_by_label` also finds a departed player,
    whose `fpl_id` is NULL.
    """
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        terms = _condition_terms_from_form(
            db, metrics=condition_metric, manager_names=condition_manager_name,
            player_refs=condition_player_name, comparisons=condition_comparison,
            thresholds=condition_threshold, season_years=condition_season_year,
            notes=condition_note,
            resolve_player=lambda ref: services.resolve_player_by_label(db, league, ref).id,
        )
        services.edit_trade(
            db, league, trade_id,
            set_condition=True,
            condition_logic=condition_logic or None,
            condition_effect=condition_effect or None,
            pick_round_if_met=_safe_int(
                pick_round_if_met, 1, ROSTER_SIZE, field="upgrade round"
            ) if str(pick_round_if_met).strip() else None,
            condition_terms=terms,
        )
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/discord/apply")
def admin_discord_apply(
    request: Request,
    db: Session = Depends(get_db),
    ingest_id: str = Form(...),
    replacement_fpl_id: str = Form(""),
    fpl_manager_id: str = Form(""),
    injured_fpl_id: str = Form(""),
    start_gw: str = Form(""),
):
    """Confirm a Discord proposal, with the reviewer's corrections layered on top.

    The overrides exist because a proposal is deliberately PARTIAL: an IL announcement
    never names the replacement player `place_on_il` requires, so that field is always
    supplied here rather than parsed. The others let a mis-resolved manager or player
    be fixed without going back to Discord.
    """
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        overrides = {
            "replacement_fpl_id": _safe_int(
                replacement_fpl_id, 1, 10**9, field="replacement"
            ) if replacement_fpl_id.strip() else None,
            "injured_fpl_id": _safe_int(
                injured_fpl_id, 1, 10**9, field="injured player"
            ) if injured_fpl_id.strip() else None,
            "start_gw": _safe_int(start_gw, 1, SEASON_LAST_GW, field="start gameweek")
            if start_gw.strip() else None,
            "fpl_manager_id": fpl_manager_id.strip() or None,
        }
        services.apply_discord_ingest(db, league, ingest_id, **overrides)
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/discord/reject")
def admin_discord_reject(
    request: Request, db: Session = Depends(get_db), ingest_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.reject_discord_ingest(db, league, ingest_id)
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/discord/map")
def admin_discord_map(
    request: Request,
    db: Session = Depends(get_db),
    fpl_manager_id: str = Form(...),
    discord_user_id: str = Form(""),
):
    """Bind a Discord account to a manager — the single highest-value piece of setup.

    With it, the AUTHOR of an IL post is a known manager at certainty 1.0 and no name
    matching happens at all. Submitting a blank id unmaps.
    """
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/health", status_code=303)
    league = _league_or_404(db)
    try:
        services.map_discord_author(
            db, league, fpl_manager_id=fpl_manager_id,
            discord_user_id=discord_user_id,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/health", status_code=303)


@router.post("/admin/corrections/trade/term-state")
def admin_condition_term_state(
    request: Request,
    db: Session = Depends(get_db),
    term_id: str = Form(...),
    manual_state: str = Form(""),
):
    """Rule on a `manual` condition term — the escape valve's other half.

    A manual term is the commissioner's own words, so only they can say whether it
    came true. Blank returns it to undecided, which resolves as pending rather than
    not_met, per the module-wide rule that an unanswered condition leaves the base
    round in force.
    """
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.set_condition_term_state(db, league, term_id, manual_state or None)
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/trade/edit")
def admin_trade_edit(
    request: Request, db: Session = Depends(get_db), trade_id: str = Form(...),
    from_fpl: str = Form(""), to_fpl: str = Form(""), event_gw: str = Form(""),
    conditions: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        gw = _safe_int(event_gw, 1, SEASON_LAST_GW, field="gameweek") if event_gw.strip() else None
        services.edit_trade(
            db, league, trade_id,
            from_fpl=from_fpl.strip() or None, to_fpl=to_fpl.strip() or None,
            event_gw=gw, conditions=conditions,
        )
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/trade/delete")
def admin_trade_delete(
    request: Request, db: Session = Depends(get_db), trade_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.delete_trade(db, league, trade_id)
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/discovery/edit")
def admin_discovery_edit(
    request: Request, db: Session = Depends(get_db), result_id: str = Form(...),
    manager_name: str = Form(""), player_name: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.edit_discovery_result(
            db, league, result_id,
            manager_name=manager_name, player_name=player_name,
        )
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/discovery/delete")
def admin_discovery_delete(
    request: Request, db: Session = Depends(get_db), result_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.delete_discovery_result(db, league, result_id)
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/pick/delete")
def admin_pick_delete(
    request: Request, db: Session = Depends(get_db), pick_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.delete_draft_pick(db, league, pick_id)
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/discovery/link")
def admin_discovery_link(
    request: Request, db: Session = Depends(get_db),
    season_year: str = Form(...), pick_number: str = Form(...),
    player_fpl_id: str = Form(...),
):
    """Attach a real player to a free-text discovery pick. Deliberately a human
    decision — see services.link_discovery_pick for why nothing auto-matches."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.link_discovery_pick(
            db, league,
            season_year=_safe_int(season_year, 2000, 2100, field="season"),
            pick_number=_safe_int(pick_number, 1, 999, field="pick number"),
            player_fpl_id=_safe_int(player_fpl_id, 1, 10_000_000, field="player id"),
        )
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/draft/link")
def admin_draft_pick_link(
    request: Request, db: Session = Depends(get_db),
    season_year: str = Form(...), pick_number: str = Form(...),
    player_name: str = Form(...),
):
    """Attach a real player to a free-text MAIN-draft pick.

    Named by LABEL rather than element id, unlike the discovery form: the fix for an
    unresolved pick is usually a player who was dropped before GW1, and the accent-aware
    picker is how a commissioner actually finds him. Deliberately a human decision — see
    services.link_draft_pick.
    """
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        player = services.resolve_player_by_label(db, league, player_name)
        services.link_draft_pick(
            db, league,
            season_year=_safe_int(season_year, 2000, 2100, field="season"),
            pick_number=_safe_int(pick_number, 1, 999, field="pick number"),
            player_fpl_id=player.fpl_id,
        )
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/discovery/match")
def admin_discovery_match_now(request: Request, db: Session = Depends(get_db)):
    """Run the matcher on demand. It also runs daily off the back of a full sync;
    this is for when you've just recorded a pick and don't want to wait a day."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    services.match_discovery_picks(db)
    return _corrections_redirect()


@router.post("/admin/corrections/discovery/suggestion/confirm")
def admin_discovery_suggestion_confirm(
    request: Request, db: Session = Depends(get_db),
    suggestion_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.confirm_discovery_suggestion(db, league, suggestion_id)
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/discovery/suggestion/reject")
def admin_discovery_suggestion_reject(
    request: Request, db: Session = Depends(get_db),
    suggestion_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.reject_discovery_suggestion(db, league, suggestion_id)
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.post("/admin/corrections/discovery/unlink")
def admin_discovery_unlink(
    request: Request, db: Session = Depends(get_db),
    season_year: str = Form(...), pick_number: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/corrections", status_code=303)
    league = _league_or_404(db)
    try:
        services.unlink_discovery_pick(
            db, league,
            season_year=_safe_int(season_year, 2000, 2100, field="season"),
            pick_number=_safe_int(pick_number, 1, 999, field="pick number"),
        )
    except RuleViolation as e:
        return _err(e)
    return _corrections_redirect()


@router.get("/admin/keepers", response_class=HTMLResponse)
def admin_keepers(request: Request, db: Session = Depends(get_db)):
    """Correct a player's derived keeper facts. Acquisition drives the =<2 waiver
    keeper cap and the derivation calls any unexplained roster gap a drop, so a
    missing injury-list record can quietly cost a manager a waiver slot."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/keepers", status_code=303)
    league = _league_or_404(db)
    return templates.TemplateResponse(request, "admin_keepers.html", {
        "request": request, "league": league, "is_admin": True,
        "roster_gaps": services.unexplained_roster_gaps(db, league),
        **services.keeper_overrides_context(db, league),
    })


@router.post("/admin/keepers/override")
def admin_keeper_override(
    request: Request, db: Session = Depends(get_db), fpl_manager_id: str = Form(...),
    player_fpl_id: str = Form(...), acquisition: str = Form(""),
    years_remaining: str = Form(""),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/keepers", status_code=303)
    league = _league_or_404(db)
    try:
        yrs = _safe_int(years_remaining, 0, 4, field="years remaining") \
            if years_remaining.strip() else None
        services.set_keeper_override(
            db, league, fpl_manager_id=fpl_manager_id,
            player_fpl_id=_safe_int(player_fpl_id, 1, 10_000_000, field="player"),
            acquisition=acquisition.strip() or None, years_remaining=yrs,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/keepers", status_code=303)


@router.post("/admin/keepers/clear")
def admin_keeper_override_clear(
    request: Request, db: Session = Depends(get_db), fpl_manager_id: str = Form(...),
    player_fpl_id: str = Form(...),
):
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/keepers", status_code=303)
    league = _league_or_404(db)
    try:
        services.clear_keeper_override(
            db, league, fpl_manager_id=fpl_manager_id,
            player_fpl_id=_safe_int(player_fpl_id, 1, 10_000_000, field="player"),
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/keepers", status_code=303)


@router.post("/admin/keepers/il-backfill")
def admin_il_backfill(
    request: Request, db: Session = Depends(get_db), fpl_manager_id: str = Form(...),
    injured_fpl_id: str = Form(...), replacement_fpl_id: str = Form(...),
    start_gw: str = Form(...),
):
    """Commissioner-only: enter a HISTORICAL injury-list placement (e.g. for a
    prior season with no IL records at all, per CLAUDE.md's documented caveat).
    Unlike the manager self-service /il/place, this takes an explicit start_gw
    and isn't gated on the in-season phase — a past season's fact doesn't wait
    for gw_logic_active. Reuses services.place_on_il; it already accepts an arbitrary
    start_gw, and `require_roster=False` waives the "is he actually yours" check that
    manager self-service enforces — a historical placement is precisely the case the
    roster cannot confirm, because the snapshot shows the replacement in his slot."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/keepers", status_code=303)
    league = _league_or_404(db)
    try:
        services.place_on_il(
            db, league, fpl_manager_id=fpl_manager_id, require_roster=False,
            injured_fpl_id=_safe_int(injured_fpl_id, 1, 10_000_000, field="injured player"),
            replacement_fpl_id=_safe_int(replacement_fpl_id, 1, 10_000_000, field="replacement"),
            start_gw=_safe_int(start_gw, 1, SEASON_LAST_GW, field="start GW"),
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/admin/keepers", status_code=303)


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request, db: Session = Depends(get_db)):
    """Commissioner audit log: every team-affecting action (who/what/when),
    sortable/filterable client-side. Any admin can view."""
    if not is_admin(request):
        return RedirectResponse("/admin/login?next=/admin/audit", status_code=303)
    league = _league_or_404(db)
    return templates.TemplateResponse(request, "audit.html", {
        "request": request, "league": league, "is_admin": True,
        "entries": services.get_audit_log(db, league),
    })


@router.get("/cups", response_class=HTMLResponse)
def cups_page(request: Request, db: Session = Depends(get_db)):
    """Public, read-only cup brackets."""
    league = _league_or_404(db)
    return templates.TemplateResponse(request, "cups.html", {
        "request": request, "league": league, "cups": services.get_cups(db, league),
        "shield": services.get_shield(db, league),
    })


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(
        request, "history.html",
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
    return templates.TemplateResponse(request, "seasons.html", {
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
    return templates.TemplateResponse(request, "season_detail.html", {
        "request": request, "league": _league_or_404(db), "season_league": season,
        "season": season.season_year, "is_current": season.is_current,
        "standings": services.get_standings(db, season),
        # Both tables below are ordered by the ADJUSTED standings, so without the log
        # an archived season can differ from FPL's official final table — and now from
        # the money too — with nothing on the page saying why.
        "adjustments": services.get_standing_adjustments(db, season),
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
        request, "trade.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "managers": [{"name": m.display, "fpl": m.fpl_manager_id} for m in managers]},
    )


@router.get("/trade/assets/{side}", response_class=HTMLResponse)
def trade_assets(side: str, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    fpl = request.query_params.get(f"{side}_manager")
    assets = services.manager_assets(db, league, fpl) if fpl else None
    return templates.TemplateResponse(
        request, "_trade_assets.html", {"request": request, "side": side, "assets": assets}
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
    a_clubs: list[str] = Form(default=[]),
    b_clubs: list[str] = Form(default=[]),
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
            a_clubs=a_clubs, b_clubs=b_clubs,
        )
    except RuleViolation as e:
        return _err(e)
    return RedirectResponse("/trades", status_code=303)


@router.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    trades = services.get_trades(db)
    # Filter option lists, derived from what's actually on the page (client-side
    # filtering only — no new endpoints).
    seasons = [season["year"] for season in trades]
    managers = sorted({
        name for season in trades for row in season["trades"]
        for name in (row["from"], row["to"]) if name
    })
    return templates.TemplateResponse(
        request, "trades.html",
        {"request": request, "league": league, "is_admin": is_admin(request),
         "trades": trades, "seasons": seasons, "managers": managers,
         "trade_notes": services.get_trade_notes(db, league)},
    )


@router.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    seasons_data = services.get_all_transactions(db)
    seasons = [season["year"] for season in seasons_data]
    managers = sorted({
        move["manager"]
        for season in seasons_data for week in season["weeks"] for move in week["moves"]
    })
    return templates.TemplateResponse(
        request, "transactions.html",
        {"request": request, "league": league, "seasons_data": seasons_data,
         "seasons": seasons, "managers": managers,
         "window": services.waiver_window(db, league)},
    )


@router.get("/scoreboard", response_class=HTMLResponse)
def scoreboard_page(request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    gw = request.query_params.get("gw")
    return templates.TemplateResponse(request, "scoreboard.html", {
        "request": request, "league": league,
        "board": services.get_scoreboard(db, league, int(gw) if gw and gw.isdigit() else None),
    })


# ---- draft board ----
@router.get("/draft/{year}", response_class=HTMLResponse)
def draft_page(year: int, request: Request, draft_type: str = "main", db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(request, "draft.html", _board_ctx(request, db, league, year, draft_type))


@router.get("/draft/{year}/board", response_class=HTMLResponse)
def draft_board_partial(year: int, request: Request, draft_type: str = "main", db: Session = Depends(get_db)):
    """Board partial for the every-7s poll, so all devices see picks live."""
    league = _league_or_404(db)
    return templates.TemplateResponse(request, "_board.html", _board_ctx(request, db, league, year, draft_type))


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
    return templates.TemplateResponse(request, "_queue.html", _queue_ctx(request, db, league, year, draft_type))


@router.post("/draft/{year}/queue/add", response_class=HTMLResponse)
def draft_queue_add(
    year: int, request: Request, player_fpl_id: int | None = Form(None),
    team_code: int | None = Form(None),
    draft_type: str = Form("main"), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    fpl = current_manager_id(request)
    if not fpl:
        return _forbidden(request, "Log in to queue picks.")
    try:
        services.add_to_queue(db, league, fpl_manager_id=fpl, player_fpl_id=player_fpl_id,
                              team_code=team_code, season_year=year, draft_type=draft_type)
    except RuleViolation as e:
        return _err(e)
    return templates.TemplateResponse(request, "_queue.html", _queue_ctx(request, db, league, year, draft_type))


@router.post("/draft/{year}/queue/remove", response_class=HTMLResponse)
def draft_queue_remove(
    year: int, request: Request, player_fpl_id: int | None = Form(None),
    team_code: int | None = Form(None),
    draft_type: str = Form("main"), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    fpl = current_manager_id(request)
    if not fpl:
        return _forbidden(request, "Log in to manage your queue.")
    try:
        services.remove_from_queue(db, league, fpl_manager_id=fpl, player_fpl_id=player_fpl_id,
                                   team_code=team_code, season_year=year, draft_type=draft_type)
    except RuleViolation as e:
        return _err(e)
    return templates.TemplateResponse(request, "_queue.html", _queue_ctx(request, db, league, year, draft_type))


@router.post("/draft/{year}/queue/reorder", response_class=HTMLResponse)
def draft_queue_reorder(
    year: int, request: Request, order: str = Form(...),
    draft_type: str = Form("main"), db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    fpl = current_manager_id(request)
    if not fpl:
        return _forbidden(request, "Log in to manage your queue.")
    keys = [k.strip() for k in order.split(",") if k.strip()]
    try:
        services.reorder_queue(db, league, fpl_manager_id=fpl, ordered_keys=keys,
                               season_year=year, draft_type=draft_type)
    except RuleViolation as e:
        return _err(e)
    return templates.TemplateResponse(request, "_queue.html", _queue_ctx(request, db, league, year, draft_type))


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


def _search_viewer_id(request: Request, db: Session, league, year: int, me):
    """Whose 'you already have a goalie team' the club search should reflect.

    A manager sees their own. The commissioner drafts ON BEHALF of whoever is on the
    clock, so theirs is that manager's — using the admin's own would grey out clubs
    for a team that isn't picking.
    """
    if not is_admin(request):
        return me.id if me else None
    slot = services.next_open_pick(services.get_draft_board(db, league, year))
    owner_fpl = slot.get("owner_fpl") if slot else None
    if not owner_fpl:
        return None
    owner = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(owner_fpl))
        .one_or_none()
    )
    return owner.id if owner else None


@router.get("/draft/{year}/search", response_class=HTMLResponse)
def draft_search(
    year: int, request: Request, q: str = "", position: str = "", sort: str = "",
    db: Session = Depends(get_db),
):
    league = _league_or_404(db)
    results = []
    if q.strip() or position or sort:
        # Keeper selections are private until they lock, so "kept: X" would otherwise
        # leak them here — this page is reachable in the offseason. Drafting can't
        # happen before the reveal (draft_available is true only in PHASE_DRAFT, which
        # forces keepers_editable false), so nobody can pick a hidden keeper.
        me = _current_manager(request, db, league)
        results = services.search_players(
            db, league, q=q.strip() or None, position=position or None,
            sort=sort or None, available_year=year, include_taken=True,
            kept_for={me.id} if me else None,
            kept_all=is_admin(request) or services.keepers_revealed(league),
            # Greys out every club for a manager who already has a goalie team,
            # mirroring the rule record_pick enforces. For the admin, who acts for
            # whoever is on the clock rather than as themselves, that would grey out
            # the board on somebody else's behalf — so resolve it from the slot.
            for_manager_id=_search_viewer_id(request, db, league, year, me),
            limit=50,
        )
    on_clock = services.next_open_pick(services.get_draft_board(db, league, year))
    can_pick = bool(on_clock) and can_act_as(request, on_clock.get("owner_fpl"))
    return templates.TemplateResponse(
        request, "_search_results.html", {"request": request, "results": results, "year": year,
                                 "is_admin": is_admin(request), "can_pick": can_pick}
    )


@router.post("/draft/{year}/pick", response_class=HTMLResponse)
def draft_pick(
    year: int, request: Request, player_fpl_id: int | None = Form(None),
    team_code: int | None = Form(None),
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
    error = None
    if slot and slot.get("owner_fpl"):
        if not can_act_as(request, slot["owner_fpl"]):
            return _forbidden(request, "It's not your pick to make.")
        try:
            services.record_pick(
                db, league, season_year=year, pick_number=slot["pick"],
                owner_fpl=slot["owner_fpl"], player_fpl_id=player_fpl_id,
                team_code=team_code, round=slot["round"],
                # An admin may correct a slot that's already been made. For anyone
                # else record_pick still refuses, so a live draft can't be overwritten
                # by a double-click or a stale board.
                overwrite=is_admin(request),
            )
        except RuleViolation as e:
            error = str(e)
    return _board_response(request, db, league, year, error=error)


@router.post("/draft/{year}/trade-pick", response_class=HTMLResponse)
def draft_trade_pick(
    year: int, request: Request, pick: str = Form(...), to_fpl: str = Form(...),
    draft_type: str = Form("main"),
    condition_logic: str = Form("all"),
    condition_effect: str = Form("escalate_round"),
    pick_round_if_met: str = Form(""),
    conditions: str = Form(""),
    condition_metric: list[str] = Form(default=[]),
    condition_manager_name: list[str] = Form(default=[]),
    condition_player_fpl_id: list[str] = Form(default=[]),
    condition_comparison: list[str] = Form(default=[]),
    condition_threshold: list[str] = Form(default=[]),
    condition_season_year: list[str] = Form(default=[]),
    condition_note: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """`pick` is the combined "<original_fpl>:<round>" slot id; the current holder
    (from) is derived from live pick ownership so the form only needs pick + to.

    The condition fields are all optional and commissioner-only. They arrive as
    parallel STRING arrays, one entry per term, and are passed through only when at
    least one term named a metric — omission has to behave exactly as it did before
    this feature existed.
    """
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
    cond: dict = {}
    if any((m or "").strip() for m in condition_metric):
        # Commissioner-only: a condition binds a future season's result to a pick, and
        # the two managers in the trade are not the only people it affects.
        if not is_admin(request):
            return _forbidden(request, "Only the commissioner can make a pick conditional.")
        try:
            terms = _condition_terms_from_form(
                db, metrics=condition_metric, manager_names=condition_manager_name,
                player_refs=condition_player_fpl_id, comparisons=condition_comparison,
                thresholds=condition_threshold, season_years=condition_season_year,
                notes=condition_note,
                resolve_player=lambda ref: services._resolve_player(
                    db, _safe_int(ref, 1, 10**9, field="condition player")
                ).id,
            )
            cond = {
                "condition_logic": condition_logic or None,
                "condition_effect": condition_effect or None,
                "pick_round_if_met": _safe_int(
                    pick_round_if_met, 1, ROSTER_SIZE, field="upgrade round"
                ) if str(pick_round_if_met).strip() else None,
                "condition_terms": terms,
                "conditions": conditions or None,
            }
        except RuleViolation as e:
            return _err(e)
    try:
        services.trade_pick(
            db, league, from_fpl=from_fpl, to_fpl=to_fpl, original_fpl=original_fpl,
            round=round, season_year=year, draft_type=draft_type, **cond,
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


@router.post("/draft/{year}/order-later", response_class=HTMLResponse)
def draft_set_order_later(
    year: int, request: Request, order: str = Form(...), round: str = Form(""),
    db: Session = Depends(get_db),
):
    """Set the pick order for rounds 2+. Empty `round` = the base order used by every
    round from 2 on; a number overrides that round only. Round 1 is set separately."""
    league = _league_or_404(db)
    if not is_admin(request):
        return _forbidden(request, "Only the commissioner can set the draft order.")
    ids = [s.strip() for s in order.split(",") if s.strip()]
    try:
        rnd = _safe_int(round, 2, ROSTER_SIZE, field="round") if round.strip() else None
        services.set_draft_order_override(db, league, year, ids, round=rnd)
    except RuleViolation as e:
        return _err(e)
    return _board_response(request, db, league, year)


@router.post("/draft/{year}/order-revert", response_class=HTMLResponse)
def draft_revert_order(
    year: int, request: Request, round: str = Form(""), db: Session = Depends(get_db),
):
    """Drop an override so the round goes back to following the standings."""
    league = _league_or_404(db)
    if not is_admin(request):
        return _forbidden(request, "Only the commissioner can set the draft order.")
    try:
        rnd = _safe_int(round, 2, ROSTER_SIZE, field="round") if round.strip() else None
        services.clear_draft_order_override(db, league, year, round=rnd)
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
    resp = templates.TemplateResponse(request, "_discovery_board.html", _discovery_ctx(request, db, league, year))
    resp.headers["HX-Trigger"] = "discoveryChanged"
    return resp


@router.get("/discovery/{year}", response_class=HTMLResponse)
def discovery_page(year: int, request: Request, db: Session = Depends(get_db)):
    league = _league_or_404(db)
    return templates.TemplateResponse(request, "discovery.html", _discovery_ctx(request, db, league, year))


@router.get("/discovery/{year}/board", response_class=HTMLResponse)
def discovery_board_partial(year: int, request: Request, db: Session = Depends(get_db)):
    """Discovery board partial for the every-7s poll (live multi-device)."""
    league = _league_or_404(db)
    return templates.TemplateResponse(request, "_discovery_board.html", _discovery_ctx(request, db, league, year))


@router.post("/discovery/{year}/pick", response_class=HTMLResponse)
def discovery_pick(
    year: int, request: Request, player_name: str = Form(...),
    db: Session = Depends(get_db),
):
    """Discovery picks are players NOT in the league (future PL arrivals) — recorded as
    a free-text name, not a player search."""
    league = _league_or_404(db)
    if not _feature_allowed(request, db, league, "discovery_available"):
        return _locked_response("The discovery draft")
    board = services.get_discovery_board(db, league, year)
    slot = services.next_open_pick(board)
    if slot and slot.get("owner_fpl"):
        if not can_act_as(request, slot["owner_fpl"]):
            return _forbidden(request, "It's not your discovery pick to make.")
        try:
            services.record_discovery_pick(
                db, league, season_year=year, pick_number=slot["pick"],
                owner_fpl=slot["owner_fpl"], player_name=player_name, round=slot["round"],
            )
        except RuleViolation as e:
            return _err(e)
    return _discovery_board_response(request, db, league, year)
