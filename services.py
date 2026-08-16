"""Read-only query helpers serving PRECOMPUTED data from our tables.

Per the architecture (CLAUDE.md), these never call the FPL API — they read the
synced/normalized rows. Shared by the JSON API (api.py) and the homepage
(main.py) so both render the same data.
"""

import os

from sqlalchemy import func
from sqlalchemy.orm import Session

import draftprep
from audit import current_actor
from models import (
    AuditLog,
    DraftLottery,
    DraftPick,
    Fixture,
    Gameweek,
    GameweekPoints,
    InjuryList,
    KeeperSeed,
    KeeperSelection,
    League,
    Manager,
    Match,
    Standing,
    Player,
    PlayerProjection,
    PlayerSeason,
    PlTeam,
    Roster,
    Tournament,
    TournamentMatch,
    Trade,
)
from rules import (
    ANTI_TANKING_MIN_WEEKS,
    ANTI_TANKING_MIN_ZERO_PLAYERS,
    CUP_SEED_THROUGH_GW,
    CUP_SIZE,
    CUP_START_GW,
    DISCOVERY_OPEN_DAY,
    DISCOVERY_OPEN_MONTH,
    KEEPER_FRESH_DRAFT,
    KEEPER_FRESH_WAIVER,
    MIN_IL_STAY_GWS,
    PAYOUT_STRUCTURE,
    PHASE_IN_SEASON,
    PHASE_OFFSEASON,
    PHASE_PRESEASON,
    RuleViolation,
    SEASON_LAST_GW,
    TRADE_DEADLINE_DAY,
    TRADE_DEADLINE_MONTH,
    LIVE_FIXTURE_WINDOW_HOURS,
    compute_payouts,
    current_tanking_streak,
    decide_sync,
    keepers_revealed as _keepers_revealed_rule,
    next_phase,
    phase_features,
    h2h_standings,
    il_can_return,
    il_same_position,
    GOALIE_TEAM_MODES,
    draft_picks_per_manager,
    generate_draft_slots,
    goalie_team_keepable,
    goalie_teams_on,
    keeper_eligible,
    keeper_status,
    match_winner,
    tanking_windows,
    validate_keeper_selection,
    zero_minute_count,
)


def resolve_league(db: Session, league_key: str) -> League | None:
    """Look up a league by its FPL league id (the public, stable identifier)."""
    return db.query(League).filter_by(fpl_league_id=str(league_key)).one_or_none()


def current_league(db: Session) -> League | None:
    """The active season's league row. Prefers the `is_current` flag (set by the
    advance-season rollover, so no env redeploy is needed); falls back to the
    FPL_DRAFT_LEAGUE_ID env, then to the only league row if there's just one."""
    from settings import LEAGUE_ID

    lg = db.query(League).filter_by(is_current=True).first()
    if lg:
        return lg
    if LEAGUE_ID:
        lg = resolve_league(db, LEAGUE_ID)
        if lg:
            return lg
    rows = db.query(League).all()
    return rows[0] if len(rows) == 1 else None


def latest_gameweek(db: Session, league: League) -> Gameweek | None:
    return (
        db.query(Gameweek)
        .filter_by(league_id=league.id)
        .order_by(Gameweek.number.desc())
        .first()
    )


# ---- audit log ----
def record_audit(
    db: Session,
    league: League,
    *,
    action: str,
    summary: str,
    manager_ids: list | None = None,
    details: dict | None = None,
) -> None:
    """Append an audit entry for a team-affecting action. Adds to the session
    only (NO commit) so it shares the caller's transaction and is atomic with the
    change — call it immediately before the caller's `db.commit()`. The acting
    identity comes from the request-scoped ContextVar (audit.current_actor), so
    callers don't thread an actor argument; unset (scripts/cron) → system."""
    actor, kind = current_actor()
    db.add(
        AuditLog(
            league_id=league.id,
            actor=actor,
            actor_kind=kind,
            action=action,
            summary=summary,
            manager_ids=[str(m) for m in manager_ids] if manager_ids else None,
            details=details,
        )
    )


def get_audit_log(
    db: Session,
    league: League,
    *,
    action: str | None = None,
    manager_fpl_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Audit entries newest-first, with affected-team names resolved. Filters by
    action and/or by an affected manager (matched in Python on the manager_ids
    list for Postgres/sqlite portability)."""
    names = {
        str(m.id): m.display
        for m in db.query(Manager).filter_by(league_id=league.id)
    }
    target_id = None
    if manager_fpl_id:
        mgr = (
            db.query(Manager)
            .filter_by(league_id=league.id, fpl_manager_id=str(manager_fpl_id))
            .one_or_none()
        )
        target_id = str(mgr.id) if mgr else "__none__"
    q = db.query(AuditLog).filter_by(league_id=league.id)
    if action:
        q = q.filter(AuditLog.action == action)
    rows = q.order_by(AuditLog.created_at.desc()).limit(max(1, min(limit, 2000))).all()
    out = []
    for r in rows:
        mids = r.manager_ids or []
        if target_id is not None and target_id not in mids:
            continue
        out.append({
            "id": str(r.id),
            "when": r.created_at.isoformat() if r.created_at else None,
            "actor": r.actor,
            "actor_kind": r.actor_kind,
            "action": r.action,
            "summary": r.summary,
            "teams": [names.get(m, "—") for m in mids],
            "details": r.details,
        })
    return out


DEMO_DEFAULT_GW = 19  # demo: a mid-season GW so Upcoming/Scores have data to show


def _derived_current_gameweek(db: Session, league: League) -> int | None:
    """The GW we're 'on' — derived from stored data only (no live FPL call, per
    the two-truths boundary): the latest GW whose window has started (start_date <=
    today), else the latest GW that has points data, else None."""
    import datetime as _dt

    today = _dt.date.today()
    started = [
        g.number
        for g in db.query(Gameweek).filter_by(league_id=league.id)
        if g.start_date and g.start_date <= today
    ]
    if started:
        return max(started)
    gp = (
        db.query(GameweekPoints, Gameweek)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(Gameweek.league_id == league.id)
        .all()
    )
    return max((gw.number for _p, gw in gp), default=None)


def current_gameweek(db: Session, league: League) -> int | None:
    """Current GW (see `_derived_current_gameweek`). In the demo sandbox, if the data
    has no upcoming GWs (e.g. a copy of a finished season), pretend we're mid-season so
    Upcoming/Scores populate from the real matches + fixtures already in the copy —
    overridable with DEMO_CURRENT_GW. Live/prod is unaffected."""
    real = _derived_current_gameweek(db, league)
    from auth import is_demo

    if is_demo():
        env = os.getenv("DEMO_CURRENT_GW")
        if env and env.isdigit():
            return int(env)
        if real is None or real >= SEASON_LAST_GW - 3:
            return DEMO_DEFAULT_GW
    return real


def waiver_window(db: Session, league: League) -> dict | None:
    """Current add/drop window: waivers run from a GW's start until 24h before the next
    GW's deadline; the final 24h before that deadline is free agency. Derived from the
    stored GW deadline dates (date-granular). None outside the in-season window.
    (Add/drops happen in FPL itself; this surfaces which window the league is in.)"""
    import datetime as _dt

    cur = current_gameweek(db, league)
    if cur is None:
        return None
    nxt = (
        db.query(Gameweek).filter_by(league_id=league.id, number=cur + 1).one_or_none()
    )
    next_deadline = nxt.start_date if nxt else None
    if not next_deadline:
        return {"state": "between", "next_deadline": None, "next_gw": None}
    today = _dt.date.today()
    fa_from = next_deadline - _dt.timedelta(days=1)
    state = "free_agency" if today >= fa_from else "waivers"
    return {
        "state": state,
        "label": "Free agency (final 24h)" if state == "free_agency" else "Waivers open",
        "next_deadline": next_deadline.isoformat(),
        "next_gw": cur + 1,
    }


def get_scoreboard(db: Session, league: League, gw_number: int | None = None) -> dict:
    """Current-GW H2H scoreboard: each matchup with live scores (from gameweek_points,
    falling back to the match's stored points) and whether it's finished."""
    gw_number = gw_number or current_gameweek(db, league)
    if gw_number is None:
        return {"gameweek": None, "matches": []}
    gw = (
        db.query(Gameweek).filter_by(league_id=league.id, number=gw_number).one_or_none()
    )
    if not gw:
        return {"gameweek": gw_number, "matches": []}
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    live = {
        gp.manager_id: gp.total_points
        for gp in db.query(GameweekPoints).filter_by(gameweek_id=gw.id)
    }
    matches = []
    for mt in db.query(Match).filter_by(league_id=league.id, gameweek_id=gw.id):
        hs = live.get(mt.home_manager_id, mt.home_points)
        as_ = live.get(mt.away_manager_id, mt.away_points)
        matches.append({
            "home": names.get(mt.home_manager_id),
            "away": names.get(mt.away_manager_id),
            "home_score": hs, "away_score": as_,
            "finished": bool(mt.finished),
            "leader": (names.get(mt.home_manager_id) if (hs or 0) > (as_ or 0)
                       else names.get(mt.away_manager_id) if (as_ or 0) > (hs or 0) else None),
        })
    matches.sort(key=lambda x: (x["home"] or ""))
    return {"gameweek": gw_number, "matches": matches}


def gw_finished(db: Session, league: League, number: int) -> bool:
    """Has gameweek `number` finished? (any finished H2H match in that GW)."""
    return (
        db.query(Match)
        .join(Gameweek, Gameweek.id == Match.gameweek_id)
        .filter(
            Gameweek.league_id == league.id,
            Gameweek.number == number,
            Match.finished.is_(True),
        )
        .first()
        is not None
    )


def _phase_label(macro: str, discovery_open: bool, trades_off: bool, cups: bool) -> str:
    if macro == "offseason":
        return "Off-season"
    if macro == "draft":
        return "Draft"
    if macro == "preseason":
        return "Pre-season"
    # in_season sub-states (stack)
    if discovery_open:
        return "In season — discovery draft"
    if cups:
        return "Cup season"
    if trades_off:
        return "In season — post trade deadline"
    return "In season"


def phase_context(db: Session, league: League) -> dict:
    """The league's current phase + derived feature flags (the single source the
    UI/routes consult). Macro phase is stored on the league; the in-season
    sub-state (trades-off after Feb 1, cups after GW28, discovery window) is derived
    from the calendar/GW so it can't drift from reality."""
    import datetime as _dt

    macro = league.phase or PHASE_OFFSEASON
    today = _dt.date.today()
    sy = league.season_year or today.year
    # Trade deadline is Feb 1 of the year the season ENDS (season_year + 1).
    trades_off = today >= _dt.date(sy + 1, TRADE_DEADLINE_MONTH, TRADE_DEADLINE_DAY)
    cups_available = gw_finished(db, league, CUP_START_GW)
    feats = phase_features(
        macro,
        trades_off=trades_off,
        cups_available=cups_available,
        discovery_open=bool(league.discovery_open),
        gw_logic=(macro == PHASE_IN_SEASON),
    )
    from auth import is_demo

    if is_demo():
        # Demo sandbox: unlock everything so testers can explore every feature
        # (draft, discovery, trades, keepers, cups, …) regardless of the season phase.
        feats = {k: True for k in feats}
    return {
        "macro": macro,
        "label": ("Demo — all features on" if is_demo()
                  else _phase_label(macro, bool(league.discovery_open), trades_off, cups_available)),
        "current_gw": current_gameweek(db, league),
        "discovery_open": bool(league.discovery_open),
        "phase_manual": bool(league.phase_manual),
        "demo": is_demo(),
        **feats,
        # Deliberately NOT one of `feats`: the demo blanket above rewrites those, and
        # this flag must make the same demo decision as the service-layer redaction or
        # the page and the data disagree. keepers_revealed owns that decision, alone.
        "keepers_public": keepers_revealed(league),
    }


def keepers_revealed(league: League) -> bool:
    """Whether this league's keeper selections are public (see rules.keepers_revealed).

    The demo check is not optional. `phase_context` forces every feature flag True in
    demo, which makes `keepers_editable` True there — so the raw predicate would leave
    keepers permanently HIDDEN on the demo site, the exact inverse of what demo is for.
    """
    from auth import is_demo

    return is_demo() or _keepers_revealed_rule(
        league.phase or PHASE_OFFSEASON, bool(league.keepers_locked)
    )


def set_phase(db: Session, league: League, macro: str, *, manual: bool = True) -> dict:
    """Admin override: set the macro phase (and pin it so auto-advance won't move it
    while `manual` is True). Used for the admin-confirmed transitions and manual fixes."""
    from rules import PHASES

    if macro not in PHASES:
        raise RuleViolation(f"unknown phase {macro!r}")
    import datetime as _dt

    league.phase = macro
    league.phase_manual = manual
    league.phase_set_at = _dt.datetime.now(_dt.timezone.utc)
    record_audit(db, league, action="phase.set",
                 summary=f"Set phase to {macro}" + (" (pinned)" if manual else ""),
                 details={"phase": macro, "manual": manual})
    db.commit()
    return {"phase": league.phase, "manual": league.phase_manual}


def set_phase_pin(db: Session, league: League, manual: bool) -> None:
    """Pin/unpin the phase (when unpinned, sync auto-advance resumes)."""
    league.phase_manual = manual
    record_audit(db, league, action="phase.pin",
                 summary=("Pinned" if manual else "Unpinned") + f" phase ({league.phase})",
                 details={"manual": manual, "phase": league.phase})
    db.commit()


def enter_draft_phase(db: Session, league: League) -> dict:
    """Admin: start the (main) draft. Locks keeper selection and moves to the `draft`
    phase (pinned). Keeper-year decrement + the new-season carry happen at
    `advance_season` (Preseason), where they're consumed into the new league row."""
    from rules import PHASE_DRAFT

    league.keepers_locked = True
    record_audit(db, league, action="draft.enter",
                 summary="Entered draft phase (keepers locked)")
    set_phase(db, league, PHASE_DRAFT, manual=True)
    return {"phase": league.phase, "keepers_locked": True}


def close_discovery(db: Session, league: League) -> None:
    """Admin: confirm the discovery draft is complete — shut the window and mark it
    done so the Oct-1 auto-open won't re-open it."""
    league.discovery_open = False
    league.discovery_done = True
    record_audit(db, league, action="discovery.close",
                 summary="Closed the discovery draft window")
    db.commit()


def flag_ineligible(db: Session, league: League) -> int:
    """Flag players added to FPL after this season's draft (not in the pool snapshot)
    and not defenders, as ineligible for the league. No-op if no snapshot was taken
    (e.g. seasons before the snapshot existed). Returns the number newly flagged."""
    from models import PlayerIneligibility, PlayerPoolSnapshot

    snapshot = {
        fid for (fid,) in db.query(PlayerPoolSnapshot.fpl_id).filter_by(league_id=league.id)
    }
    if not snapshot:
        return 0
    already = {
        fid for (fid,) in db.query(PlayerIneligibility.fpl_id).filter_by(league_id=league.id)
    }
    added = 0
    # fpl_id is nullable since the code rekey (a departed player holds no slot);
    # a NULL would otherwise be written as an ineligibility row.
    for p in db.query(Player).filter(Player.fpl_id.isnot(None)):
        if p.fpl_id in snapshot or p.fpl_id in already:
            continue
        pos = (p.position or "").upper()
        if pos == "DEF":  # defenders added later stay eligible
            continue
        # A goalkeeper signed in January is owned the moment he signs — whoever holds
        # his club gets him, with no transaction. Flagging him "added after the draft"
        # would put a player somebody demonstrably owns on the ineligible report.
        if pos == "GKP" and goalie_teams_on(league.goalie_team_mode):
            continue
        db.add(PlayerIneligibility(
            league_id=league.id, fpl_id=p.fpl_id,
            reason="added to FPL after the draft (non-defender)",
        ))
        added += 1
    if added:
        record_audit(db, league, action="players.ineligible",
                     summary=f"Flagged {added} player(s) ineligible (added after the draft)",
                     details={"count": added})
        db.commit()
    return added


def ineligible_players(db: Session, league: League) -> list[dict]:
    """The league's ineligible players (post-draft non-defender additions), for the
    report + to exclude from draft/keeper search."""
    from models import PlayerIneligibility

    # PlayerIneligibility is keyed (league_id, fpl_id) and that fpl_id is THAT
    # season's element id — joining it to the global Player.fpl_id resolves to
    # whoever holds the id now. Match its own composite key instead.
    rows = (
        db.query(PlayerIneligibility, PlayerSeason)
        .join(
            PlayerSeason,
            (PlayerSeason.league_id == PlayerIneligibility.league_id)
            & (PlayerSeason.fpl_id == PlayerIneligibility.fpl_id),
        )
        .filter(PlayerIneligibility.league_id == league.id)
        .order_by(PlayerSeason.name)
        .all()
    )
    return [
        {"fpl_id": ps.fpl_id, "name": ps.name, "position": ps.position,
         "team": ps.current_team, "reason": il.reason}
        for il, ps in rows
    ]


def _ineligible_fpl_ids(db: Session, league: League) -> set:
    from models import PlayerIneligibility

    return {
        fid for (fid,) in db.query(PlayerIneligibility.fpl_id).filter_by(league_id=league.id)
    }


def sync_plan(db: Session, league: League, now=None) -> str:
    """Decide what a sync run should do right now: 'full' | 'live' | 'skip'. Gathers
    the facts (was there a full sync today? is a PL match live? does a GW start today?)
    and defers the decision to the pure rules.decide_sync. Lets the cron fire often
    while only doing real work when it's useful."""
    import datetime as _dt
    from models import Fixture, SyncLog

    now = now or _dt.datetime.now(_dt.timezone.utc)
    today = now.date()

    full_today = (
        db.query(SyncLog)
        .filter(SyncLog.kind == "league", SyncLog.ok.is_(True))
        .filter(SyncLog.started_at >= _dt.datetime(today.year, today.month, today.day, tzinfo=_dt.timezone.utc))
        .first()
        is not None
    )
    window = _dt.timedelta(hours=LIVE_FIXTURE_WINDOW_HOURS)
    live_fixture = (
        db.query(Fixture)
        .filter(
            Fixture.league_id == league.id,
            Fixture.kickoff_time.isnot(None),
            Fixture.kickoff_time <= now,
            Fixture.kickoff_time >= now - window,
        )
        .first()
        is not None
    )
    gw_starts_today = (
        db.query(Gameweek)
        .filter(Gameweek.league_id == league.id, Gameweek.start_date == today)
        .first()
        is not None
    )
    return decide_sync(
        full_today=full_today, live_fixture=live_fixture, gw_starts_today=gw_starts_today
    )


def _carry_club_seed(db: Session, new_league: League, manager, team_id, remaining: int):
    """Write (or update) a goalie team's carried keeper clock on the new season."""
    seed = (
        db.query(KeeperSeed)
        .filter_by(manager_id=manager.id, team_id=team_id)
        .one_or_none()
    )
    if seed:
        seed.years_remaining = remaining
        seed.league_id = new_league.id
        seed.season_year = new_league.season_year
        return
    db.add(KeeperSeed(
        league_id=new_league.id, manager_id=manager.id, team_id=team_id,
        years_remaining=remaining, season_year=new_league.season_year,
    ))


def advance_season(db: Session, old_league: League, new_league: League) -> dict:
    """Roll the league over to a new season (Preseason). The new league row must
    already be synced (the route runs sync for the new FPL id first). Carries forward,
    matched by the stable FPL entry_id (managers.fpl_manager_id):
      1. identity — display_name + password_hash (fills blanks on the new rows),
      2. keeper state — for players kept for the new season, a KeeperSeed on the new
         league with years_remaining decremented by 1 (so the clock ticks),
      3. the draft-day player-pool snapshot for the new league,
    then flips `is_current` to the new league and sets it to the preseason phase.
    Idempotent: safe to re-run."""
    from models import PlayerPoolSnapshot

    if old_league.id == new_league.id:
        raise RuleViolation("new season must be a different league")

    old_mgrs = {
        m.fpl_manager_id: m
        for m in db.query(Manager).filter_by(league_id=old_league.id)
    }
    new_mgrs = {
        m.fpl_manager_id: m
        for m in db.query(Manager).filter_by(league_id=new_league.id)
    }
    # 1. identity carry (only fill blanks, so re-running can't clobber)
    carried = 0
    for entry_id, nm in new_mgrs.items():
        om = old_mgrs.get(entry_id)
        if not om:
            continue
        if om.display_name and not nm.display_name:
            nm.display_name = om.display_name
        if om.password_hash and not nm.password_hash:
            nm.password_hash = om.password_hash
        carried += 1

    # 2. keeper carry (decrement remaining for players kept for the new season)
    status = _derive_keeper_status(db, old_league)
    clubs = _derive_gk_team_keeper_status(db, old_league)
    seeded = 0
    for ks in db.query(KeeperSelection).filter_by(
        league_id=old_league.id, season_year=new_league.season_year
    ):
        om = db.get(Manager, ks.manager_id)
        nm = new_mgrs.get(om.fpl_manager_id) if om else None
        if not nm:
            continue
        if ks.team_id is not None:
            # A kept goalie team, carried on its own clock. Without this branch it
            # falls into the `derived is None` skip below and the clock silently never
            # ticks — the club would be keepable forever, with nothing logged.
            club = clubs.get(ks.manager_id)
            if club is None or club["team_id"] != ks.team_id:
                continue
            _carry_club_seed(db, new_league, nm, ks.team_id,
                             max(club["years_remaining"] - 1, 0))
            seeded += 1
            continue
        derived = status.get(ks.manager_id, {}).get(ks.player_id)
        if derived is None:
            # The selecting manager no longer holds this player — they traded him
            # away after submitting. The selection just doesn't count (they end up
            # one keeper short), and crucially it must NOT fall through to a fresh
            # clock: that silently wrote a brand-new 2-year keeper into the season
            # for a player the manager doesn't own, permanently and invisibly.
            continue
        new_remaining = max(derived["years_remaining"] - 1, 0)
        seed = (
            db.query(KeeperSeed)
            .filter_by(manager_id=nm.id, player_id=ks.player_id)
            .one_or_none()
        )
        if seed:
            seed.years_remaining = new_remaining
            seed.league_id = new_league.id
            seed.season_year = new_league.season_year
        else:
            db.add(KeeperSeed(
                league_id=new_league.id, manager_id=nm.id, player_id=ks.player_id,
                years_remaining=new_remaining, season_year=new_league.season_year,
            ))
        seeded += 1

    # 3. the draft-day player-pool snapshot is NOT taken here — see
    #    snapshot_player_pool(). At this point `players` still holds the OUTGOING
    #    season's element ids (sync_players is still gated), so capturing now would
    #    record last season's ids as this season's draft-day pool and make
    #    flag_ineligible mass-flag legitimate players.
    snapped = 0

    # 4. flip current + set preseason. The outgoing season is frozen against the
    # FPL feed for good: its league id is now free for FPL to hand to anyone.
    for lg in db.query(League):
        lg.is_current = (lg.id == new_league.id)
    old_league.sync_locked = True
    new_league.sync_locked = False
    import datetime as _dt

    new_league.phase = PHASE_PRESEASON
    new_league.phase_manual = False
    new_league.phase_set_at = _dt.datetime.now(_dt.timezone.utc)
    record_audit(db, new_league, action="season.rollover",
                 summary=(f"Rolled over to {new_league.season_year}: carried "
                          f"{carried} identities, seeded {seeded} keepers"),
                 details={"new_season": new_league.season_year, "managers_carried": carried,
                          "keepers_seeded": seeded, "pool_snapshot": snapped})
    db.commit()
    return {
        "new_season": new_league.season_year,
        "managers_carried": carried,
        "keepers_seeded": seeded,
        "pool_snapshot": snapped,
    }


def snapshot_player_pool(db: Session, league: League) -> int:
    """Capture the draft-day player pool (the set of element ids that existed) for
    `league`. Idempotent — skips ids already recorded.

    Must run AFTER the first post-rollover sync_players, not inside advance_season:
    sync_players is gated on a league being current and unfrozen, so until the
    rollover flips those flags `players` still holds the previous season's element
    ids. Capturing early would record last season's ids as this season's draft-day
    pool, and flag_ineligible would then flag most of the real pool as
    'added after the draft'.
    """
    have = {
        fid for (fid,) in db.query(PlayerPoolSnapshot.fpl_id).filter_by(
            league_id=league.id
        )
    }
    snapped = 0
    for (fid,) in db.query(Player.fpl_id).filter(Player.fpl_id.isnot(None)):
        if fid not in have:
            db.add(PlayerPoolSnapshot(league_id=league.id, fpl_id=fid))
            snapped += 1
    if snapped:
        db.commit()
    return snapped


def advance_phase_if_due(db: Session, league: League, now=None) -> bool:
    """Auto-advance the time/GW-driven phase transitions during sync (the heartbeat):
    in_season→offseason at GW38, preseason→in_season at GW1, and the Oct-1 discovery
    auto-open. Skipped when the admin has pinned the phase (`phase_manual`). Returns
    True if anything changed. The fact-gathering is here; the decision is pure
    (`rules.next_phase`)."""
    import datetime as _dt

    if league.phase_manual:
        return False
    today = now or _dt.date.today()
    new_macro, open_disc = next_phase(
        league.phase,
        gw38_done=gw_finished(db, league, SEASON_LAST_GW),
        gw1_started=(current_gameweek(db, league) or 0) >= 1,
        today=today,
        season_year=league.season_year or today.year,
        discovery_open=bool(league.discovery_open),
        discovery_done=bool(league.discovery_done),
    )
    changed = False
    if new_macro != league.phase:
        league.phase = new_macro
        changed = True
    if open_disc and not league.discovery_open:
        league.discovery_open = True
        changed = True
    # The season is over: freeze the row against the FPL feed. FPL reuses league
    # ids, so once 38 GWs are done this id can start resolving to someone else's
    # league — syncing it would merge their teams into our finished season.
    if new_macro == PHASE_OFFSEASON and not league.sync_locked:
        league.sync_locked = True
        changed = True
    if changed:
        league.phase_set_at = _dt.datetime.now(_dt.timezone.utc)
        record_audit(db, league, action="phase.auto",
                     summary=f"Auto-advanced phase to {league.phase}"
                             + (" (discovery opened)" if open_disc else "")
                             + (" (season frozen)" if league.sync_locked else ""),
                     details={"phase": league.phase, "discovery_open": bool(open_disc),
                              "sync_locked": league.sync_locked})
        db.commit()
    return changed


def fixtures_for_gws(db: Session, league: League, gw_numbers: list[int]) -> dict:
    """Real-life PL fixtures for the given GW numbers, indexed for quick lookup by a
    player's club: {gw: {team_short: [{opp, home, difficulty, kickoff}, ...]}}. A
    club may have 0 (blank) or 2 (double) fixtures in a GW, hence the list."""
    out: dict = {gw: {} for gw in gw_numbers}
    if not gw_numbers:
        return out
    rows = (
        db.query(Fixture)
        .filter(Fixture.league_id == league.id, Fixture.event.in_(gw_numbers))
        .all()
    )
    for f in rows:
        kickoff = f.kickoff_time.isoformat() if f.kickoff_time else None
        if f.home_team:
            out[f.event].setdefault(f.home_team, []).append(
                {"opp": f.away_team, "home": True, "difficulty": f.home_difficulty, "kickoff": kickoff}
            )
        if f.away_team:
            out[f.event].setdefault(f.away_team, []).append(
                {"opp": f.home_team, "home": False, "difficulty": f.away_difficulty, "kickoff": kickoff}
            )
    return out


def get_standings(db: Session, league: League) -> list[dict]:
    """Live standings with commissioner adjustments applied as accumulating deltas
    on top of the synced totals, then re-ranked."""
    from models import StandingAdjustment

    rows = (
        db.query(Standing, Manager)
        .join(Manager, Manager.id == Standing.manager_id)
        .filter(Standing.league_id == league.id)
        .all()
    )
    dt: dict = {}   # manager_id -> summed H2H (total) delta
    dpf: dict = {}  # manager_id -> summed points_for delta
    for a in db.query(StandingAdjustment).filter_by(league_id=league.id):
        dt[a.manager_id] = dt.get(a.manager_id, 0) + a.total_delta
        dpf[a.manager_id] = dpf.get(a.manager_id, 0) + a.points_for_delta

    out = []
    for s, m in rows:
        out.append({
            "manager": m.display,
            "fpl": m.fpl_manager_id,
            "total": (s.total or 0) + dt.get(m.id, 0),
            "points_for": (s.points_for or 0) + dpf.get(m.id, 0),
            "points_against": s.points_against,
            "matches_won": s.matches_won,
            "matches_drawn": s.matches_drawn,
            "matches_lost": s.matches_lost,
            "total_delta": dt.get(m.id, 0),
            "points_for_delta": dpf.get(m.id, 0),
            "adjusted": bool(dt.get(m.id) or dpf.get(m.id)),
        })
    out.sort(key=lambda x: (-(x["total"] or 0), -(x["points_for"] or 0), x["manager"]))
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


def adjust_standing(
    db: Session, league: League, *, fpl_manager_id: str,
    total_delta: int = 0, points_for_delta: int = 0,
    gameweek: int | None = None, note: str | None = None,
) -> dict:
    """Apply a RELATIVE standings adjustment (delta) for a manager — e.g. a -3 H2H
    / -10 total deduction. Deltas accumulate and apply on top of live standings."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    if not total_delta and not points_for_delta:
        raise RuleViolation("enter a non-zero H2H and/or total points change")
    from models import StandingAdjustment

    db.add(StandingAdjustment(
        league_id=league.id, manager_id=manager.id,
        total_delta=total_delta, points_for_delta=points_for_delta,
        gameweek=gameweek, note=note,
    ))
    record_audit(db, league, action="standing.adjust",
                 summary=(f"Adjusted {manager.display}: H2H {total_delta:+d}, "
                          f"points {points_for_delta:+d}"
                          + (f" — {note}" if note else "")),
                 manager_ids=[manager.id],
                 details={"total_delta": total_delta, "points_for_delta": points_for_delta,
                          "gameweek": gameweek, "note": note})
    db.commit()
    return {"manager": manager.display, "total_delta": total_delta, "points_for_delta": points_for_delta}


def get_standing_adjustments(db: Session, league: League) -> list[dict]:
    """The log of standings adjustments (deltas) — the evidence trail."""
    from models import StandingAdjustment

    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    rows = (
        db.query(StandingAdjustment)
        .filter_by(league_id=league.id)
        .order_by(StandingAdjustment.created_at.desc())
        .all()
    )
    return [
        {
            "id": str(a.id),
            "manager": names.get(a.manager_id), "total_delta": a.total_delta,
            "points_for_delta": a.points_for_delta, "gameweek": a.gameweek,
            "note": a.note, "when": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


def reset_manager_password(db: Session, league: League, fpl_manager_id: str) -> None:
    """Clear a manager's UI password so they set a new one on next login."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    manager.password_hash = None
    record_audit(db, league, action="manager.password_reset",
                 summary=f"Reset {manager.display}'s password",
                 manager_ids=[manager.id])
    db.commit()


def delete_standing_adjustment(db: Session, league: League, adjustment_id: str) -> None:
    """Remove a standings adjustment (commissioner only). Reversible by re-adding."""
    from models import StandingAdjustment

    row = (
        db.query(StandingAdjustment)
        .filter_by(league_id=league.id, id=adjustment_id)
        .one_or_none()
    )
    if not row:
        raise RuleViolation("adjustment not found")
    mgr = db.get(Manager, row.manager_id)
    record_audit(db, league, action="standing.adjust.delete",
                 summary=(f"Removed standings adjustment for "
                          f"{mgr.display if mgr else '—'}: H2H {row.total_delta:+d}, "
                          f"points {row.points_for_delta:+d}"),
                 manager_ids=[row.manager_id],
                 details={"total_delta": row.total_delta,
                          "points_for_delta": row.points_for_delta, "note": row.note})
    db.delete(row)
    db.commit()


def get_rosters(db: Session, league: League) -> list[dict]:
    """Current rosters (latest synced gameweek), grouped by manager."""
    gw = latest_gameweek(db, league)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.name).all()
    )
    # one overlay for the whole loop rather than one per manager
    moved = player_ownership(db, league) if gw is not None else {}
    out = []
    for m in managers:
        players = _squad_players(db, league, m.id, gw.id if gw else None, moved=moved)
        out.append(
            {
                "manager": m.display,
                "players": [
                    {"name": p.name, "position": p.position, "team": p.current_team}
                    for p in players
                ],
            }
        )
    return out


def _squad_players(
    db: Session, league: League, manager_id, gw_id, *, moved: dict | None = None
) -> list[PlayerSeason]:
    """`league` is positional and has no default on purpose: a missed caller must
    fail loudly rather than silently filter on league_id IS NULL.

    Resolves membership through _effective_roster_pids, not a join on Roster, so a
    commissioner-entered trade moves the player in BOTH directions — the join alone
    would neither add him to the buyer nor remove him from the seller. Pass `moved`
    when looping over managers to compute the overlay once.
    """
    if gw_id is None:
        return []
    pids = _effective_roster_pids(db, league, manager_id, gw_id, moved)
    if not pids:
        return []
    return (
        db.query(PlayerSeason)
        .filter(
            PlayerSeason.player_id.in_(pids),
            PlayerSeason.league_id == league.id,
        )
        .order_by(PlayerSeason.position, PlayerSeason.name)
        .all()
    )


def _player_stat_dict(p: "Player | PlayerSeason") -> dict:
    """Duck-typed over both — PlayerSeason mirrors every attribute read here,
    including `news`."""
    return {
        "fpl_id": p.fpl_id, "name": p.name, "position": p.position, "team": p.current_team,
        "price": (p.price / 10) if p.price is not None else None,
        "status": p.status, "news": p.news,
        "form": p.form, "points_per_game": p.points_per_game,
        "total_points": p.total_points, "goals_scored": p.goals_scored,
        "assists": p.assists, "clean_sheets": p.clean_sheets, "bonus": p.bonus,
        "minutes": p.minutes, "ict_index": p.ict_index,
        "selected_by_percent": p.selected_by_percent,
    }


_POSITION_ORDER = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}


def get_my_team(db: Session, league: League, fpl_manager_id: str) -> dict | None:
    """A single manager's current squad with rich per-player stats + a recent
    points trend (from stored gameweek_points). None if the manager isn't found."""
    manager = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not manager:
        return None
    gw = latest_gameweek(db, league)
    players = _squad_players(db, league, manager.id, gw.id if gw else None)

    # recent points trend per player (last 5 synced GWs, oldest->newest)
    recent = (
        db.query(GameweekPoints, Gameweek)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(GameweekPoints.manager_id == manager.id, Gameweek.league_id == league.id)
        .order_by(Gameweek.number.desc())
        .limit(5)
        .all()
    )
    # player_points embeds THAT season's element ids, which FPL recycles — resolve
    # them through player_season and key the trend by the stable player_id.
    season_map = {
        ps.fpl_id: ps.player_id
        for ps in db.query(PlayerSeason).filter_by(league_id=league.id)
    }
    trend: dict = {}
    for gp, _g in reversed(recent):
        for entry in (gp.player_points or []):
            pid = season_map.get(entry.get("fpl_id"))
            if pid is not None:
                trend.setdefault(pid, []).append(entry.get("points"))

    # keeper badges for the upcoming season
    upcoming = (league.season_year or 0) + 1
    keeper_pids = {
        pid for (pid,) in db.query(KeeperSelection.player_id).filter_by(
            league_id=league.id, manager_id=manager.id, season_year=upcoming
        )
    }

    out_players = []
    for p in players:
        d = _player_stat_dict(p)
        d["trend"] = trend.get(p.player_id, [])
        # p is a PlayerSeason row: p.id is the snapshot's own PK, and keeper_pids
        # holds players.id — so this must compare player_id or it is always False.
        d["is_keeper"] = p.player_id in keeper_pids
        out_players.append(d)
    out_players.sort(key=lambda d: (_POSITION_ORDER.get(d["position"], 9), d["name"]))
    return {
        "manager": manager.display,
        "fpl": manager.fpl_manager_id,
        "gameweek": gw.number if gw else None,
        "players": out_players,
        "status": _manager_status(db, league, manager),
    }


def _manager_status(db: Session, league: League, manager: Manager) -> dict:
    """A manager's standing on the league's risk rules: anti-tanking (current
    0-minute streak vs. the threshold) and active injury-list players with how many
    gameweeks they've been on the IL."""
    by_manager = _tanking_counts_by_manager(db, league)
    counts = by_manager.get(manager.id, {}).get("counts", {})
    streak = current_tanking_streak(counts)
    # A dismissed window is not a violation any more; the streak is separate and
    # unaffected, so a manager whose only window was cleared falls through to it.
    flagged = manager.id in _open_windows(db, league, by_manager)
    if flagged:
        tank_state = "flagged"
    elif streak >= ANTI_TANKING_MIN_WEEKS - 1 and streak > 0:
        tank_state = "at_risk"
    else:
        tank_state = "ok"

    cur = current_gameweek(db, league)
    il_rows = (
        db.query(InjuryList, PlayerSeason)
        .join(PlayerSeason, PlayerSeason.player_id == InjuryList.player_id)
        .filter(InjuryList.manager_id == manager.id, InjuryList.status == "active",
                PlayerSeason.league_id == league.id)
        .all()
    )
    il = []
    for entry, p in il_rows:
        gws_on = None
        if cur is not None and entry.start_gw is not None:
            gws_on = max(cur - entry.start_gw + 1, 0)
        repl = db.get(Player, entry.replacement_id) if entry.replacement_id else None
        eligible_gw = il_return_eligible_gw(entry.start_gw) if entry.start_gw is not None else None
        il.append({
            "id": str(entry.id),
            "player": p.name, "position": p.position,
            "replacement": repl.name if repl else None,
            "start_gw": entry.start_gw, "end_gw": entry.end_gw, "gws_on_il": gws_on,
            "return_gw": eligible_gw,
            "can_return": cur is not None and eligible_gw is not None and cur >= eligible_gw,
        })
    from models import InternationalList

    intl = []
    for entry, p in (
        db.query(InternationalList, PlayerSeason)
        .join(PlayerSeason, PlayerSeason.player_id == InternationalList.player_id)
        .filter(InternationalList.manager_id == manager.id,
                InternationalList.status == "active",
                PlayerSeason.league_id == league.id)
        .all()
    ):
        repl = db.get(Player, entry.replacement_id) if entry.replacement_id else None
        gws_out = max(cur - entry.start_gw + 1, 0) if (cur and entry.start_gw) else None
        intl.append({
            "id": str(entry.id), "player": p.name, "position": p.position,
            "replacement": repl.name if repl else None, "tournament": entry.tournament,
            "start_gw": entry.start_gw, "gws_out": gws_out,
        })
    return {
        "tanking": {
            "state": tank_state,
            "streak": streak,
            "threshold": ANTI_TANKING_MIN_WEEKS,
            "min_players": ANTI_TANKING_MIN_ZERO_PLAYERS,
        },
        "injury_list": il,
        "international_list": intl,
    }


def get_upcoming_matchups(
    db: Session, league: League, fpl_manager_id: str, n: int = 3
) -> list[dict]:
    """The manager's next `n` H2H matchups (from the synced schedule) with both
    squads and each player's real-life PL fixture + difficulty. Future starting XIs
    aren't set yet, so each squad shown is the current 15-man roster (projected)."""
    manager = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not manager:
        return []
    cur = current_gameweek(db, league)
    if cur is None:
        return []
    gw_numbers = [cur + i for i in range(1, n + 1) if cur + i <= SEASON_LAST_GW]
    if not gw_numbers:
        return []

    gws = {
        g.number: g
        for g in db.query(Gameweek).filter(
            Gameweek.league_id == league.id, Gameweek.number.in_(gw_numbers)
        )
    }
    fixtures = fixtures_for_gws(db, league, gw_numbers)
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    fpls = {m.id: m.fpl_manager_id for m in db.query(Manager).filter_by(league_id=league.id)}
    latest = latest_gameweek(db, league)
    latest_id = latest.id if latest else None

    def squad_with_fixtures(manager_id, gw_num):
        rows = _squad_players(db, league, manager_id, latest_id)
        gw_fix = fixtures.get(gw_num, {})
        out = []
        for p in rows:
            d = _player_stat_dict(p)
            d["fixtures"] = gw_fix.get(p.current_team, [])
            out.append(d)
        out.sort(key=lambda d: (_POSITION_ORDER.get(d["position"], 9), d["name"]))
        return out

    result = []
    for num in gw_numbers:
        g = gws.get(num)
        if not g:
            continue
        match = (
            db.query(Match)
            .filter(
                Match.league_id == league.id,
                Match.gameweek_id == g.id,
                (Match.home_manager_id == manager.id) | (Match.away_manager_id == manager.id),
            )
            .one_or_none()
        )
        if not match:
            result.append({"gameweek": num, "opponent": None})
            continue
        opp_id = match.away_manager_id if match.home_manager_id == manager.id else match.home_manager_id
        result.append({
            "gameweek": num,
            "opponent": names.get(opp_id),
            "opponent_fpl": fpls.get(opp_id),
            "my_squad": squad_with_fixtures(manager.id, num),
            "opp_squad": squad_with_fixtures(opp_id, num),
        })
    return result


def get_injury_list(db: Session, league: League) -> list[dict]:
    """Active injury-list entries for the league (admin-managed; may be empty)."""
    rows = (
        db.query(InjuryList, Manager, PlayerSeason)
        .join(Manager, Manager.id == InjuryList.manager_id)
        .join(PlayerSeason, PlayerSeason.player_id == InjuryList.player_id)
        .filter(Manager.league_id == league.id, InjuryList.status == "active",
                PlayerSeason.league_id == league.id)
        .all()
    )
    return [
        {
            "manager": m.display,
            "player": p.name,
            "start_gw": il.start_gw,
            "end_gw": il.end_gw,
            "status": il.status,
        }
        for il, m, p in rows
    ]


def _window_label(window: list[int]) -> str:
    """[10, 11, 12] -> 'GW10–12'."""
    return f"GW{window[0]}–{window[-1]}"


def _gw_fixture_teams(db: Session, league: League) -> dict:
    """gw_number -> the set of club short names with a real fixture that gameweek.

    A club with no fixture (a blank GW) plays nobody, so its players' 0 minutes say
    nothing about the manager. Short names join against `PlayerSeason.current_team`,
    per the Fixture model's own docstring."""
    teams: dict = {}
    for event, home, away in (
        db.query(Fixture.event, Fixture.home_team, Fixture.away_team)
        .filter(Fixture.league_id == league.id, Fixture.event.isnot(None))
        .all()
    ):
        slot = teams.setdefault(event, set())
        slot.update(t for t in (home, away) if t)
    return teams


def _season_by_fpl_id(db: Session, league: League) -> dict:
    """this season's element id -> PlayerSeason row. `player_points` embeds the
    element ids of the season it was synced in, and FPL recycles those every year, so
    position/club must be read off the season snapshot rather than the global pool."""
    return {
        ps.fpl_id: ps
        for ps in db.query(PlayerSeason).filter_by(league_id=league.id)
        if ps.fpl_id is not None
    }


def _tanking_counts_by_manager(db: Session, league: League) -> dict:
    """manager_id -> {"manager": Manager, "counts": {gw_number: zero_minute_count}}.

    The count is what the manager can be held responsible for, so three structural
    zeroes are stripped out first: a club with no fixture that GW, a player covered by
    the injury or international list, and the lone non-starting goalkeeper every squad
    has to carry (see `rules.zero_minute_count`)."""
    rows = (
        db.query(GameweekPoints, Gameweek, Manager)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .join(Manager, Manager.id == GameweekPoints.manager_id)
        .filter(Manager.league_id == league.id)
        .all()
    )
    if not rows:
        return {}

    season = _season_by_fpl_id(db, league)
    goalkeeper_ids = frozenset(
        fid for fid, ps in season.items() if (ps.position or "").upper().startswith("GK")
    )
    fixture_teams = _gw_fixture_teams(db, league)
    cover = _absence_cover(db, league, max(gw.number for _gp, gw, _m in rows))

    per_manager: dict = {}
    for gp, gw, m in rows:
        playing = fixture_teams.get(gw.number) or set()
        excused = set()
        for entry in (gp.player_points or []):
            fid = entry.get("fpl_id")
            ps = season.get(fid)
            if ps is None:
                continue
            # A GW with NO fixture rows at all is missing data, not 20 blank clubs —
            # excusing everyone there would silently switch the rule off for good.
            if playing and ps.current_team not in playing:
                excused.add(fid)
            elif gw.number in cover.get((m.id, ps.player_id), ()):
                excused.add(fid)
        entry_for = per_manager.setdefault(m.id, {"manager": m, "counts": {}})
        entry_for["counts"][gw.number] = zero_minute_count(
            gp.player_points or [],
            excused=excused,
            goalkeeper_ids=goalkeeper_ids,
        )
    return per_manager


def _cleared_windows(db: Session, league: League) -> set:
    """{(manager_id, window_label)} the commissioner has dismissed."""
    from models import TankingFlagClear

    return {
        (c.manager_id, c.window)
        for c in db.query(TankingFlagClear).filter_by(league_id=league.id)
    }


def _open_windows(db: Session, league: League, counts_by_manager: dict = None) -> dict:
    """manager_id -> the tanking windows still standing (dismissals removed).

    `get_flags` deliberately keeps cleared windows so admin can see and restore them;
    every OTHER reader wants only the open ones, or a dismissal would clear the flag
    from the homepage list while leaving it on My Team and in Flagged Actions.

    Pass `counts_by_manager` if you already have it — recomputing costs a scan of every
    manager's every gameweek plus the fixture and absence lookups."""
    cleared = _cleared_windows(db, league)
    out: dict = {}
    for mid, info in (counts_by_manager or _tanking_counts_by_manager(db, league)).items():
        open_ = [w for w in tanking_windows(info["counts"])
                 if (mid, _window_label(w)) not in cleared]
        if open_:
            out[mid] = open_
    return out


def get_flags(db: Session, league: League) -> list[dict]:
    """Anti-tanking flags across all synced gameweeks (precomputed read). Flags a
    manager when >=3 of their rostered players posted 0 minutes in each of >=3
    consecutive gameweeks. Each window carries `cleared` (commissioner-dismissed).
    """
    cleared = _cleared_windows(db, league)
    rule = (
        f"{ANTI_TANKING_MIN_ZERO_PLAYERS}+ rostered players with 0 minutes "
        f"for {ANTI_TANKING_MIN_WEEKS}+ straight GWs"
    )
    flags = []
    for mid, info in _tanking_counts_by_manager(db, league).items():
        windows = tanking_windows(info["counts"])
        if not windows:
            continue
        flags.append({
            "manager": info["manager"].display,
            "fpl": info["manager"].fpl_manager_id,
            "rule": rule,
            "windows": [
                {"label": _window_label(w), "cleared": (mid, _window_label(w)) in cleared}
                for w in windows
            ],
        })
    return sorted(flags, key=lambda f: f["manager"])


# back-compat alias (older callers / JSON route)
get_infractions = get_flags


def clear_flag(db: Session, league: League, fpl_manager_id: str, window: str) -> None:
    """Commissioner dismisses an anti-tanking flag (manager + GW window)."""
    from models import TankingFlagClear

    manager = _resolve_manager(db, league, fpl_manager_id)
    exists = (
        db.query(TankingFlagClear)
        .filter_by(league_id=league.id, manager_id=manager.id, window=window)
        .one_or_none()
    )
    if not exists:
        db.add(TankingFlagClear(league_id=league.id, manager_id=manager.id, window=window))
        record_audit(db, league, action="tanking.clear",
                     summary=f"Dismissed anti-tanking flag for {manager.display} ({window})",
                     manager_ids=[manager.id], details={"window": window})
        db.commit()


def restore_flag(db: Session, league: League, fpl_manager_id: str, window: str) -> None:
    """Undo a flag dismissal."""
    from models import TankingFlagClear

    manager = _resolve_manager(db, league, fpl_manager_id)
    db.query(TankingFlagClear).filter_by(
        league_id=league.id, manager_id=manager.id, window=window
    ).delete()
    record_audit(db, league, action="tanking.restore",
                 summary=f"Restored anti-tanking flag for {manager.display} ({window})",
                 manager_ids=[manager.id], details={"window": window})
    db.commit()


# ---- fines (commissioner-issued; feed payouts + net winnings) ----
def add_fine(
    db: Session, league: League, *, fpl_manager_id: str, amount: int,
    reason: str | None = None, gameweek: int | None = None,
) -> dict:
    from models import Fine

    manager = _resolve_manager(db, league, fpl_manager_id)
    if not amount:
        raise RuleViolation("enter a non-zero fine amount")
    db.add(Fine(league_id=league.id, manager_id=manager.id, amount=amount,
                reason=reason, gameweek=gameweek))
    record_audit(db, league, action="fine.add",
                 summary=(f"Fined {manager.display} ${amount}"
                          + (f" — {reason}" if reason else "")),
                 manager_ids=[manager.id],
                 details={"amount": amount, "reason": reason, "gameweek": gameweek})
    db.commit()
    return {"manager": manager.display, "amount": amount}


def delete_fine(db: Session, league: League, fine_id: str) -> None:
    from models import Fine

    row = db.query(Fine).filter_by(league_id=league.id, id=fine_id).one_or_none()
    if not row:
        raise RuleViolation("fine not found")
    mgr = db.get(Manager, row.manager_id)
    record_audit(db, league, action="fine.delete",
                 summary=f"Removed ${row.amount} fine on {mgr.display if mgr else '—'}"
                         + (f" — {row.reason}" if row.reason else ""),
                 manager_ids=[row.manager_id],
                 details={"amount": row.amount, "reason": row.reason})
    db.delete(row)
    db.commit()


def get_fines(db: Session, league: League) -> list[dict]:
    """All fines (the evidence log), newest first."""
    from models import Fine

    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    rows = (
        db.query(Fine).filter_by(league_id=league.id)
        .order_by(Fine.created_at.desc()).all()
    )
    return [
        {"id": str(f.id), "manager": names.get(f.manager_id), "amount": f.amount,
         "reason": f.reason, "gameweek": f.gameweek,
         "when": f.created_at.isoformat() if f.created_at else None}
        for f in rows
    ]


def _fines_by_manager_id(db: Session, league: League) -> dict:
    """manager_id -> total dollars fined (for payouts)."""
    from models import Fine

    totals: dict = {}
    for f in db.query(Fine).filter_by(league_id=league.id):
        totals[f.manager_id] = totals.get(f.manager_id, 0) + f.amount
    return totals


# ---- injury list (admin-managed writes) ----
def _resolve_manager(db: Session, league: League, fpl_manager_id: str) -> Manager:
    m = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not m:
        raise RuleViolation(f"manager {fpl_manager_id} not found in league")
    return m


def _resolve_player(db: Session, fpl_id: int) -> Player:
    # fpl_id is nullable (a departed player releases their slot), and `filter_by`
    # turns None into `fpl_id IS NULL` — matching every departed row at once, so
    # `.one_or_none()` raises MultipleResultsFound (an unhandled 500) instead of a
    # clean refusal. search_players already excludes these from "available", but
    # fail safe here too rather than depend on every caller pre-filtering.
    if fpl_id is None:
        raise RuleViolation("no player specified")
    p = db.query(Player).filter_by(fpl_id=fpl_id).one_or_none()
    if not p:
        raise RuleViolation(f"player {fpl_id} not found")
    return p


def _resolve_team(db: Session, team_code: int) -> PlTeam:
    """A goalie team by FPL's permanent team `code` — the twin of _resolve_player.

    `code`, not `pl_teams.id`, is what travels over the wire: it is a small stable
    integer that survives every season, exactly like `fpl_id` does for players, so
    forms and hx-vals stay the same shape they already were.
    """
    if team_code is None:
        raise RuleViolation("no goalie team specified")
    t = db.query(PlTeam).filter_by(code=team_code).one_or_none()
    if not t:
        raise RuleViolation(f"goalie team {team_code} not found")
    return t


def goalie_team_keepers(db: Session, teams=None) -> dict:
    """{pl_teams.id: [Player, ...]} — every goalkeeper at each club, right now.

    Derived on read, never stored. Owning a goalie team means owning whoever keeps
    for that club TODAY: a January signing joins your squad and a sale leaves it,
    with nothing to reconcile. A stored keeper list would be a snapshot that goes
    stale the first time a club buys a goalkeeper.

    Departed players (fpl_id NULL) are excluded — they are no longer in the league.
    """
    rows = teams if teams is not None else db.query(PlTeam).all()
    by_short = {t.short_name: t for t in rows}
    out: dict = {t.id: [] for t in rows}
    if not by_short:
        return out
    keepers = (
        db.query(Player)
        .filter(Player.position == "GKP",
                Player.fpl_id.isnot(None),
                Player.current_team.in_(list(by_short)))
        .order_by(Player.name)
        .all()
    )
    for p in keepers:
        out[by_short[p.current_team].id].append(p)
    return out


def season_identity(db: Session, league: League, player_ids=None) -> dict:
    """player_id -> PlayerSeason row for this league's season.

    The single source of truth for season-scoped player attributes. `players` is
    global and always holds whatever season synced last, so reading names/clubs
    off it shows the wrong thing for any past season. This works identically for a
    current or a frozen league: sync_players refreshes player_season on every run
    while a league is unfrozen, then it stays put — so there is one code path, no
    `if league.sync_locked` branching at call sites.
    """
    q = db.query(PlayerSeason).filter_by(league_id=league.id)
    if player_ids is not None:
        q = q.filter(PlayerSeason.player_id.in_(player_ids))
    return {ps.player_id: ps for ps in q}


def player_names(db: Session, league: League) -> dict:
    """player_id -> display name, preferring this season's snapshot.

    Falls back to the live pool for anyone with no snapshot row. That matters on the
    draft board: you draft from the CURRENT pool, so a player who joined the league
    after the last completed season (a promoted club's squad, a new signing) has no
    snapshot row yet and would otherwise render as a blank name once picked.
    """
    names = {p.id: p.name for p in db.query(Player)}
    names.update({pid: ps.name for pid, ps in season_identity(db, league).items()})
    return names


def stats_season(db: Session, league: League) -> League:
    """The season whose player STATISTICS we display.

    Once the season is under way, its own running totals are what matter, so we show
    `league`. Before then — offseason, draft, preseason — the current season has no
    numbers yet and everyone is drafting on last year's production, so we fall back
    to the most recent completed season that actually has statistics.

    The switch is the phase, which advance_phase_if_due flips to in_season at GW1, so
    the changeover happens on its own every year. After GW38 the just-finished season
    is both current and completed, so either branch resolves to it.
    """
    if league.phase == PHASE_IN_SEASON:
        return league
    completed = (
        db.query(League)
        .join(PlayerSeason, PlayerSeason.league_id == League.id)
        .filter(League.sync_locked.is_(True), PlayerSeason.total_points.isnot(None))
        .order_by(League.season_year.desc())
        .first()
    )
    return completed or league


def projection_season_year(db: Session) -> int | None:
    """The season our imported projections describe — the newest one we hold.

    Read off the data on purpose, the same way stats_season reads the stats season off
    the data rather than off a flag. `league.season_year + 1` is correct only in the
    window between GW38 and the rollover: the moment advance_season flips is_current to
    the new row, +1 becomes the season AFTER it, every projected column silently blanks
    for a whole year, and nothing errors. Taking the max shows 26/27 projections all
    through 26/27 and switches by itself the day next summer's file is imported.

    None before the first import — that is how the page knows to hide the whole column
    group rather than render ten columns of em-dashes for every player.
    """
    return db.query(func.max(PlayerProjection.season_year)).scalar()


def projection_index(db: Session, season_year: int) -> dict:
    """player_id -> PlayerProjection for a season, in one query.

    Keyed on PlayerProjection.player_id — a players.id — NOT the projection row's own
    .id, which would compare False against every roster/keeper FK (the PlayerSeason
    gotcha in CLAUDE.md, one table over).
    """
    return {
        r.player_id: r
        for r in db.query(PlayerProjection).filter_by(season_year=season_year)
    }


def year_label(year: int | None) -> str:
    """A season year as the league writes it: 2026 -> '26/27'. Takes a bare int
    because the projected season has no league row until the rollover runs."""
    y = year or 0
    return f"{y % 100:02d}/{(y + 1) % 100:02d}"


def season_label(league: League) -> str:
    """A season as the league writes it: 2025 -> '25/26'."""
    return year_label(league.season_year)


def _il_to_dict(entry: InjuryList, injured: Player, replacement: Player | None) -> dict:
    return {
        "id": str(entry.id),
        "player": injured.name,
        "position": injured.position,
        "replacement": replacement.name if replacement else None,
        "start_gw": entry.start_gw,
        "end_gw": entry.end_gw,
        "status": entry.status,
    }


def _refuse_goalkeeper_list_move(league: League, *players, what: str) -> None:
    """Goalkeepers are out of scope for the injury and international lists.

    Under the goalie-team rule the rule is unsatisfiable anyway: both lists demand a
    same-position replacement, and the only goalkeepers a manager owns are the ones at
    their own club — so the only legal replacement is somebody they already have.
    Worse, it would burn the one-active-entry-per-manager slot on a no-op. An injured
    club keeper needs no action: his club's backup plays and scores in the same slot.
    """
    if not goalie_teams_on(league.goalie_team_mode):
        return
    if any((p.position or "").upper() == "GKP" for p in players):
        raise RuleViolation(
            f"goalkeepers aren't on the {what} — your club's other keeper covers the "
            "absence automatically"
        )


def place_on_il(
    db: Session,
    league: League,
    *,
    fpl_manager_id: str,
    injured_fpl_id: int,
    replacement_fpl_id: int,
    start_gw: int,
) -> dict:
    """Place a manager's injured player on the IL with a same-position replacement.

    Enforces: one active IL player per manager; replacement same position.
    """
    manager = _resolve_manager(db, league, fpl_manager_id)
    injured = _resolve_player(db, injured_fpl_id)
    replacement = _resolve_player(db, replacement_fpl_id)

    existing = (
        db.query(InjuryList)
        .filter_by(manager_id=manager.id, status="active")
        .first()
    )
    if existing:
        raise RuleViolation("manager already has an active injury-list player")
    _refuse_goalkeeper_list_move(league, injured, replacement, what="injury list")
    if not il_same_position(injured.position, replacement.position):
        raise RuleViolation(
            f"replacement is {replacement.position}, must match injured "
            f"player's position {injured.position}"
        )

    entry = InjuryList(
        player_id=injured.id,
        manager_id=manager.id,
        start_gw=start_gw,
        replacement_id=replacement.id,
        status="active",
    )
    db.add(entry)
    record_audit(db, league, action="il.place",
                 summary=(f"{manager.display} placed {injured.name} "
                          f"({injured.position}) on IL → {replacement.name} (GW{start_gw})"),
                 manager_ids=[manager.id],
                 details={"injured_fpl_id": injured_fpl_id,
                          "replacement_fpl_id": replacement_fpl_id, "start_gw": start_gw})
    db.commit()
    db.refresh(entry)
    return _il_to_dict(entry, injured, replacement)


def il_return_eligible_gw(start_gw: int) -> int:
    """Earliest GW an IL'd player may return (min stay, capped at season end)."""
    return min(start_gw + MIN_IL_STAY_GWS, SEASON_LAST_GW)


def return_from_il(
    db: Session, league: League, il_id: str, return_gw: int, via: str = "manual"
) -> dict:
    """Return an active IL player. Enforces the minimum-stay rule (a return at or
    after the season's last GW is automatic). `via='waiver'` marks a waiver return.
    """
    entry = db.get(InjuryList, il_id)
    if not entry:
        raise RuleViolation("injury-list entry not found")
    if entry.status != "active":
        raise RuleViolation(f"injury-list entry is already '{entry.status}'")
    if not il_can_return(entry.start_gw, return_gw):
        raise RuleViolation(
            f"minimum {MIN_IL_STAY_GWS}-GW stay not met "
            f"(placed GW{entry.start_gw}, return GW{return_gw})"
        )

    entry.end_gw = return_gw
    entry.status = "waived" if via == "waiver" else "returned"
    injured = db.get(Player, entry.player_id)
    replacement = db.get(Player, entry.replacement_id) if entry.replacement_id else None
    mgr = db.get(Manager, entry.manager_id)
    record_audit(db, league, action="il.return",
                 summary=(f"{mgr.display if mgr else '—'} returned "
                          f"{injured.name if injured else '—'} from IL "
                          f"(GW{return_gw}, {entry.status})"),
                 manager_ids=[entry.manager_id],
                 details={"il_id": str(il_id), "return_gw": return_gw, "via": via})
    db.commit()
    db.refresh(entry)
    return _il_to_dict(entry, injured, replacement)


# ---- international list (AFCON / Asia Cup temporary leave) ----
def place_on_intl(
    db: Session, league: League, *, fpl_manager_id: str, away_fpl_id: int,
    replacement_fpl_id: int, start_gw: int, tournament: str | None = None,
) -> dict:
    """Replace a player away at a national-team cup with a same-position replacement.
    One active entry per manager; one replacement for the whole absence. Keeper
    eligibility is preserved while away (covered in the keeper-drop derivation)."""
    from models import InternationalList

    manager = _resolve_manager(db, league, fpl_manager_id)
    away = _resolve_player(db, away_fpl_id)
    replacement = _resolve_player(db, replacement_fpl_id)
    if away.id == replacement.id:
        raise RuleViolation("replacement must be a different player")
    _refuse_goalkeeper_list_move(league, away, replacement, what="international list")
    if not il_same_position(away.position, replacement.position):
        raise RuleViolation(
            f"replacement is {replacement.position}, must match the away "
            f"player's position {away.position}"
        )
    existing = (
        db.query(InternationalList).filter_by(manager_id=manager.id, status="active").first()
    )
    if existing:
        raise RuleViolation("manager already has an active international-list player")
    entry = InternationalList(
        player_id=away.id, manager_id=manager.id, start_gw=start_gw,
        replacement_id=replacement.id, tournament=tournament or None, status="active",
    )
    db.add(entry)
    record_audit(db, league, action="intl.place",
                 summary=(f"{manager.display} placed {away.name} ({away.position}) on the "
                          f"international list → {replacement.name} (GW{start_gw}"
                          + (f", {tournament}" if tournament else "") + ")"),
                 manager_ids=[manager.id],
                 details={"away_fpl_id": away_fpl_id, "replacement_fpl_id": replacement_fpl_id,
                          "start_gw": start_gw, "tournament": tournament})
    db.commit()
    db.refresh(entry)
    return {"player": away.name, "replacement": replacement.name, "start_gw": start_gw}


def return_from_intl(db: Session, league: League, intl_id: str, return_gw: int) -> dict:
    """Re-add a returning player (their nation was eliminated). No minimum stay — the
    replacement is dropped to make room (the manager picks the returner back up)."""
    from models import InternationalList

    entry = db.get(InternationalList, intl_id)
    if not entry:
        raise RuleViolation("international-list entry not found")
    if entry.status != "active":
        raise RuleViolation(f"international-list entry is already '{entry.status}'")
    entry.end_gw = return_gw
    entry.status = "returned"
    away = db.get(Player, entry.player_id)
    mgr = db.get(Manager, entry.manager_id)
    record_audit(db, league, action="intl.return",
                 summary=(f"{mgr.display if mgr else '—'} returned "
                          f"{away.name if away else '—'} from the international list "
                          f"(GW{return_gw})"),
                 manager_ids=[entry.manager_id],
                 details={"intl_id": str(intl_id), "return_gw": return_gw})
    db.commit()
    return {"returned_gw": return_gw}


def get_transactions(db: Session, league: League) -> list[dict]:
    """Weekly add/drops derived from consecutive per-GW roster snapshots (the FPL Draft
    waiver feed isn't public). Grouped newest-GW first: for each manager, players in
    GW n but not n-1 = added; in n-1 but not n = dropped. Captures waivers/FA/trades."""
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    pnames = player_names(db, league)
    # (manager_id, gw_number) -> set(player_id)
    rosters: dict = {}
    for mid, pid, gnum in (
        db.query(Roster.manager_id, Roster.player_id, Gameweek.number)
        .join(Gameweek, Gameweek.id == Roster.gameweek_id)
        .filter(Gameweek.league_id == league.id)
    ):
        rosters.setdefault((mid, gnum), set()).add(pid)

    gws = sorted({gnum for (_mid, gnum) in rosters})
    by_gw: dict = {}
    for i in range(1, len(gws)):
        prev_gw, gw = gws[i - 1], gws[i]
        for mid in names:
            before = rosters.get((mid, prev_gw), set())
            after = rosters.get((mid, gw), set())
            if not before or not after:
                continue
            for pid in (after - before):
                by_gw.setdefault(gw, []).append(
                    {"manager": names[mid], "player": pnames.get(pid, "?"), "action": "added"}
                )
            for pid in (before - after):
                by_gw.setdefault(gw, []).append(
                    {"manager": names[mid], "player": pnames.get(pid, "?"), "action": "dropped"}
                )
    return [
        {"gameweek": gw, "moves": sorted(by_gw[gw], key=lambda x: (x["manager"], x["action"]))}
        for gw in sorted(by_gw, reverse=True)
    ]


def player_pool_freshness(db: Session) -> dict:
    """When the global player pool was last pulled, and how much of it is live.

    `players` accumulates: a player who leaves the PL keeps their row (so history
    still renders) but loses their `fpl_id`, since that slot goes back to FPL. So
    "in the current pool" means fpl_id IS NOT NULL, and the rest are historical.
    """
    from models import SyncLog

    last = (
        db.query(SyncLog)
        .filter(SyncLog.kind == "players", SyncLog.ok.is_(True))
        .order_by(SyncLog.started_at.desc())
        .first()
    )
    total = db.query(Player).count()
    live = db.query(Player).filter(Player.fpl_id.isnot(None)).count()
    return {
        "synced_at": last.started_at if last else None,
        "notes": last.notes if last else None,
        "live": live,
        "historical": total - live,
    }


def player_portal(
    db: Session, league: League, *, viewer_is_owner: bool = False
) -> list[dict]:
    """Every player with league context (owner, on-IL, ineligible, keeper
    acquisition/years/eligibility) for the data portal, plus that player's
    statistics from the most recent COMPLETED season (see stats_season) and, in the
    `proj_*` keys, an imported outside forecast for the projected season (see
    projection_season_year). One row per player; stat and projection fields are None
    for anyone the respective season didn't cover.

    Projections are the owner's alone to see (CLAUDE.md) — `viewer_is_owner` defaults
    to False, the same "disclose nothing unless told to" default
    `_derive_keeper_status`'s `kept_for`/`kept_all` already use, so a caller that
    forgets to pass it leaks nothing.
    """
    gw = latest_gameweek(db, league)
    owner_by_pid: dict = {}
    if gw:
        for mid, pid in db.query(Roster.manager_id, Roster.player_id).filter_by(gameweek_id=gw.id):
            owner_by_pid[pid] = mid
        # Must move in step with _derive_keeper_status below: this row renders the
        # owner and the keeper facts side by side, so overlaying only one of them
        # shows the new owner with a blank keeper column.
        owner_by_pid.update(player_ownership(db, league))
    # A SEPARATE overlay, deliberately not folded into owner_by_pid above: keeper
    # facts are a question about last season's tenure, and a player drafted thirty
    # seconds ago has none yet under his new manager — showing blank there is
    # correct, not a bug. This one only feeds the displayed `owner`/`rostered`
    # fields, so the live draft shows up immediately without corrupting keeper
    # status. Same DraftPick shape search_players already uses for "taken".
    upcoming = (league.season_year or 0) + 1
    display_owner_by_pid = dict(owner_by_pid)
    for pid, mid in (
        db.query(DraftPick.player_id, DraftPick.manager_id)
        .filter(DraftPick.league_id == league.id, DraftPick.season_year == upcoming,
                DraftPick.player_id.isnot(None))
    ):
        display_owner_by_pid[pid] = mid
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    il_pids = {
        e.player_id for e in
        db.query(InjuryList).join(Manager, Manager.id == InjuryList.manager_id)
        .filter(Manager.league_id == league.id, InjuryList.status == "active")
    }
    inelig = _ineligible_fpl_ids(db, league)
    kstatus = _derive_keeper_status(db, league)

    def _f(v):  # numeric-ish strings -> float, else None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _value(points, price):
        """Projected points per £m. Derived on read, never stored, so it cannot drift
        from the two numbers either side of it. `not price` catches 0.0 as well as
        None, so a priceless row renders blank rather than raising."""
        if points is None or not price:
            return None
        return points / price

    # Identity + league context come from the live pool; STATISTICS come from the
    # most recent completed season's snapshot. `players` only ever holds the current
    # season, which during a draft is all zeros.
    stats_lg = stats_season(db, league)
    stats = season_identity(db, stats_lg)

    # Projections are an outside forecast on their own table, joined in memory by
    # players.id like the stats above. Left-join semantics matter: the historical
    # players in this list will never have one, and a few live ones are missing from
    # the file. They stay in the list, blank.
    # Don't even ASK when the viewer can't see it: same "no viewer, no query" shape
    # _derive_keeper_status uses for its own kept lookup — the redaction is then a
    # fact about the SQL, not a promise that might drift from what's rendered.
    proj_year = projection_season_year(db) if viewer_is_owner else None
    proj = projection_index(db, proj_year) if proj_year else {}

    rows = []
    for p in db.query(Player).order_by(Player.name):
        owner_mid = owner_by_pid.get(p.id)
        display_owner_mid = display_owner_by_pid.get(p.id)
        ks = kstatus.get(owner_mid, {}).get(p.id) if owner_mid else None
        s = stats.get(p.id)  # None for players absent that season
        pr = proj.get(p.id)  # None for anyone the projection file didn't cover
        rows.append({
            "fpl_id": p.fpl_id, "name": p.name, "position": p.position,
            "team": p.current_team, "status": p.status, "news": p.news,
            "price": (p.price / 10) if p.price is not None else None,
            "total_points": s.total_points if s else None,
            "points_per_game": _f(s.points_per_game) if s else None,
            "goals_scored": s.goals_scored if s else None,
            "assists": s.assists if s else None,
            "clean_sheets": s.clean_sheets if s else None,
            "bonus": s.bonus if s else None,
            "minutes": s.minutes if s else None,
            "ict_index": _f(s.ict_index) if s else None,
            # Keys mirror the projection model's field names, so there is no
            # translation layer to keep in step; proj_value is the only derived one.
            # proj_price is already £m — do NOT divide it by 10 like Player.price.
            "proj_price": pr.price if pr else None,
            "proj_points": pr.points if pr else None,
            "proj_value": _value(pr.points, pr.price) if pr else None,
            "proj_minutes": pr.minutes if pr else None,
            "proj_goals": pr.goals_scored if pr else None,
            "proj_assists": pr.assists if pr else None,
            "proj_clean_sheets": pr.clean_sheets if pr else None,
            "proj_bonus": pr.bonus if pr else None,
            "proj_defensive_contributions": pr.defensive_contributions if pr else None,
            "proj_yellow_cards": pr.yellow_cards if pr else None,
            "owner": names.get(display_owner_mid), "rostered": display_owner_mid is not None,
            "on_il": p.id in il_pids, "ineligible": p.fpl_id in inelig,
            "acquisition": ks["acquisition"] if ks else None,
            "keeper_years": ks["years_remaining"] if ks else None,
            "keeper_eligible": (ks["eligible"] if ks else None),
        })
    return rows


def reconcile_absences(db: Session, league: League) -> int:
    """After a sync: auto-close any active IL / international-list entry whose player is
    back on the manager's latest synced roster — i.e. the manager re-added them in the
    FPL app. Sets the entry to 'returned' at the latest GW. Returns the number closed."""
    from models import InternationalList

    gw = latest_gameweek(db, league)
    if not gw:
        return 0
    on_roster = {
        (mid, pid)
        for mid, pid in db.query(Roster.manager_id, Roster.player_id).filter_by(gameweek_id=gw.id)
    }
    closed = 0
    for model in (InjuryList, InternationalList):
        for e in (
            db.query(model)
            .join(Manager, Manager.id == model.manager_id)
            .filter(Manager.league_id == league.id, model.status == "active")
            .all()
        ):
            if (e.manager_id, e.player_id) in on_roster:
                e.end_gw = gw.number
                e.status = "returned"
                closed += 1
                kind = "il" if model is InjuryList else "intl"
                mgr = db.get(Manager, e.manager_id)
                player = db.get(Player, e.player_id)
                record_audit(
                    db, league, action=f"{kind}.auto_return",
                    summary=(f"Auto-returned {player.name if player else '—'} from "
                             f"{'IL' if kind == 'il' else 'the international list'} for "
                             f"{mgr.display if mgr else '—'} (re-added in FPL, GW{gw.number})"),
                    manager_ids=[e.manager_id],
                    details={"return_gw": gw.number})
    if closed:
        db.commit()
    return closed


def flagged_actions(db: Session, league: League) -> list[dict]:
    """League attention items for the home page: IL/international players that must be
    returned at season end, players on the IL 4+ GWs (eligible to return), and teams
    flagged or at risk of an anti-tanking violation."""
    from models import InternationalList

    cur = current_gameweek(db, league)
    season_over = cur is not None and cur >= SEASON_LAST_GW
    out: list[dict] = []

    # IL: season-end return-or-release, and 4+ GW eligible-to-return
    for il, m, p in (
        db.query(InjuryList, Manager, PlayerSeason)
        .join(Manager, Manager.id == InjuryList.manager_id)
        .join(PlayerSeason, PlayerSeason.player_id == InjuryList.player_id)
        .filter(Manager.league_id == league.id, InjuryList.status == "active",
                PlayerSeason.league_id == league.id)
    ):
        gws_on = (cur - il.start_gw + 1) if (cur and il.start_gw) else None
        if season_over:
            out.append({"category": "Injury list", "manager": m.display,
                        "detail": f"Season over — return or release {p.name}"})
        elif gws_on is not None and gws_on >= MIN_IL_STAY_GWS:
            out.append({"category": "Injury list", "manager": m.display,
                        "detail": f"{p.name} on the IL {gws_on} GWs — eligible to return"})

    # International: season-end return
    for il, m, p in (
        db.query(InternationalList, Manager, PlayerSeason)
        .join(Manager, Manager.id == InternationalList.manager_id)
        .join(PlayerSeason, PlayerSeason.player_id == InternationalList.player_id)
        .filter(Manager.league_id == league.id, InternationalList.status == "active",
                PlayerSeason.league_id == league.id)
    ):
        if season_over:
            out.append({"category": "International", "manager": m.display,
                        "detail": f"Season over — return {p.name} from international duty"})

    # Anti-tanking: flagged (in violation) or at risk (one GW short of the threshold).
    # Dismissed windows drop out, so this table can't contradict the flag list below it.
    by_manager = _tanking_counts_by_manager(db, league)
    open_windows = _open_windows(db, league, by_manager)
    for mid, info in by_manager.items():
        counts = info["counts"]
        if mid in open_windows:
            out.append({"category": "Anti-tanking", "manager": info["manager"].display,
                        "detail": "flagged for an anti-tanking violation"})
        else:
            streak = current_tanking_streak(counts)
            if streak and streak >= ANTI_TANKING_MIN_WEEKS - 1:
                out.append({"category": "Anti-tanking", "manager": info["manager"].display,
                            "detail": f"at risk — {streak} straight GWs near the threshold"})
    return out


def get_international_list(db: Session, league: League) -> list[dict]:
    """Active international-list entries for the league."""
    from models import InternationalList

    rows = (
        db.query(InternationalList, Manager, PlayerSeason)
        .join(Manager, Manager.id == InternationalList.manager_id)
        .join(PlayerSeason, PlayerSeason.player_id == InternationalList.player_id)
        .filter(Manager.league_id == league.id, InternationalList.status == "active",
                PlayerSeason.league_id == league.id)
        .all()
    )
    return [
        {"manager": m.display, "player": p.name, "tournament": il.tournament,
         "start_gw": il.start_gw}
        for il, m, p in rows
    ]


# ---- cups (auto-bracket from GW28 standings, auto-scored 2-GW totals) ----
def seed_managers(db: Session, league: League, through_gw: int = CUP_SEED_THROUGH_GW):
    """Managers ranked 1..N by H2H standings through `through_gw` (cup seeding)."""
    rows = (
        db.query(Match)
        .join(Gameweek, Gameweek.id == Match.gameweek_id)
        .filter(
            Match.league_id == league.id,
            Match.finished.is_(True),
            Gameweek.number <= through_gw,
        )
        .all()
    )
    results = [
        (m.home_manager_id, m.away_manager_id, m.home_points or 0, m.away_points or 0)
        for m in rows
    ]
    order = h2h_standings(results)
    by_id = {m.id: m for m in db.query(Manager).filter_by(league_id=league.id).all()}
    seeded = [by_id[mid] for mid in order if mid in by_id]
    seeded += [m for m in by_id.values() if m not in seeded]  # any with no matches
    return seeded


def _two_gw_total(db: Session, league: League, manager_id, gw_numbers) -> int:
    rows = (
        db.query(GameweekPoints.total_points)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(
            GameweekPoints.manager_id == manager_id,
            Gameweek.league_id == league.id,
            Gameweek.number.in_(gw_numbers),
        )
        .all()
    )
    return sum((r[0] or 0) for r in rows)


def _two_gw_tiebreak(db: Session, league: League, manager_id, gw_numbers) -> tuple:
    """Team (goals, assists, clean_sheets) totals over a cup match's gameweeks —
    the cup tiebreakers, in priority order."""
    rows = (
        db.query(
            GameweekPoints.team_goals,
            GameweekPoints.team_assists,
            GameweekPoints.team_clean_sheets,
        )
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(
            GameweekPoints.manager_id == manager_id,
            Gameweek.league_id == league.id,
            Gameweek.number.in_(gw_numbers),
        )
        .all()
    )
    g = sum((r[0] or 0) for r in rows)
    a = sum((r[1] or 0) for r in rows)
    cs = sum((r[2] or 0) for r in rows)
    return (g, a, cs)


def _get_tournament(db: Session, league: League, name: str):
    return (
        db.query(Tournament)
        .filter_by(league_id=league.id, name=name)
        .one_or_none()
    )


def _round_matches(db: Session, tournament, round_no: int):
    return (
        db.query(TournamentMatch)
        .filter_by(tournament_id=tournament.id, round=round_no)
        .all()
    )


def _loser(m: TournamentMatch):
    return m.manager_a if m.winner_id == m.manager_b else m.manager_b


def _find_by_seeds(matches, seed_map, seeds: set):
    for m in matches:
        if {seed_map.get(m.manager_a), seed_map.get(m.manager_b)} == seeds:
            return m
    return None


def generate_cups(db: Session, league: League, through_gw: int = CUP_SEED_THROUGH_GW):
    """Seed from GW`through_gw` standings and create Cup (top 6) + Pup Cup
    (bottom 4) with their first-round matches. Regenerates if cups already exist."""
    seeds = seed_managers(db, league, through_gw)
    if len(seeds) < 10:
        raise RuleViolation(f"need 10 seeded managers, found {len(seeds)}")

    for t in (
        db.query(Tournament)
        .filter(Tournament.league_id == league.id, Tournament.name.in_(["Cup", "Pup Cup"]))
        .all()
    ):
        db.query(TournamentMatch).filter_by(tournament_id=t.id).delete()
        db.delete(t)
    db.flush()

    cup = Tournament(name="Cup", league_id=league.id)
    pup = Tournament(name="Pup Cup", league_id=league.id)
    db.add_all([cup, pup])
    db.flush()

    def add(t, rnd, a, b):
        db.add(
            TournamentMatch(
                tournament_id=t.id, round=rnd, manager_a=a.id, manager_b=b.id
            )
        )

    # seeds[0]=seed1. Cup QF: 3v6, 4v5 (seeds 1,2 bye). Pup play-in: 7v10, 8v9.
    add(cup, 1, seeds[2], seeds[5])
    add(cup, 1, seeds[3], seeds[4])
    add(pup, 1, seeds[6], seeds[9])
    add(pup, 1, seeds[7], seeds[8])
    record_audit(db, league, action="cup.generate",
                 summary=f"Generated Cup + Pup Cup brackets (seeded through GW{through_gw})",
                 manager_ids=[s.id for s in seeds],
                 details={"through_gw": through_gw})
    db.commit()
    return get_cups(db, league)


def score_cup_round(db: Session, league: League, round_no: int, gw1: int, gw2: int):
    """Auto-score every match in `round_no` from 2-GW totals, set winners, then
    generate the next round (with Cup QF losers feeding the Pup Cup)."""
    seeds = seed_managers(db, league)
    seed_map = {m.id: i + 1 for i, m in enumerate(seeds)}
    cup = _get_tournament(db, league, "Cup")
    pup = _get_tournament(db, league, "Pup Cup")
    if not cup or not pup:
        raise RuleViolation("cups not generated yet")

    cup_r = _round_matches(db, cup, round_no)
    pup_r = _round_matches(db, pup, round_no)
    if not (cup_r or pup_r):
        raise RuleViolation(f"no round {round_no} matches to score")

    gws = [gw1, gw2]
    for m in cup_r + pup_r:
        m.score_a = _two_gw_total(db, league, m.manager_a, gws)
        m.score_b = _two_gw_total(db, league, m.manager_b, gws)
        side = match_winner(
            m.score_a, m.score_b,
            seed_map.get(m.manager_a, 99), seed_map.get(m.manager_b, 99),
            _two_gw_tiebreak(db, league, m.manager_a, gws),
            _two_gw_tiebreak(db, league, m.manager_b, gws),
        )
        m.winner_id = m.manager_a if side == "a" else m.manager_b
    db.flush()

    if round_no == 1:
        qf_36 = _find_by_seeds(cup_r, seed_map, {3, 6})
        qf_45 = _find_by_seeds(cup_r, seed_map, {4, 5})
        pi_710 = _find_by_seeds(pup_r, seed_map, {7, 10})
        pi_89 = _find_by_seeds(pup_r, seed_map, {8, 9})
        # Cup SF re-seeds: seed 1 plays the LOWEST remaining seed (worst surviving
        # winner), seed 2 plays the HIGHEST remaining seed (best surviving winner).
        winners = sorted([qf_36.winner_id, qf_45.winner_id], key=lambda mid: seed_map.get(mid, 99))
        best_remaining, worst_remaining = winners[0], winners[1]
        db.add(TournamentMatch(tournament_id=cup.id, round=2, manager_a=seeds[0].id, manager_b=worst_remaining))
        db.add(TournamentMatch(tournament_id=cup.id, round=2, manager_a=seeds[1].id, manager_b=best_remaining))
        # Pup SF: Cup QF losers vs play-in winners
        db.add(TournamentMatch(tournament_id=pup.id, round=2, manager_a=_loser(qf_45), manager_b=pi_89.winner_id))
        db.add(TournamentMatch(tournament_id=pup.id, round=2, manager_a=_loser(qf_36), manager_b=pi_710.winner_id))
    elif round_no == 2:
        # Cup: final (SF winners) + 3rd-place playoff (SF losers).
        db.add(TournamentMatch(tournament_id=cup.id, round=3, manager_a=cup_r[0].winner_id, manager_b=cup_r[1].winner_id))
        db.add(TournamentMatch(tournament_id=cup.id, round=3, manager_a=_loser(cup_r[0]), manager_b=_loser(cup_r[1])))
        # Pup Cup: final only (only the winner is paid).
        db.add(TournamentMatch(tournament_id=pup.id, round=3, manager_a=pup_r[0].winner_id, manager_b=pup_r[1].winner_id))

    scored = cup_r + pup_r
    record_audit(db, league, action="cup.score",
                 summary=f"Scored cup round {round_no} (GW{gw1}+{gw2}): {len(scored)} match(es)",
                 manager_ids=[mid for m in scored for mid in (m.manager_a, m.manager_b) if mid],
                 details={"round": round_no, "gw1": gw1, "gw2": gw2})
    db.commit()
    return get_cups(db, league)


def _historical_cup_winners(db: Session, league: League):
    """(cup_winner_manager_id, pup_winner_manager_id) from the imported season_history
    for this league's season — matched to a current manager by display name. Either may
    be None. Lets old seasons (no live bracket) still pay cup/pup winnings."""
    from models import SeasonHistory

    sy = league.season_year
    year_str = f"{sy % 100:02d}/{(sy + 1) % 100:02d}" if sy else None
    row = (
        db.query(SeasonHistory)
        .filter_by(league_id=league.id, year=year_str)
        .one_or_none()
        if year_str else None
    )
    if not row:
        return None, None
    by_name = {}
    for m in db.query(Manager).filter_by(league_id=league.id):
        if m.display:
            by_name[m.display.strip().lower()] = m.id
    resolve = lambda name: by_name.get(name.strip().lower()) if name else None
    return resolve(row.cup_winner), resolve(row.pup_winner)


def override_cup_match(db: Session, league: League, match_id: str, score_a: int, score_b: int) -> dict:
    """Admin: hand-set a cup match's two scores and recompute its winner. Used for the
    DGW 'first game only' case (and any manual correction), since per-fixture splitting
    isn't auto. Does NOT regenerate later rounds — score the round normally to advance."""
    m = db.get(TournamentMatch, match_id)
    if not m:
        raise RuleViolation("cup match not found")
    seeds = seed_managers(db, league)
    seed_map = {mm.id: i + 1 for i, mm in enumerate(seeds)}
    m.score_a, m.score_b = score_a, score_b
    side = match_winner(score_a, score_b, seed_map.get(m.manager_a, 99), seed_map.get(m.manager_b, 99))
    m.winner_id = m.manager_a if side == "a" else m.manager_b
    win = db.get(Manager, m.winner_id)
    record_audit(db, league, action="cup.override",
                 summary=(f"Overrode cup match score to {score_a}–{score_b}"
                          f" (winner: {win.display if win else '—'})"),
                 manager_ids=[mid for mid in (m.manager_a, m.manager_b) if mid],
                 details={"match_id": str(match_id), "score_a": score_a, "score_b": score_b})
    db.commit()
    return {"match": match_id, "score_a": score_a, "score_b": score_b}


SHIELD_NAME = "Pupmunity Shield"


def prior_season_shield_participants(db: Session, league: League):
    """Suggested Shield participants: the PRIOR season's Cup + Pup winners, mapped to
    THIS league's managers by stable FPL entry id. Returns (cup_fpl, pup_fpl), either
    may be None. Reads the prior season's live tournaments, else its season_history."""
    prior = (
        db.query(League)
        .filter(League.season_year == (league.season_year or 0) - 1)
        .order_by(League.season_year.desc())
        .first()
    )
    if not prior:
        return None, None
    cup_id, pup_id = None, None
    cup = _get_tournament(db, prior, "Cup")
    if cup:
        final, _ = _cup_final_and_third(db, cup)
        cup_id = final.winner_id if final else None
    pup = _get_tournament(db, prior, "Pup Cup")
    if pup:
        pf = _round_matches(db, pup, 3)
        pup_id = pf[0].winner_id if pf and pf[0].winner_id else None
    if cup_id is None or pup_id is None:
        h_cup, h_pup = _historical_cup_winners(db, prior)
        cup_id = cup_id or h_cup
        pup_id = pup_id or h_pup
    # map the prior-season manager ids -> this season's managers by entry id
    def to_current(prior_mid):
        if not prior_mid:
            return None
        pm = db.get(Manager, prior_mid)
        if not pm:
            return None
        cur = db.query(Manager).filter_by(
            league_id=league.id, fpl_manager_id=pm.fpl_manager_id
        ).one_or_none()
        return cur.fpl_manager_id if cur else None
    return to_current(cup_id), to_current(pup_id)


def set_shield(db: Session, league: League, *, cup_winner_fpl: str, pup_winner_fpl: str) -> dict:
    """Create/replace the Pupmunity Shield: last season's Cup winner vs Pup winner,
    played in GW1. One match; scored later via score_shield."""
    a = _resolve_manager(db, league, cup_winner_fpl)
    b = _resolve_manager(db, league, pup_winner_fpl)
    if a.id == b.id:
        raise RuleViolation("the two Shield teams must be different")
    existing = _get_tournament(db, league, SHIELD_NAME)
    if existing:
        db.query(TournamentMatch).filter_by(tournament_id=existing.id).delete()
        db.delete(existing)
        db.flush()
    shield = Tournament(name=SHIELD_NAME, league_id=league.id, start_gw=1, end_gw=1)
    db.add(shield)
    db.flush()
    db.add(TournamentMatch(tournament_id=shield.id, round=1, manager_a=a.id, manager_b=b.id))
    record_audit(db, league, action="shield.set",
                 summary=f"Set Pupmunity Shield: {a.display} (Cup) vs {b.display} (Pup)",
                 manager_ids=[a.id, b.id])
    db.commit()
    return {"cup_winner": a.display, "pup_winner": b.display}


def score_shield(db: Session, league: League, gw: int = 1) -> dict:
    """Score the Shield from a single gameweek's totals; set the winner."""
    shield = _get_tournament(db, league, SHIELD_NAME)
    if not shield:
        raise RuleViolation("Shield not set up yet")
    matches = _round_matches(db, shield, 1)
    if not matches:
        raise RuleViolation("Shield has no match")
    m = matches[0]
    m.score_a = _two_gw_total(db, league, m.manager_a, [gw])
    m.score_b = _two_gw_total(db, league, m.manager_b, [gw])
    side = match_winner(
        m.score_a, m.score_b, 1, 2,
        _two_gw_tiebreak(db, league, m.manager_a, [gw]),
        _two_gw_tiebreak(db, league, m.manager_b, [gw]),
    )
    m.winner_id = m.manager_a if side == "a" else m.manager_b
    win = db.get(Manager, m.winner_id)
    record_audit(db, league, action="shield.score",
                 summary=(f"Scored Pupmunity Shield (GW{gw}): {m.score_a}–{m.score_b}, "
                          f"winner {win.display if win else '—'}"),
                 manager_ids=[mid for mid in (m.manager_a, m.manager_b) if mid],
                 details={"gw": gw, "score_a": m.score_a, "score_b": m.score_b})
    db.commit()
    return {"winner": win.display}


def get_shield(db: Session, league: League) -> dict | None:
    """The Shield match for display (participants, score, winner) or None."""
    shield = _get_tournament(db, league, SHIELD_NAME)
    if not shield:
        return None
    matches = _round_matches(db, shield, 1)
    if not matches:
        return None
    m = matches[0]
    names = {mm.id: mm.display for mm in db.query(Manager).filter_by(league_id=league.id)}
    return {
        "id": str(m.id),
        "cup_winner": names.get(m.manager_a), "pup_winner": names.get(m.manager_b),
        "score_a": m.score_a, "score_b": m.score_b,
        "winner": names.get(m.winner_id) if m.winner_id else None,
    }


def _shield_lines(db: Session, league: League) -> dict:
    """Pupmunity Shield payout lines: each of the 2 teams pays the entry; the winner
    collects both entries. {manager_id: [(label, amount)]}."""
    shield = _get_tournament(db, league, SHIELD_NAME)
    if not shield:
        return {}
    matches = _round_matches(db, shield, 1)
    if not matches:
        return {}
    m = matches[0]
    entry = PAYOUT_STRUCTURE["shield_entry"]
    extra: dict = {}
    for mid in (m.manager_a, m.manager_b):
        extra.setdefault(mid, []).append(("Pupmunity Shield entry", -float(entry)))
    if m.winner_id:
        extra.setdefault(m.winner_id, []).append(("Pupmunity Shield", float(entry) * 2))
    return extra


def add_side_payout(
    db: Session, league: League, *, fpl_manager_id: str, label: str, amount: int,
    gameweek: int | None = None,
) -> dict:
    """Record a side-pot credit/debit (weekly-entry pool, team-sale clause, etc.)."""
    from models import SidePayout

    manager = _resolve_manager(db, league, fpl_manager_id)
    if not amount:
        raise RuleViolation("enter a non-zero amount")
    if not label.strip():
        raise RuleViolation("enter a label")
    db.add(SidePayout(league_id=league.id, manager_id=manager.id, label=label.strip(),
                      amount=amount, gameweek=gameweek))
    record_audit(db, league, action="side_payout.add",
                 summary=f"Side payout for {manager.display}: ${amount} — {label.strip()}",
                 manager_ids=[manager.id],
                 details={"amount": amount, "label": label.strip(), "gameweek": gameweek})
    db.commit()
    return {"manager": manager.display, "amount": amount}


def delete_side_payout(db: Session, league: League, side_id: str) -> None:
    from models import SidePayout

    row = db.query(SidePayout).filter_by(league_id=league.id, id=side_id).one_or_none()
    if not row:
        raise RuleViolation("side payout not found")
    mgr = db.get(Manager, row.manager_id)
    record_audit(db, league, action="side_payout.delete",
                 summary=(f"Removed side payout for {mgr.display if mgr else '—'}: "
                          f"${row.amount} — {row.label}"),
                 manager_ids=[row.manager_id],
                 details={"amount": row.amount, "label": row.label})
    db.delete(row)
    db.commit()


def get_side_payouts(db: Session, league: League) -> list[dict]:
    from models import SidePayout

    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    rows = (
        db.query(SidePayout).filter_by(league_id=league.id)
        .order_by(SidePayout.created_at.desc()).all()
    )
    return [
        {"id": str(s.id), "manager": names.get(s.manager_id), "label": s.label,
         "amount": s.amount, "gameweek": s.gameweek}
        for s in rows
    ]


def _side_payout_lines(db: Session, league: League) -> dict:
    """{manager_id: [(label, amount)]} side-pot lines for the winnings table."""
    from models import SidePayout

    extra: dict = {}
    for s in db.query(SidePayout).filter_by(league_id=league.id):
        extra.setdefault(s.manager_id, []).append((s.label, float(s.amount)))
    return extra


def weekly_winnings(db: Session, league: League) -> dict:
    """Auto weekly pool: the highest gameweek_points total each GW wins
    PAYOUT_STRUCTURE['weekly_prize'] (split equally on ties). Returns
    {manager_id: total_won} across all played gameweeks."""
    by_gw: dict = {}  # gw number -> {manager_id: total}
    for mid, total, gnum in (
        db.query(GameweekPoints.manager_id, GameweekPoints.total_points, Gameweek.number)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(Gameweek.league_id == league.id)
    ):
        by_gw.setdefault(gnum, {})[mid] = total or 0
    prize = PAYOUT_STRUCTURE["weekly_prize"]
    won: dict = {}
    for scores in by_gw.values():
        top = max(scores.values(), default=0)
        if top <= 0:  # unplayed / no data
            continue
        winners = [mid for mid, s in scores.items() if s == top]
        share = prize / len(winners)
        for mid in winners:
            won[mid] = won.get(mid, 0.0) + share
    return won


def _weekly_pool_lines(db: Session, league: League) -> dict:
    """Weekly-pool winnings lines: every manager pays the annual entry; weekly winners
    get their $10/GW share. {manager_id: [(label, amount)]}."""
    entry = PAYOUT_STRUCTURE["weekly_entry"]
    won = weekly_winnings(db, league)
    extra: dict = {}
    for m in db.query(Manager).filter_by(league_id=league.id):
        extra[m.id] = [("Weekly pool entry", -float(entry))]
    for mid, amt in won.items():
        extra.setdefault(mid, []).append(("Weekly winnings", round(amt, 2)))
    return extra


def _cup_final_and_third(db: Session, cup: Tournament):
    """Identify the Cup's final vs 3rd-place match: the final is between the two
    semifinal winners; the other round-3 match is the 3rd-place playoff."""
    sf_winners = {m.winner_id for m in _round_matches(db, cup, 2)}
    r3 = _round_matches(db, cup, 3)
    final = next(
        (m for m in r3 if m.manager_a in sf_winners and m.manager_b in sf_winners), None
    )
    third = next((m for m in r3 if m is not final), None)
    return final, third


def get_cups(db: Session, league: League) -> list[dict]:
    """Read both cup brackets (matches grouped by round) for API/homepage."""
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    out = []
    for t in (
        db.query(Tournament)
        .filter(Tournament.league_id == league.id, Tournament.name.in_(["Cup", "Pup Cup"]))
        .order_by(Tournament.name)  # "Cup" before "Pup Cup"
        .all()
    ):
        labels = {
            1: "Quarterfinal" if t.name == "Cup" else "Play-in",
            2: "Semifinal",
            3: "Final",
        }
        third_id = None
        if t.name == "Cup":
            _, third = _cup_final_and_third(db, t)
            third_id = third.id if third else None
        matches = (
            db.query(TournamentMatch)
            .filter_by(tournament_id=t.id)
            .order_by(TournamentMatch.round)
            .all()
        )
        out.append(
            {
                "name": t.name,
                "matches": [
                    {
                        "id": str(m.id),
                        "round": m.round,
                        "round_label": "3rd-place"
                        if m.id == third_id
                        else labels.get(m.round, f"Round {m.round}"),
                        "home": names.get(m.manager_a),
                        "away": names.get(m.manager_b),
                        "home_score": m.score_a,
                        "away_score": m.score_b,
                        "winner": names.get(m.winner_id) if m.winner_id else None,
                    }
                    for m in matches
                ],
            }
        )
    return out


def get_payouts(db: Session, league: League, other_fines: float = 0.0) -> dict:
    """Season-end payouts + overall winnings from final standings + cup results
    (precomputed read).

    Resolves recipient slots (league 1/2/3 + last from the ADJUSTED standings —
    `get_standings`, with `standing_adjustments` applied and re-ranked; cup 1/2/3 and
    pup champion from the brackets) and applies the configured payout structure.
    Pulls per-manager fines from the fines table (winner collects the pool); each
    manager's `net` is their payout minus the buy-in (overall winnings). Every
    manager is listed (those with no payout show net = -entry_fee - fines).
    """
    # The four position slots follow the ADJUSTED standings, not the raw synced
    # `Standing.rank`. A commissioner deduction changes where a team finished, and
    # 1st/2nd/3rd/last are a consequence of where they finished — sorting the synced
    # rank meant the winnings table and the standings table on the SAME page could
    # name different people. Reuses get_standings, as _reverse_standings_managers does
    # for the draft order, so there is one definition of "the standings" and the
    # tie-breaks can't drift apart.
    by_fpl = {m.fpl_manager_id: m for m in db.query(Manager).filter_by(league_id=league.id)}
    # Best first, adjusted. A manager with no Standing row is absent from get_standings,
    # so they can neither win a slot nor be fined for last — they still count toward the
    # pot and owe their buy-in, exactly as the old join-based query left them.
    ranked = [
        by_fpl[row["fpl"]]
        for row in get_standings(db, league)
        if row.get("fpl") in by_fpl
    ]
    recipients: dict = {}
    if len(ranked) >= 1:
        recipients["league_1"] = ranked[0].id
    if len(ranked) >= 2:
        recipients["league_2"] = ranked[1].id
    if len(ranked) >= 3:
        recipients["league_3"] = ranked[2].id
    if ranked:
        recipients["last_place"] = ranked[-1].id

    cups_pending = False  # a bracket exists but its decisive match isn't scored yet
    cup = _get_tournament(db, league, "Cup")
    if cup:
        final, third = _cup_final_and_third(db, cup)
        if final and final.winner_id:
            recipients["cup_1"] = final.winner_id
            recipients["cup_2"] = _loser(final)
        else:
            cups_pending = True
        if third and third.winner_id:
            recipients["cup_3"] = third.winner_id
        elif third is not None:
            cups_pending = True
    pup = _get_tournament(db, league, "Pup Cup")
    pup_entrants = 0
    if pup:
        pup_entrants = len({
            mid for m in _round_matches(db, pup, 1) for mid in (m.manager_a, m.manager_b)
        })
        pup_final = _round_matches(db, pup, 3)
        if pup_final and pup_final[0].winner_id:
            recipients["pup_cup"] = pup_final[0].winner_id
        else:
            cups_pending = True

    # Historical fallback: a finished past season may have no live bracket (cups were
    # never run in-app), but the winners are recorded in season_history. Resolve
    # cup_1 / pup_cup from there so old payouts still show. (cup 2nd/3rd aren't tracked
    # in that table.)
    if not cup and not pup:
        hist_cup, hist_pup = _historical_cup_winners(db, league)
        if hist_cup and "cup_1" not in recipients:
            recipients["cup_1"] = hist_cup
        if hist_pup and "pup_cup" not in recipients:
            recipients["pup_cup"] = hist_pup
        cups_pending = False

    num_managers = db.query(Manager).filter_by(league_id=league.id).count()
    fines = _fines_by_manager_id(db, league)
    # Pup winner takes the Pup buy-in pool ($25 x entrants); default to the typical
    # 6-team field (bottom 4 + 2 Cup losers) when the entrant count is unknown.
    pup_pool = PAYOUT_STRUCTURE["pup_entry"] * (pup_entrants or 6)
    # merge side-pot lines (weekly pool + Pupmunity Shield + manual side payouts)
    extra: dict = {}
    for src in (
        _weekly_pool_lines(db, league),
        _shield_lines(db, league),
        _side_payout_lines(db, league),
    ):
        for mid, lines in src.items():
            extra.setdefault(mid, []).extend(lines)
    raw = compute_payouts(
        recipients, num_managers, other_fines=other_fines, fines=fines, pup_pool=pup_pool,
        extra=extra,
    )
    all_mgrs = db.query(Manager).filter_by(league_id=league.id).all()
    names = {m.id: m.display for m in all_mgrs}
    entry_fee = PAYOUT_STRUCTURE["entry_fee"]
    # Every manager appears: those without a payout still lost their buy-in (+ fines).
    payouts = []
    for m in all_mgrs:
        info = raw.get(m.id)
        if info:
            payouts.append({"manager": m.display, "fpl": m.fpl_manager_id, **info})
        else:
            owed = fines.get(m.id, 0)
            payouts.append({
                "manager": m.display, "fpl": m.fpl_manager_id,
                "total": -float(owed) if owed else 0.0,
                "net": round(-entry_fee - owed, 2),
                "breakdown": ([{"label": "Fine(s)", "amount": -float(owed)}] if owed else []),
            })
    payouts.sort(key=lambda x: -x["net"])
    return {
        "entry_fee": entry_fee,
        "num_managers": num_managers,
        "base_pot": entry_fee * num_managers,
        "total_paid": round(sum(p["total"] for p in payouts), 2),
        "total_fines": sum(fines.values()),
        "cups_pending": cups_pending,
        "payouts": payouts,
    }


# ---- keepers (imported seeds = years remaining; engine = start-vs-final) ----
def set_keeper_seed(
    db: Session, league: League, *, fpl_manager_id: str, player_fpl_id: int, years_remaining: int
) -> dict:
    """Set a player's keeper years-remaining for a manager (commissioner override)."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id)
    if years_remaining < 0 or years_remaining > 4:
        raise RuleViolation("years_remaining must be 0..4")
    seed = (
        db.query(KeeperSeed)
        .filter_by(manager_id=manager.id, player_id=player.id)
        .one_or_none()
    )
    if seed:
        seed.years_remaining = years_remaining
    else:
        seed = KeeperSeed(
            league_id=league.id,
            manager_id=manager.id,
            player_id=player.id,
            years_remaining=years_remaining,
            season_year=league.season_year,
        )
        db.add(seed)
    record_audit(db, league, action="keeper.seed",
                 summary=(f"Set keeper seed for {manager.display}: {player.name} = "
                          f"{years_remaining} yr(s) remaining"),
                 manager_ids=[manager.id],
                 details={"player_fpl_id": player_fpl_id, "years_remaining": years_remaining})
    db.commit()
    return {"manager": manager.display, "player": player.name, "years_remaining": years_remaining}


def set_keeper_override(
    db: Session, league: League, *, fpl_manager_id: str, player_fpl_id: int,
    years_remaining: int | None = None, acquisition: str | None = None,
) -> dict:
    """Correct a player's derived keeper facts for a manager.

    `acquisition` is 'draft' | 'waiver' | 'trade'; both fields are optional, and only
    what's passed is changed. Overriding acquisition matters because the =<2 waiver
    keeper cap counts on it, and the derivation calls any unexplained roster gap a
    drop — a missing injury-list record is enough to cost someone a waiver slot.
    """
    from rules import KEEPER_ACQUISITIONS

    if years_remaining is None and acquisition is None:
        raise RuleViolation("nothing to set")
    if acquisition is not None and acquisition not in KEEPER_ACQUISITIONS:
        raise RuleViolation(
            f"acquisition must be one of {', '.join(KEEPER_ACQUISITIONS)}"
        )
    if years_remaining is not None and not 0 <= years_remaining <= 4:
        raise RuleViolation("years_remaining must be 0..4")

    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id)
    derived = _derive_keeper_status(db, league).get(manager.id, {}).get(player.id)
    if derived is None:
        # Without this, overriding a player this manager doesn't hold defaulted
        # years_remaining to 0 and silently wrote a 0-year seed against a non-owner.
        # The UI can't reach it (the page only lists candidates) but the endpoint can.
        raise RuleViolation(
            f"{player.name} is not one of {manager.display}'s keeper candidates"
        )

    seed = (
        db.query(KeeperSeed)
        .filter_by(manager_id=manager.id, player_id=player.id)
        .one_or_none()
    )
    if not seed:
        seed = KeeperSeed(
            league_id=league.id, manager_id=manager.id, player_id=player.id,
            years_remaining=(years_remaining if years_remaining is not None
                             else derived.get("years_remaining", 0)),
            season_year=league.season_year,
        )
        db.add(seed)
    elif years_remaining is not None:
        seed.years_remaining = years_remaining
    if acquisition is not None:
        seed.acquisition = acquisition

    record_audit(
        db, league, action="keeper.override",
        summary=(f"Keeper override for {manager.display} — {player.name}: "
                 + ", ".join(filter(None, [
                     f"acquisition {derived.get('acquisition')} → {acquisition}"
                     if acquisition is not None else None,
                     f"years {derived.get('years_remaining')} → {years_remaining}"
                     if years_remaining is not None else None,
                 ]))),
        manager_ids=[manager.id],
        details={"player_fpl_id": player_fpl_id, "derived": derived,
                 "acquisition": acquisition, "years_remaining": years_remaining},
    )
    db.commit()
    return {"manager": manager.display, "player": player.name,
            "acquisition": seed.acquisition, "years_remaining": seed.years_remaining}


def keeper_overrides_context(db: Session, league: League) -> dict:
    """Per-manager rostered players with their effective keeper facts, which of those
    the commissioner has overridden, and the waiver count against the cap.

    The waiver count is the number the commissioner is actually trying to influence —
    the =<2 cap is what blocks a keeper submission — so it's shown alongside rather
    than left to be discovered at submit time.
    """
    from rules import KEEPER_MAX_WAIVER

    # kept_all: the commissioner's own page, and the "kept" column is the point of it
    status = _derive_keeper_status(db, league, kept_all=True)
    seeds = {
        (s.manager_id, s.player_id): s
        for s in db.query(KeeperSeed).filter_by(league_id=league.id)
    }
    fpl_by_pid = {p.id: p.fpl_id for p in db.query(Player)}
    out = []
    for m in (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name)
    ):
        rows = []
        for pid, v in status.get(m.id, {}).items():
            seed = seeds.get((m.id, pid))
            rows.append({
                "player": v["player"], "position": v["position"],
                "fpl_id": fpl_by_pid.get(pid),
                "acquisition": v["acquisition"],
                "years_remaining": v["years_remaining"],
                "eligible": v["eligible"],
                "kept": v.get("kept"),
                "acq_overridden": bool(seed and seed.acquisition),
                "has_seed": bool(seed),
            })
        rows.sort(key=lambda r: (r["acquisition"], r["player"]))
        waivers = sum(1 for r in rows if r["acquisition"] == "waiver" and r["eligible"])
        out.append({
            "manager": m.display, "fpl": m.fpl_manager_id, "players": rows,
            "waiver_eligible": waivers,
        })
    return {"managers": out, "max_waiver": KEEPER_MAX_WAIVER}


def clear_keeper_override(
    db: Session, league: League, *, fpl_manager_id: str, player_fpl_id: int,
) -> None:
    """Drop the correction entirely so the player falls back to the derived values."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id)
    seed = (
        db.query(KeeperSeed)
        .filter_by(manager_id=manager.id, player_id=player.id)
        .one_or_none()
    )
    if not seed:
        raise RuleViolation("no keeper override set for that player")
    record_audit(
        db, league, action="keeper.override.clear",
        summary=f"Cleared keeper override for {manager.display} — {player.name}",
        manager_ids=[manager.id],
        details={"player_fpl_id": player_fpl_id,
                 "previous": {"acquisition": seed.acquisition,
                              "years_remaining": seed.years_remaining}},
    )
    db.delete(seed)
    db.commit()


def _absence_cover(db: Session, league: League, last_n: int) -> dict:
    """(manager_id, player_id) -> the set of GW numbers an injury-list or
    international-list entry covers. An open-ended entry (no `end_gw`) folds through
    `last_n`, the final gameweek in view.

    Both lists preserve keeper eligibility, and neither absence is the manager's doing,
    so this one definition serves every reader: the keeper-drop derivation (a covered
    gap is not a drop) and the anti-tanking count (a covered player's 0 minutes are not
    held against you). They must agree on what "covered" means."""
    from models import InternationalList

    cover: dict = {}
    for model in (InjuryList, InternationalList):
        for e in (
            db.query(model)
            .join(Manager, Manager.id == model.manager_id)
            .filter(Manager.league_id == league.id)
            .all()
        ):
            cover.setdefault((e.manager_id, e.player_id), set()).update(
                range(e.start_gw, (e.end_gw or last_n) + 1)
            )
    return cover


def _roster_presence_and_il_coverage(db: Session, league: League, last_n: int) -> tuple:
    """(presence, il): `presence` is (manager_id, player_id) -> the set of GW
    numbers that manager actively rostered them; `il` is the same key -> GW
    numbers covered by an IL or international-list entry (an open-ended entry
    folds through `last_n`, the final GW). Shared by `_derive_keeper_status`
    (explains a gap for a player already a candidate) and
    `unexplained_roster_gaps` (finds a gap with no InjuryList/InternationalList
    row at all) — one source of truth for both readers."""
    presence: dict = {}
    for mid, pid, gnum in (
        db.query(Roster.manager_id, Roster.player_id, Gameweek.number)
        .join(Gameweek, Gameweek.id == Roster.gameweek_id)
        .filter(Gameweek.league_id == league.id)
        .all()
    ):
        presence.setdefault((mid, pid), set()).add(gnum)

    return presence, _absence_cover(db, league, last_n)


def unexplained_roster_gaps(
    db: Session, league: League, *, window: int = None, min_tenure: int = None
) -> list[dict]:
    """Managers whose roster shows a player active recently (within `window`
    gameweeks of the final GW), held down continuously for at least `min_tenure`
    gameweeks right up to that point, but absent at the final GW with no
    IL/international coverage explaining it — the shape of a legitimate
    injury-list absence nobody recorded (see the Šeško case, commit 4cf5d40).
    Read-only diagnostic: surfaces candidates for
    POST /admin/keepers/il-backfill, doesn't fix anything itself.

    An ordinary drop looks identical from here — that's inherent, not a bug —
    so two filters narrow this to the cases actually worth a look: `window`
    (recent enough) and `min_tenure` (a real, established run, not a one- or
    two-week streamed pickup). Checked against real prod data: `window` alone
    flagged 53 cases, nearly all ordinary short-term churn; requiring a
    meaningful prior tenure cut that down to the ones that look like Šeško's.
    Both default to their `rules.ROSTER_GAP_*` constants.
    """
    from rules import ROSTER_GAP_MIN_TENURE, ROSTER_GAP_REVIEW_WINDOW

    if window is None:
        window = ROSTER_GAP_REVIEW_WINDOW
    if min_tenure is None:
        min_tenure = ROSTER_GAP_MIN_TENURE
    gw = latest_gameweek(db, league)
    if gw is None:
        return []
    last_n = gw.number
    presence, il = _roster_presence_and_il_coverage(db, league, last_n)
    mgr_names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    mgr_fpl = {m.id: m.fpl_manager_id for m in db.query(Manager).filter_by(league_id=league.id)}
    pnames = player_names(db, league)
    fpl_by_pid = {p.id: p.fpl_id for p in db.query(Player)}

    out = []
    for (mid, pid), gws in presence.items():
        if last_n in gws:
            continue                              # still on the roster — no gap
        il_gws = il.get((mid, pid), set())
        if last_n in il_gws:
            continue                              # already explained
        last_active = max(gws)
        if last_active < last_n - window + 1:
            continue                              # dropped long ago — ordinary churn
        tenure = 0
        gwn = last_active
        while gwn in gws:
            tenure += 1
            gwn -= 1
        if tenure < min_tenure:
            continue                              # a brief streamed pickup, not an absence
        out.append({
            "manager": mgr_names.get(mid), "manager_fpl": mgr_fpl.get(mid),
            "player": pnames.get(pid, "?"), "player_fpl_id": fpl_by_pid.get(pid),
            "last_active_gw": last_active, "final_gw": last_n,
        })
    out.sort(key=lambda r: (r["manager"] or "", -r["last_active_gw"]))
    return out


def _derive_keeper_status(
    db: Session,
    league: League,
    *,
    kept_for: set | None = None,
    kept_all: bool = False,
) -> dict:
    """Core keeper derivation, shared by the report and selection validation.
    Returns {manager_id: {player_id: {player, position, acquisition,
    keeper_years, eligible}}} for players on each manager's final-GW roster.

    `kept` / `kept_discovery` are PRIVATE until keepers are revealed (see
    rules.keepers_revealed), so they DEFAULT TO FALSE FOR EVERYONE. Callers opt in:
    `kept_for` = the manager ids whose selections this viewer may see, `kept_all` =
    show every manager's. Defaulting to hidden means a new caller leaks nothing by
    forgetting to think about it — and no rule, cap or write path reads `kept`, so
    the default is safe for every internal consumer.

    The keys are always PRESENT (False when redacted), never omitted, so the /v1 JSON
    shape doesn't change with league state.
    """
    gw = latest_gameweek(db, league)
    if gw is None:
        return {}
    clubs_on = goalie_teams_on(league.goalie_team_mode)
    live_positions = (
        {pid: pos for pid, pos in db.query(Player.id, Player.position)}
        if clubs_on else {}
    )
    last_n = gw.number
    # season-scoped identity: `players` is global and always holds the
    # latest season, so names/positions off it are wrong for a past one.
    players = season_identity(db, league)

    presence, il = _roster_presence_and_il_coverage(db, league, last_n)

    # A candidate is either on the active roster at the final GW, OR still covered
    # by an open-ended IL/international entry through the final GW — a player
    # swapped out for an IL replacement and never returned is genuinely still
    # theirs for keeper purposes, even though the FPL-synced roster shows the
    # replacement in that slot at the final GW, not him.
    final_candidates = {k for k, gws in presence.items() if last_n in gws}
    final_candidates |= {k for k, covered in il.items() if last_n in covered}
    traded_in = {
        (t.to_manager, t.player_id)
        for t in db.query(Trade).filter_by(league_id=league.id)
    }
    # Keyed on (manager, player), matching the table's own unique constraint. Keying
    # on player alone let two managers' seeds for the same player collide, with one
    # silently winning.
    seed_remaining: dict = {}   # (manager_id, player_id) -> commissioner years
    seed_acq: dict = {}         # (manager_id, player_id) -> commissioner acquisition
    for s in db.query(KeeperSeed).filter_by(league_id=league.id):
        seed_remaining[(s.manager_id, s.player_id)] = s.years_remaining
        if s.acquisition:
            seed_acq[(s.manager_id, s.player_id)] = s.acquisition

    # submitted keepers for the upcoming season (so rosters can flag them locked)
    upcoming = (league.season_year or 0) + 1
    # Don't even ASK when nothing could be shown: with no viewer and no kept_all every
    # flag would be redacted to False anyway, so the query is pure waste — and not
    # issuing it turns "this caller can't see submitted keepers" from a promise about
    # the output into a fact about the SQL. draft_preparation relies on that.
    kept = {}
    if kept_all or kept_for:
        kept = {
            (s.manager_id, s.player_id): s.is_discovery
            for s in db.query(KeeperSelection).filter_by(
                league_id=league.id, season_year=upcoming
            )
        }
        if not kept_all:
            kept = {k: v for k, v in kept.items() if k[0] in kept_for}

    # Off-roster discovery keepers: submit_keepers deliberately allows the bonus 6th
    # keeper to be ANY player, not one on the roster or covered by IL — that IS the
    # whole point of the discovery draft. Without this union such a selection was
    # invisible everywhere that reads this function's OUTPUT (the keepers page,
    # keeper_candidates) even though it already correctly blocks everyone else from
    # drafting him (search_players) and correctly counts toward the manager's
    # draft-slot math (effective_keeper_selections, e503afd) — the same gap, in the
    # read path instead of the slot-count path. `p is not None` excludes a goalie-team
    # selection (player_id NULL); `- final_candidates` keeps this additive only for
    # pairs with no roster/IL story to tell. Scoped by `kept`, itself already
    # privacy-filtered above, so this can't leak a hidden pick to an unentitled viewer.
    discovery_only = {
        (m, p) for (m, p), is_disc in kept.items() if is_disc and p is not None
    } - final_candidates
    final_candidates |= discovery_only

    def _dropped(mid, pid, upto=None) -> bool:
        # A candidate reached purely through IL coverage (see final_candidates
        # above) may have no recorded roster presence at all — .get, not [], and
        # `first` must come from whichever of the two actually has data, or an
        # IL-only candidate with empty `gws` would crash min() below.
        gws = presence.get((mid, pid), set())
        il_gws = il.get((mid, pid), set())
        if not gws and not il_gws:
            return False
        first = min(gws | il_gws)
        # a gap between first appearance and the final GW, not covered by the IL,
        # means the player was dropped (to FA) and later re-acquired.
        # `upto` is the last GW this manager could have held him: for a manager who
        # TRADED HIM AWAY that's the gameweek before the trade, or the empty tail
        # after it reads as a drop and their whole tenure derives as 'waiver'.
        end = last_n if upto is None else upto
        return any(g not in gws and g not in il_gws for g in range(first, end + 1))

    # Who handed us each player, and when they stopped holding him. Ordered so the
    # most recent acquisition wins if a player arrives at the same manager twice.
    trade_from: dict = {}
    for t in (
        db.query(Trade)
        .filter(Trade.league_id == league.id, Trade.player_id.isnot(None),
                Trade.pick_round.is_(None))
        .order_by(Trade.created_at, Trade.id)
    ):
        trade_from[(t.to_manager, t.player_id)] = (
            t.from_manager, (t.event_gw - 1) if t.event_gw else None
        )

    memo: dict = {}

    def _status_for(mid, pid, upto=None, seen=()):
        """(acquisition, years_remaining) for a manager's tenure of a player.

        A trade transfers the player and changes nothing else, so when he arrived by
        trade both the clock and the label come from the SENDER — evaluated as of the
        moment he left them. Recursive so a chain carries the whole way, with `seen`
        guarding a trade-and-trade-back from looping.
        """
        key = (mid, pid, upto)
        if key in memo:
            return memo[key]
        carried = seed_remaining.get((mid, pid))
        inherited = None
        src = trade_from.get((mid, pid))
        if src and (src[0], pid) in presence and (mid, pid) not in seen:
            s_acq, s_years = _status_for(
                src[0], pid, src[1], seen + ((mid, pid),)
            )
            inherited = s_acq
            if carried is None:
                carried = s_years
        memo[key] = keeper_status(
            1 in presence.get((mid, pid), set()),   # started_with_manager (on GW1 roster)
            (mid, pid) in traded_in,
            _dropped(mid, pid, upto),
            carried,
            # A pure discovery candidate (off-roster, off-IL) has none of the signals
            # above to go on — the discovery draft IS the acquisition, worth a full
            # draft-length clock, exactly what submit_keepers already synthesizes at
            # submission time. A commissioner seed still wins over it, same as always.
            acquisition=seed_acq.get((mid, pid)) or (
                "discovery" if (mid, pid) in discovery_only else None
            ),
            traded_from=inherited,
        )
        return memo[key]

    # Ownership follows commissioner-entered trades (see player_ownership); roster
    # HISTORY does not. `hist` is the manager whose Roster rows and IL entries
    # actually describe this player, `owner` is whoever may now keep him. They differ
    # only for an overlaid player — and re-keying the history to the new owner would
    # be a lie, as well as a crash: presence[(new owner, pid)] is empty, so _dropped
    # would read False and quietly un-cap the clock.
    moved = player_ownership(db, league)

    status: dict = {}
    for hist, pid in final_candidates:
        owner = moved.get(pid, hist)
        acq, remaining = _status_for(hist, pid)
        # An overlaid player carries the sender's status wholesale, exactly as a
        # synced trade does above — the commissioner's own override for the new owner
        # still wins, since advance_season rewrites the seed owner-keyed at rollover.
        if owner != hist:
            if seed_remaining.get((owner, pid)) is not None:
                remaining = seed_remaining[(owner, pid)]
            if seed_acq.get((owner, pid)):
                acq = seed_acq[(owner, pid)]
        # Season identity first; fall back to the live pool rather than rendering a
        # raw UUID at someone, which is what a player with no snapshot row used to do.
        p = players.get(pid) or db.get(Player, pid)
        # Under the goalie-team rule a goalkeeper is not a keepable asset at all — his
        # CLUB is. Enforced here rather than only in the UI: this dict is what
        # submit_keepers validates against, so without it a manager could keep Raya
        # and then draft Arsenal and own him twice on a 14-slot board.
        # Position comes from the live pool, not the season snapshot, which is last
        # season's and would misclassify anyone FPL has reclassified.
        gk_off_board = clubs_on and (live_positions.get(pid) or "").upper() == "GKP"
        status.setdefault(owner, {})[pid] = {
            "player": p.name if p else str(pid),
            "position": p.position if p else None,
            "acquisition": acq,
            "years_remaining": remaining,
            "eligible": keeper_eligible(remaining) and not gk_off_board,
            "reason": "goalkeepers are kept as a club" if gk_off_board else None,
            # keyed on the OWNER: a KeeperSelection belongs to whoever submitted it
            "kept": (owner, pid) in kept,  # submitted keeper for next season
            "kept_discovery": kept.get((owner, pid), False),
        }
    return status


def _goalie_team_history(db: Session) -> dict:
    """{(season_year, team_id): (fpl_manager_id, 'draft'|'keeper')} across every season.

    Keyed on the FPL entry id, not managers.id: `managers` has one row per manager PER
    SEASON, so a club held for three years belongs to three different manager rows and
    a UUID-keyed history would read as three different owners.
    """
    out: dict = {}
    for sy, tid, fpl in (
        db.query(DraftPick.season_year, DraftPick.team_id, Manager.fpl_manager_id)
        .join(Manager, Manager.id == DraftPick.manager_id)
        .filter(DraftPick.team_id.isnot(None), DraftPick.draft_type == "main")
    ):
        out[(sy, tid)] = (fpl, "draft")
    # A keeper selection for season S beats a draft pick for S: you can't do both, and
    # if somehow both exist the retention is the later fact.
    for sy, tid, fpl in (
        db.query(KeeperSelection.season_year, KeeperSelection.team_id,
                 Manager.fpl_manager_id)
        .join(Manager, Manager.id == KeeperSelection.manager_id)
        .filter(KeeperSelection.team_id.isnot(None))
    ):
        out[(sy, tid)] = (fpl, "keeper")
    return out


def set_goalie_team_mode(db: Session, league: League, mode: str) -> dict:
    """Switch the goalie-team rule for this league row.

    Audited, because it is the single flag that changes what a draft IS — 14 picks
    instead of 15, goalkeepers off the board, clubs on it — and a season that started
    under one value and finished under the other would be unexplainable afterwards.

    Refuses an unknown value rather than storing it: `goalie_teams_on` treats anything
    it doesn't recognise as 'off', so a typo would silently hand out 15-pick boards
    with no error anywhere.
    """
    mode = (mode or "off").strip().lower()
    if mode not in GOALIE_TEAM_MODES:
        raise RuleViolation(
            f"unknown goalie-team mode '{mode}' — one of {', '.join(GOALIE_TEAM_MODES)}"
        )
    was = league.goalie_team_mode
    if was == mode:
        return {"mode": mode, "changed": False}
    league.goalie_team_mode = mode
    record_audit(db, league, action="league.goalie_team_mode",
                 summary=f"Goalie-team rule: {was} → {mode}",
                 details={"from": was, "to": mode,
                          "picks_per_manager": draft_picks_per_manager(mode)})
    db.commit()
    return {"mode": mode, "changed": True}


def goalie_team_owner(db: Session, league: League) -> dict:
    """{team_id: fpl_manager_id} — who holds each goalie team right now.

    Base is the season's draft pick or keeper selection; commissioner-entered club
    trades are then applied in `created_at` order, the same overlay-on-read shape
    `player_ownership` uses and for the same reason — the draft pick is a record of
    what happened and must not be rewritten by a later trade.

    Applied only when `from_manager` currently holds the club, so a typo'd direction
    moves nobody instead of teleporting a club (`/admin/health` surfaces the ones that
    didn't apply). `created_at` is the only reliable ordering: `date` is NULL on
    commissioner rows and the PK is a random uuid4.
    """
    cur = league.season_year or 0
    owner = {
        tid: fpl for (sy, tid), (fpl, _how) in _goalie_team_history(db).items()
        if sy == cur
    }
    fpl_by_mid = {
        m.id: m.fpl_manager_id for m in db.query(Manager).filter_by(league_id=league.id)
    }
    for t in (
        db.query(Trade)
        .filter(Trade.league_id == league.id, Trade.team_id.isnot(None))
        .order_by(Trade.created_at, Trade.id)
    ):
        if owner.get(t.team_id) == fpl_by_mid.get(t.from_manager):
            owner[t.team_id] = fpl_by_mid.get(t.to_manager)
    return owner


def _add_club_trade(db: Session, league: League, frm, to, team, owner: dict) -> None:
    """One direction of a goalie-team move. Mutates `owner` so a caller doing a swap
    validates both legs against a single, consistent picture. Adds the row; does NOT
    commit — the caller owns the transaction."""
    if owner.get(team.id) != frm.fpl_manager_id:
        raise RuleViolation(f"{frm.display} doesn't hold {team.name}")
    db.add(Trade(league_id=league.id, from_manager=frm.id, to_manager=to.id,
                 team_id=team.id))
    owner[team.id] = to.fpl_manager_id


def trade_goalie_team(
    db: Session, league: League, *, from_fpl: str, to_fpl: str, team_code: int
) -> dict:
    """Record a ONE-WAY goalie-team trade (commissioner-entered).

    The receiver must not already have a club. Two managers swapping clubs is a
    different shape — both legs have to be judged against the state BEFORE either
    happens, or the first leg leaves the receiver holding two and the second refuses.
    That path is `record_trade`.
    """
    frm = _resolve_manager(db, league, from_fpl)
    to = _resolve_manager(db, league, to_fpl)
    if frm.id == to.id:
        raise RuleViolation("pick two different managers")
    team = _resolve_team(db, team_code)
    owner = goalie_team_owner(db, league)
    # Ownership first. It is the more specific fact, and checking capacity first means
    # a trade entered backwards reports "the RECEIVER already has a club" — true, but
    # it sends you looking at the wrong manager.
    if owner.get(team.id) != frm.fpl_manager_id:
        raise RuleViolation(f"{frm.display} doesn't hold {team.name}")
    if any(fpl == to.fpl_manager_id for fpl in owner.values()):
        raise RuleViolation(
            f"{to.display} already has a goalie team — trade it away in the same deal"
        )
    _add_club_trade(db, league, frm, to, team, owner)
    record_audit(db, league, action="trade.goalie_team",
                 summary=f"Goalie team: {team.name} from {frm.display} → {to.display}",
                 manager_ids=[frm.id, to.id],
                 details={"team_code": team_code})
    db.commit()
    return {"from": frm.display, "to": to.display, "team": team.name}


def _derive_gk_team_keeper_status(
    db: Session,
    league: League,
    *,
    kept_for: set | None = None,
    kept_all: bool = False,
) -> dict:
    """{manager_id: {...}} — each manager's goalie team and its keeper clock.

    A club has no `rosters` rows, so none of `_derive_keeper_status`'s machinery
    applies: there is no GW-by-GW continuity to walk and no drop to detect. Ownership
    is a discrete per-season fact (drafted, or kept), so the clock is just how long
    ago it was acquired — `KEEPER_FRESH_DRAFT` minus the seasons since.

    Returns at most one entry per manager: the club they hold for the CURRENT season,
    which is the one they're deciding whether to keep for the next.

    A relegated club is void — the slot goes back into the draft — so eligibility is
    gated on `is_current_pl` as well as the clock.

    `kept` is redacted by default, exactly like the player path: `/v1` has no viewer,
    and a club keeper is as private as any other until keepers are revealed.
    """
    if not goalie_team_keepable(league.goalie_team_mode):
        return {}
    cur = league.season_year or 0
    history = _goalie_team_history(db)
    if not history:
        return {}

    teams = {t.id: t for t in db.query(PlTeam)}
    managers = db.query(Manager).filter_by(league_id=league.id).all()
    seeds = {
        s.team_id: s
        for s in db.query(KeeperSeed).filter(
            KeeperSeed.league_id == league.id, KeeperSeed.team_id.isnot(None)
        )
    }
    submitted = {
        s.team_id: s.manager_id
        for s in db.query(KeeperSelection).filter(
            KeeperSelection.league_id == league.id,
            KeeperSelection.season_year == cur + 1,
            KeeperSelection.team_id.isnot(None),
        )
    }

    owner = goalie_team_owner(db, league)
    out: dict = {}
    for m in managers:
        held = [tid for tid, fpl in owner.items() if fpl == m.fpl_manager_id]
        if not held:
            continue
        tid = held[0]
        # The clock is computed from whoever the RECORD says held the club — the
        # sender, if it was traded — and handed to the current owner unchanged. A
        # trade changes ownership and nothing else: the years don't reset and the
        # label doesn't become 'trade', exactly as rules.keeper_status(traded_from=)
        # does for a player. Otherwise trading a club out and back would launder a
        # spent clock clean.
        holder = history.get((cur, tid), (None, None))[0]
        # Walk back while the SAME holder still held the SAME club. A gap means they
        # lost it and got it again, which restarts the clock exactly as a drop does
        # for a player.
        acquired = cur
        while history.get((acquired - 1, tid), (None, None))[0] == holder:
            acquired -= 1
        acquisition = history.get((acquired, tid), (None, "draft"))[1]
        acquisition = "draft" if acquisition == "draft" else "waiver"
        seed = seeds.get(tid)
        remaining = (
            seed.years_remaining if seed is not None
            else (KEEPER_FRESH_DRAFT if acquisition == "draft" else KEEPER_FRESH_WAIVER)
            - (cur - acquired)
        )
        team = teams.get(tid)
        in_pl = bool(team and team.is_current_pl)
        disclose = kept_all or (kept_for and m.id in kept_for)
        out[m.id] = {
            "team_id": tid,
            "team_code": team.code if team else None,
            "player": team.name if team else str(tid),
            "short_name": team.short_name if team else None,
            "position": GOALIE_TEAM_POSITION,
            "acquisition": acquisition,
            "years_remaining": remaining,
            "eligible": keeper_eligible(remaining) and in_pl,
            "reason": None if in_pl else "relegated — no longer a Premier League club",
            "kept": bool(disclose and submitted.get(tid) == m.id),
        }
    return out


def get_keepers(
    db: Session,
    league: League,
    *,
    viewer_fpl: str | None = None,
    viewer_is_admin: bool = False,
) -> list[dict]:
    """Per-manager keeper eligibility for the upcoming selection, derived from
    roster continuity (drops reset the clock; IL and trades are explained moves),
    acquisition type, and Option-B seeds. Precomputed read; no FPL calls.

    Eligibility (acquisition, years, eligible) is public. Which players a manager has
    SUBMITTED is not, until keepers are revealed — pass the viewer so they see their
    own. No viewer means no `kept` flags at all, which is what the unauthenticated
    /v1 endpoint wants.
    """
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.name).all()
    )
    # str(): fpl_manager_id is a String column and the session value may not be. A
    # mismatch here fails closed (you'd see none of your own), which is safe but reads
    # like a bug, so coerce the same way auth.can_act_as does.
    mine = {
        m.id for m in managers
        if viewer_fpl is not None and m.fpl_manager_id == str(viewer_fpl)
    }
    status = _derive_keeper_status(
        db, league,
        kept_for=mine,
        kept_all=viewer_is_admin or keepers_revealed(league),
    )
    clubs = _derive_gk_team_keeper_status(
        db, league,
        kept_for=mine,
        kept_all=viewer_is_admin or keepers_revealed(league),
    )
    out = []
    for m in managers:
        items = list(status.get(m.id, {}).values())
        items.sort(key=lambda x: (not x["eligible"], -x["years_remaining"], x["player"]))
        out.append({"manager": m.display, "manager_fpl": m.fpl_manager_id,
                    "players": items, "goalie_team": clubs.get(m.id)})
    return out


def submit_keepers(
    db: Session,
    league: League,
    *,
    fpl_manager_id: str,
    keeper_fpl_ids: list[int],
    season_year: int,
    discovery_fpl_id: int | None = None,
    keeper_team_code: int | None = None,
) -> dict:
    """Validate and persist a manager's keeper selection for `season_year`.
    Enforces eligibility + caps (<=5, +1 discovery, <=2 waiver). Replaces any
    prior selection for that manager/season.

    `keeper_team_code` keeps the manager's goalie team, under `goalie_team_mode =
    'keeper'`. It costs one of the five, exactly like a player — the difference
    between the two modes is that cap, not the draft slot: a retained club consumes
    one of the fourteen picks either way.
    """
    manager = _resolve_manager(db, league, fpl_manager_id)
    status = _derive_keeper_status(db, league).get(manager.id, {})
    by_fpl = {p.fpl_id: p for p in db.query(Player)}

    # the discovery keeper can be any player (off-roster), so the roster
    # checkboxes won't include it — make sure it's part of the set to persist
    all_fids = list(keeper_fpl_ids)
    if discovery_fpl_id is not None and discovery_fpl_id not in all_fids:
        all_fids.append(discovery_fpl_id)

    selections = []
    for fid in all_fids:
        player = by_fpl.get(fid)
        if not player:
            raise RuleViolation(f"player {fid} not found")
        is_discovery = fid == discovery_fpl_id
        st = status.get(player.id)
        if not st:
            # The discovery (bonus 6th) keeper comes from the discovery draft and
            # may be ANY available player, not just the manager's final roster.
            if is_discovery:
                # A discovery pick is a special draft-day acquisition (the September
                # discovery draft), not a waiver pickup — the 4-year draft clock.
                st = {"player": player.name, "eligible": True,
                      "acquisition": "discovery",
                      "years_remaining": KEEPER_FRESH_DRAFT}
                if (goalie_teams_on(league.goalie_team_mode)
                        and (player.position or "").upper() == "GKP"):
                    # The one door left open into individual goalkeeper ownership: the
                    # discovery keeper may be ANY player, so it bypasses the roster
                    # candidate list where the rule is otherwise enforced.
                    st = {**st, "eligible": False,
                          "reason": "goalkeepers are kept as a club"}
            else:
                raise RuleViolation(
                    f"{player.name} is not one of {manager.display}'s keeper "
                    "candidates (traded away?)"
                )
        selections.append({**st, "fpl_id": fid, "player_id": player.id,
                           "team_id": None, "is_discovery": is_discovery})

    if keeper_team_code is not None:
        if not goalie_team_keepable(league.goalie_team_mode):
            raise RuleViolation("goalie teams aren't kept in this league")
        club = _derive_gk_team_keeper_status(db, league).get(manager.id)
        team = _resolve_team(db, keeper_team_code)
        if not club or club["team_id"] != team.id:
            raise RuleViolation(
                f"{team.name} is not {manager.display}'s goalie team"
            )
        selections.append({**club, "fpl_id": None, "player_id": None,
                           "team_id": team.id, "is_discovery": False})

    errors = validate_keeper_selection(
        selections, has_discovery_keeper=discovery_fpl_id is not None
    )
    if errors:
        raise RuleViolation("; ".join(errors))

    db.query(KeeperSelection).filter_by(
        manager_id=manager.id, season_year=season_year
    ).delete()
    for s in selections:
        db.add(
            KeeperSelection(
                league_id=league.id,
                manager_id=manager.id,
                player_id=s["player_id"],
                team_id=s["team_id"],
                season_year=season_year,
                is_discovery=s["is_discovery"],
            )
        )
    record_audit(db, league, action="keeper.submit",
                 summary=(f"{manager.display} submitted {len(selections)} keeper(s) for "
                          f"{season_year}: " + ", ".join(s["player"] for s in selections)),
                 manager_ids=[manager.id],
                 details={"season_year": season_year,
                          "keeper_fpl_ids": all_fids, "discovery_fpl_id": discovery_fpl_id,
                          "keeper_team_code": keeper_team_code})
    db.commit()
    return {
        "manager": manager.display,
        "season_year": season_year,
        "keepers": [
            {"player": s["player"], "acquisition": s["acquisition"],
             "years_remaining": s["years_remaining"], "is_discovery": s["is_discovery"]}
            for s in selections
        ],
    }


def get_keeper_selections(
    db: Session,
    league: League,
    season_year: int,
    *,
    viewer_fpl: str | None = None,
    viewer_is_admin: bool = False,
) -> list[dict]:
    """Submitted keeper selections for a season, grouped by manager.

    Identity resolves via the SUBMITTING league (`league`), not `season_year`:
    selections name the season being kept FOR, whose league row doesn't exist yet
    at submission time. So this shows the player as they were when picked, which
    is the only data available and the right context anyway. Reveal is judged on the
    same league row, for the same reason.

    Private until keepers are revealed: without a viewer this returns [], which is
    what the unauthenticated /v1 endpoint should say while selections are still open.
    """
    # Two queries, not one join. A goalie team has no PlayerSeason row, so the inner
    # join that resolves a player's season identity silently drops every club
    # selection — the keeper would simply vanish from the report.
    rows = (
        db.query(KeeperSelection, Manager, PlayerSeason)
        .join(Manager, Manager.id == KeeperSelection.manager_id)
        .join(PlayerSeason, PlayerSeason.player_id == KeeperSelection.player_id)
        .filter(KeeperSelection.league_id == league.id,
                KeeperSelection.season_year == season_year,
                PlayerSeason.league_id == league.id)
        .all()
    )
    club_rows = (
        db.query(KeeperSelection, Manager, PlTeam)
        .join(Manager, Manager.id == KeeperSelection.manager_id)
        .join(PlTeam, PlTeam.id == KeeperSelection.team_id)
        .filter(KeeperSelection.league_id == league.id,
                KeeperSelection.season_year == season_year)
        .all()
    )
    show_all = viewer_is_admin or keepers_revealed(league)
    # A selection whose player has since been traded away no longer counts, so it
    # must not be listed as a keeper either — the manager is simply one short.
    counts = {(s.manager_id, s.player_id, s.team_id)
              for s in effective_keeper_selections(db, league, season_year)}
    by_manager: dict = {}
    for sel, m, thing in rows + club_rows:
        if (sel.manager_id, sel.player_id, sel.team_id) not in counts:
            continue
        mine = viewer_fpl is not None and m.fpl_manager_id == str(viewer_fpl)
        if not (show_all or mine):
            continue
        by_manager.setdefault(m.display, []).append({
            "player": thing.name,
            "position": (GOALIE_TEAM_POSITION if sel.team_id
                         else getattr(thing, "position", None)),
            "is_discovery": sel.is_discovery,
        })
    return [{"manager": k, "keepers": v} for k, v in sorted(by_manager.items())]


# ---- drafts (board generation + commissioner-entered pick/player trades) ----
def _reverse_standings_managers(db: Session, league: League) -> list[Manager]:
    """Draft order for rounds 2+: worst-placed first.

    Reads the ADJUSTED standings, not the raw `Standing.rank` column. A commissioner
    deduction changes where a team finished, and the draft order is a consequence of
    where they finished — sorting on the synced rank meant the standings page and the
    draft board could disagree, which is exactly what a post-season deduction caused.
    Reuses get_standings rather than re-merging the deltas here, so there is one
    definition of "the standings" and the tie-breaks can't drift apart.
    """
    by_fpl = {
        m.fpl_manager_id: m
        for m in db.query(Manager).filter_by(league_id=league.id)
    }
    ordered = [
        by_fpl[row["fpl"]]
        for row in get_standings(db, league)          # best first, adjusted
        if row.get("fpl") in by_fpl
    ]
    return list(reversed(ordered))                    # worst first


def _r1_order_managers(db: Session, league: League) -> list[Manager]:
    rows = (
        db.query(DraftLottery, Manager)
        .join(Manager, Manager.id == DraftLottery.manager_id)
        .filter(DraftLottery.league_id == league.id, DraftLottery.pick_result.isnot(None))
        .all()
    )
    rows.sort(key=lambda x: x[0].pick_result)
    return [m for _, m in rows]


def set_draft_order(db: Session, league: League, fpl_manager_ids: list[str]) -> list[dict]:
    """Commissioner sets the round-1 pick order (the externally-run lottery result)."""
    managers = [_resolve_manager(db, league, fid) for fid in fpl_manager_ids]
    db.query(DraftLottery).filter_by(league_id=league.id).delete()
    for i, m in enumerate(managers, start=1):
        db.add(DraftLottery(league_id=league.id, manager_id=m.id, pick_result=i))
    record_audit(db, league, action="draft.order",
                 summary="Set round-1 draft order: "
                         + ", ".join(f"{i}. {m.display}" for i, m in enumerate(managers, start=1)),
                 manager_ids=[m.id for m in managers])
    db.commit()
    return [{"pick": i, "manager": m.display} for i, m in enumerate(managers, start=1)]


def get_draft_order(db: Session, league: League) -> list[dict]:
    """The current commissioner-set round-1 order as [{name, fpl}] in pick order
    (empty if not set yet)."""
    return [
        {"name": m.display, "fpl": m.fpl_manager_id}
        for m in _r1_order_managers(db, league)
    ]


# ---- draft order overrides (rounds 2+) ----------------------------------------
def _order_overrides(
    db: Session, league: League, season_year: int, draft_type: str = "main"
) -> dict:
    """{round_or_None: [manager_id, ...]} for every stored override."""
    from models import DraftOrderOverride

    out: dict = {}
    for row in (
        db.query(DraftOrderOverride)
        .filter_by(league_id=league.id, season_year=season_year, draft_type=draft_type)
        .order_by(DraftOrderOverride.position)
    ):
        out.setdefault(row.round, []).append(row.manager_id)
    return out


def set_draft_order_override(
    db: Session, league: League, season_year: int, fpl_manager_ids: list[str],
    *, round: int | None = None, draft_type: str = "main",
) -> list[dict]:
    """Set the pick order for rounds 2+. `round=None` sets the base order used by
    every round from 2 on; a round number overrides that round only.

    Replaces the whole list rather than patching positions, so the stored order can
    never end up with a gap or a duplicate position.
    """
    from models import DraftOrderOverride

    if round is not None and round < 2:
        raise RuleViolation("round 1 has its own order — set it as the lottery result")
    managers = [_resolve_manager(db, league, fid) for fid in fpl_manager_ids]
    if not managers:
        raise RuleViolation("pick order cannot be empty")

    prev = [
        str(mid) for mid in _order_overrides(db, league, season_year, draft_type)
        .get(round, [])
    ]
    q = db.query(DraftOrderOverride).filter_by(
        league_id=league.id, season_year=season_year, draft_type=draft_type
    )
    q = q.filter(DraftOrderOverride.round.is_(None)) if round is None else \
        q.filter(DraftOrderOverride.round == round)
    q.delete(synchronize_session=False)

    for i, m in enumerate(managers, start=1):
        db.add(DraftOrderOverride(
            league_id=league.id, season_year=season_year, draft_type=draft_type,
            round=round, position=i, manager_id=m.id,
        ))
    where = "rounds 2+" if round is None else f"round {round}"
    record_audit(
        db, league, action="draft.order.override",
        summary=(f"Set {season_year} {draft_type} draft order for {where}: "
                 + ", ".join(f"{i}. {m.display}" for i, m in enumerate(managers, 1))),
        manager_ids=[m.id for m in managers],
        details={"round": round, "season_year": season_year,
                 "draft_type": draft_type, "previous": prev},
    )
    db.commit()
    return [{"pick": i, "manager": m.display} for i, m in enumerate(managers, start=1)]


def clear_draft_order_override(
    db: Session, league: League, season_year: int,
    *, round: int | None = None, draft_type: str = "main",
) -> None:
    """Drop an override so the round falls back to the derived (standings) order."""
    from models import DraftOrderOverride

    q = db.query(DraftOrderOverride).filter_by(
        league_id=league.id, season_year=season_year, draft_type=draft_type
    )
    q = q.filter(DraftOrderOverride.round.is_(None)) if round is None else \
        q.filter(DraftOrderOverride.round == round)
    rows = q.all()
    if not rows:
        raise RuleViolation("no override set for that round")
    prev = [str(r.manager_id) for r in sorted(rows, key=lambda r: r.position)]
    for r in rows:
        db.delete(r)
    where = "rounds 2+" if round is None else f"round {round}"
    record_audit(
        db, league, action="draft.order.revert",
        summary=f"Reverted {season_year} {draft_type} draft order for {where} to standings",
        details={"round": round, "season_year": season_year,
                 "draft_type": draft_type, "previous": prev},
    )
    db.commit()


def draft_order_context(
    db: Session, league: League, season_year: int, draft_type: str = "main"
) -> dict:
    """Everything the order editor needs: the effective order per round, which rounds
    are overridden, and a per-manager pick count so a slot reassignment that gives
    someone an extra pick is visible rather than silent."""
    overrides = _order_overrides(db, league, season_year, draft_type)
    by_id = {m.id: m for m in db.query(Manager).filter_by(league_id=league.id)}
    derived = _reverse_standings_managers(db, league)

    def as_opts(mids):
        return [
            {"name": by_id[mid].display, "fpl": by_id[mid].fpl_manager_id}
            for mid in mids if mid in by_id
        ]

    base = as_opts(overrides[None]) if None in overrides else [
        {"name": m.display, "fpl": m.fpl_manager_id} for m in derived
    ]
    board = get_draft_board(db, league, season_year, draft_type)
    rounds = sorted({b["round"] for b in board if b["round"] > 1})
    counts: dict = {}
    for b in board:
        counts[b["original_owner"]] = counts.get(b["original_owner"], 0) + 1
    return {
        "base": base,
        "base_overridden": None in overrides,
        "rounds": [
            {"round": r, "overridden": r in overrides,
             "order": as_opts(overrides[r]) if r in overrides else base}
            for r in rounds
        ],
        "counts": sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])),
    }


def list_players(db: Session, league: League) -> list[dict]:
    """All players as [{fpl_id, label}] for name-based pickers (label disambiguates
    duplicate names by team)."""
    rows = db.query(Player).order_by(Player.name).all()
    return [
        {"fpl_id": p.fpl_id,
         "label": f"{p.name} · {p.current_team}" if p.current_team else p.name}
        for p in rows
    ]


def trade_pick(
    db: Session, league: League, *, from_fpl: str, to_fpl: str, original_fpl: str,
    round: int, season_year: int, draft_type: str = "main",
) -> dict:
    """Record a draft-pick trade (commissioner-entered, live). Reassigns ownership
    of the (season, draft_type, round) slot originally belonging to original_fpl.

    Refuses to leave the seller with no way to get a goalie team. Trading away your
    last slot after thirteen outfielders is the same dead end `record_pick` guards
    against, reached by a different door."""
    frm = _resolve_manager(db, league, from_fpl)
    to = _resolve_manager(db, league, to_fpl)
    orig = _resolve_manager(db, league, original_fpl)
    stranded = _goalie_team_required_reason(
        db, league, frm, season_year=season_year, draft_type=draft_type,
        pick_number=None,
    )
    if stranded:
        raise RuleViolation(f"{stranded} — trading it away would strand them")
    label = f"{season_year} {draft_type} R{round} (orig {orig.name})"
    db.add(
        Trade(
            league_id=league.id, from_manager=frm.id, to_manager=to.id,
            pick_original_manager=orig.id, pick_round=round,
            pick_season_year=season_year, pick_draft_type=draft_type, draft_pick=label,
        )
    )
    record_audit(db, league, action="trade.pick",
                 summary=f"Pick trade: {frm.display} → {to.display} ({label})",
                 manager_ids=[frm.id, to.id, orig.id],
                 details={"round": round, "season_year": season_year, "draft_type": draft_type})
    db.commit()
    return {"from": frm.display, "to": to.display, "pick": label}


def trade_player(
    db: Session, league: League, *, from_fpl: str, to_fpl: str, player_fpl_id: int
) -> dict:
    """Record a commissioner-entered player trade (e.g. mid-draft, outside the
    FPL feed)."""
    frm = _resolve_manager(db, league, from_fpl)
    to = _resolve_manager(db, league, to_fpl)
    player = _resolve_player(db, player_fpl_id)
    db.add(Trade(league_id=league.id, from_manager=frm.id, to_manager=to.id, player_id=player.id))
    record_audit(db, league, action="trade.player",
                 summary=f"Player trade: {player.name} from {frm.display} → {to.display}",
                 manager_ids=[frm.id, to.id],
                 details={"player_fpl_id": player_fpl_id})
    db.commit()
    return {"from": frm.display, "to": to.display, "player": player.name}


def _unavailable_reason(
    db: Session, league: League, player: Player, *,
    season_year: int, draft_type: str, pick_number: int,
) -> str | None:
    """Why `player` can't be drafted into this slot — or None if they're free.

    Mirrors search_players' `taken` logic so the board never offers someone that
    record_pick would then refuse: keeper selections count for either draft, while
    draft picks are scoped to their own. The current slot is excluded, so an admin
    re-recording the same player into the same slot isn't blocked by themselves.
    """
    sel = (
        db.query(KeeperSelection, Manager)
        .join(Manager, Manager.id == KeeperSelection.manager_id)
        .filter(KeeperSelection.league_id == league.id,
                KeeperSelection.season_year == season_year,
                KeeperSelection.player_id == player.id)
        .first()
    )
    if sel:
        return f"being kept by {sel[1].display}"
    taken = (
        db.query(DraftPick, Manager)
        .outerjoin(Manager, Manager.id == DraftPick.manager_id)
        .filter(DraftPick.league_id == league.id,
                DraftPick.season_year == season_year,
                DraftPick.draft_type == draft_type,
                DraftPick.player_id == player.id,
                DraftPick.pick_number != pick_number)
        .first()
    )
    if taken:
        who = taken[1].display if taken[1] else "another manager"
        return f"already drafted by {who} at #{taken[0].pick_number}"
    return None


def _team_unavailable_reason(
    db: Session, league: League, team: PlTeam, *,
    season_year: int, draft_type: str, pick_number: int, manager: Manager,
) -> str | None:
    """Why `team` can't be drafted into this slot — or None if it's free.

    The sibling of `_unavailable_reason`, and it lives beside it for the same reason:
    search mirrors this, so the board never offers a club that record_pick would then
    refuse. Two rules — a club goes once, and a manager has one goalie team. Both are
    also partial unique indexes, but a constraint violation is a 500; this is the
    sentence a human reads.
    """
    taken = (
        db.query(DraftPick, Manager)
        .outerjoin(Manager, Manager.id == DraftPick.manager_id)
        .filter(DraftPick.league_id == league.id,
                DraftPick.season_year == season_year,
                DraftPick.draft_type == draft_type,
                DraftPick.team_id == team.id,
                DraftPick.pick_number != pick_number)
        .first()
    )
    if taken:
        who = taken[1].display if taken[1] else "another manager"
        return f"already drafted by {who} at #{taken[0].pick_number}"
    mine = (
        db.query(DraftPick)
        .filter(DraftPick.league_id == league.id,
                DraftPick.season_year == season_year,
                DraftPick.draft_type == draft_type,
                DraftPick.manager_id == manager.id,
                DraftPick.team_id.isnot(None),
                DraftPick.pick_number != pick_number)
        .first()
    )
    if mine:
        return (f"a second goalie team — {manager.display} already has one "
                f"(#{mine.pick_number})")
    return None


def _goalie_team_required_reason(
    db: Session, league: League, owner: Manager, *,
    season_year: int, draft_type: str, pick_number: int, board: list[dict] | None = None,
) -> str | None:
    """Why this manager must spend THIS slot on a goalie team — or None.

    Deliberately NOT part of `_unavailable_reason`. That function doubles as search's
    taken-oracle, and a striker is not "taken" because you are out of slots; folding
    this in would grey out the entire board for everyone at once.

    The rule: if a manager has no goalie team and this is their last remaining slot,
    they may only take a club. Without it a manager finishes the draft with thirteen
    outfielders, no keepers at all, and no way to fix it that doesn't involve deleting
    somebody else's picks.
    """
    if not goalie_teams_on(league.goalie_team_mode) or draft_type != "main":
        return None
    has_team = (
        db.query(DraftPick)
        .filter(DraftPick.league_id == league.id,
                DraftPick.season_year == season_year,
                DraftPick.draft_type == draft_type,
                DraftPick.manager_id == owner.id,
                DraftPick.team_id.isnot(None))
        .first()
    )
    if has_team:
        return None
    board = board if board is not None else get_draft_board(db, league, season_year, draft_type)
    # Slots this manager still has, counting the one being used right now.
    remaining = sum(
        1 for b in board
        if b.get("owner_fpl") == owner.fpl_manager_id
        and (not b.get("player") or b["pick"] == pick_number)
    )
    # Exactly one, not "at most one". Zero means either the draft hasn't been set up
    # yet (no order, so no board) or this manager has no slots at all — in neither
    # case does refusing a pick or a trade help anyone.
    if remaining != 1:
        return None
    return (f"{owner.display} has one pick left and no goalie team — "
            "it has to be a club")


def record_pick(
    db: Session, league: League, *, season_year: int, pick_number: int,
    owner_fpl: str, player_fpl_id: int | None = None, draft_type: str = "main",
    round: int = 0, overwrite: bool = False, team_code: int | None = None,
) -> dict:
    """Record a selection at a board slot (live). Upsert by slot. With concurrent
    devices, a slot that already has a player is NOT silently overwritten — raises
    RuleViolation (a clean "pick already made") unless `overwrite` (admin correction).

    Exactly one of `player_fpl_id` / `team_code` — a slot holds a player or a goalie
    team, never both.

    A player who is already kept or already drafted is refused outright. The board
    hides them, so this is a backstop — but a stale board, a double-click race, the
    /admin API and the autodraft queue can all still get here, and handing out a
    player two managers now believe they own is not fixable by re-picking. Note this
    is NOT waived by `overwrite`: that grants permission to replace a SLOT, which is
    a different thing from permission to take an unavailable player. Correct the
    keeper selection or delete the conflicting pick first.

    Two goalie-team rules join it, and for the same reason — neither is recoverable
    after the fact: a manager gets one club, and a manager down to their last slot
    with no club must spend it on one. Note what is still NOT enforced here: squad
    quotas. A sixth defender is legal and always was.
    """
    if (player_fpl_id is None) == (team_code is None):
        raise RuleViolation("pick exactly one of a player or a goalie team")
    owner = _resolve_manager(db, league, owner_fpl)

    if team_code is not None:
        if not goalie_teams_on(league.goalie_team_mode):
            raise RuleViolation("goalie teams aren't drafted in this league")
        team = _resolve_team(db, team_code)
        reason = _team_unavailable_reason(
            db, league, team, season_year=season_year, draft_type=draft_type,
            pick_number=pick_number, manager=owner,
        )
        if reason:
            raise RuleViolation(f"{team.name} is {reason}")
        selection, label = team, team.name
    else:
        player = _resolve_player(db, player_fpl_id)
        reason = _unavailable_reason(
            db, league, player, season_year=season_year, draft_type=draft_type,
            pick_number=pick_number,
        )
        if reason:
            raise RuleViolation(f"{player.name} is {reason}")
        reserved = _goalie_team_required_reason(
            db, league, owner, season_year=season_year, draft_type=draft_type,
            pick_number=pick_number,
        )
        if reserved:
            raise RuleViolation(reserved)
        selection, label = player, player.name

    is_team = team_code is not None
    existing = (
        db.query(DraftPick)
        .filter_by(league_id=league.id, season_year=season_year, draft_type=draft_type, pick_number=pick_number)
        .one_or_none()
    )
    if existing:
        if (existing.player_id is not None or existing.team_id is not None) and not overwrite:
            raise RuleViolation(f"pick {pick_number} has already been made")
        existing.manager_id = owner.id
        existing.player_id = None if is_team else selection.id
        existing.team_id = selection.id if is_team else None
    else:
        db.add(DraftPick(
            league_id=league.id, season_year=season_year, draft_type=draft_type,
            pick_number=pick_number, round=round, manager_id=owner.id,
            player_id=None if is_team else selection.id,
            team_id=selection.id if is_team else None,
            source="draft",
        ))
    record_audit(db, league, action="draft.pick",
                 summary=(f"{owner.display} drafted {label} "
                          f"({draft_type} #{pick_number})"
                          + (" [overwrite]" if overwrite else "")),
                 manager_ids=[owner.id],
                 details={"season_year": season_year, "pick_number": pick_number,
                          "draft_type": draft_type, "player_fpl_id": player_fpl_id,
                          "team_code": team_code, "overwrite": overwrite})
    db.commit()
    return {"pick": pick_number, "owner": owner.name, "player": label}


def get_draft_queue(
    db: Session, league: League, fpl_manager_id: str, season_year: int, draft_type: str = "main"
) -> list[dict]:
    """A manager's ranked autodraft queue, players and goalie teams in one order.

    Each row is `{kind, fpl_id, team_code, name, position}` — `kind` says which of
    the two ids is set, and a club reports the TEAM pseudo-position.
    """
    from models import DraftQueue

    manager = _resolve_manager(db, league, fpl_manager_id)
    rows = (
        db.query(DraftQueue)
        .filter(
            DraftQueue.league_id == league.id, DraftQueue.season_year == season_year,
            DraftQueue.draft_type == draft_type, DraftQueue.manager_id == manager.id,
        )
        .order_by(DraftQueue.rank)
        .all()
    )
    if not rows:
        return []
    players = {
        p.id: p for p in db.query(Player).filter(
            Player.id.in_([r.player_id for r in rows if r.player_id])
        )
    }
    teams = {
        t.id: t for t in db.query(PlTeam).filter(
            PlTeam.id.in_([r.team_id for r in rows if r.team_id])
        )
    }
    out = []
    for r in rows:
        if r.team_id:
            t = teams.get(r.team_id)
            if t:
                out.append({"kind": "team", "fpl_id": None, "team_code": t.code,
                            "name": t.name, "position": GOALIE_TEAM_POSITION})
            continue
        p = players.get(r.player_id)
        if p:
            out.append({"kind": "player", "fpl_id": p.fpl_id, "team_code": None,
                        "name": p.name, "position": p.position})
    return out


def _queue_match(manager, *, league, season_year, draft_type, player=None, team=None) -> dict:
    return {
        "league_id": league.id, "season_year": season_year, "draft_type": draft_type,
        "manager_id": manager.id,
        "player_id": player.id if player else None,
        "team_id": team.id if team else None,
    }


def add_to_queue(
    db: Session, league: League, *, fpl_manager_id: str, player_fpl_id: int | None = None,
    season_year: int, draft_type: str = "main", team_code: int | None = None,
) -> None:
    """Append a player or a goalie team to the manager's queue (idempotent).

    One ranked list for both: `rank` is a single ordering over everything the manager
    wants, and a manager whose last slot must be a club needs that club rankable
    among the players.
    """
    from models import DraftQueue

    if (player_fpl_id is None) == (team_code is None):
        raise RuleViolation("queue exactly one of a player or a goalie team")
    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id) if player_fpl_id is not None else None
    team = _resolve_team(db, team_code) if team_code is not None else None
    if team is not None and not goalie_teams_on(league.goalie_team_mode):
        raise RuleViolation("goalie teams aren't drafted in this league")
    match = _queue_match(manager, league=league, season_year=season_year,
                         draft_type=draft_type, player=player, team=team)
    if db.query(DraftQueue).filter_by(**match).one_or_none():
        return
    next_rank = (
        db.query(func.coalesce(func.max(DraftQueue.rank), -1)).filter_by(
            league_id=league.id, season_year=season_year, draft_type=draft_type,
            manager_id=manager.id,
        ).scalar()
    ) + 1
    db.add(DraftQueue(**match, rank=next_rank))
    db.commit()


def remove_from_queue(
    db: Session, league: League, *, fpl_manager_id: str, player_fpl_id: int | None = None,
    season_year: int, draft_type: str = "main", team_code: int | None = None,
) -> None:
    from models import DraftQueue

    if (player_fpl_id is None) == (team_code is None):
        raise RuleViolation("remove exactly one of a player or a goalie team")
    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id) if player_fpl_id is not None else None
    team = _resolve_team(db, team_code) if team_code is not None else None
    db.query(DraftQueue).filter_by(
        **_queue_match(manager, league=league, season_year=season_year,
                       draft_type=draft_type, player=player, team=team)
    ).delete(synchronize_session=False)
    db.commit()


def reorder_queue(
    db: Session, league: League, *, fpl_manager_id: str, season_year: int,
    draft_type: str = "main", ordered_keys: list[str],
) -> list[dict]:
    """Replace a manager's queue order wholesale — same shape as
    set_draft_order_override: resolve every key up front, then rewrite rank 0..N-1 in
    one commit, so the stored order can never end up with a gap or a duplicate.

    `ordered_keys` are "player:<fpl_id>" / "team:<team_code>" strings, matching the
    `kind` values get_draft_queue already returns. The submitted set must exactly
    match the manager's CURRENT queue — this is the only guard needed against two
    tabs (or a queue mutation elsewhere) racing an in-progress reorder: a stale submit
    is refused outright rather than silently dropping or duplicating an entry.
    """
    from models import DraftQueue, PlTeam

    manager = _resolve_manager(db, league, fpl_manager_id)
    existing = db.query(DraftQueue).filter_by(
        league_id=league.id, season_year=season_year,
        draft_type=draft_type, manager_id=manager.id,
    ).all()
    by_key = {
        (f"team:{db.get(PlTeam, r.team_id).code}" if r.team_id
         else f"player:{db.get(Player, r.player_id).fpl_id}"): r
        for r in existing
    }
    if set(ordered_keys) != set(by_key):
        raise RuleViolation("your queue changed — reload and try again")
    for i, key in enumerate(ordered_keys):
        by_key[key].rank = i
    db.commit()
    return get_draft_queue(db, league, fpl_manager_id, season_year, draft_type)


def approve_queued_pick(
    db: Session, league: League, *, season_year: int, draft_type: str = "main"
) -> dict:
    """Admin: fill the on-the-clock slot from its owner's queue — picks their top
    still-available, eligible queued player or goalie team. Raises RuleViolation if
    the draft is complete or the on-the-clock manager has no usable queued pick."""
    board = (
        get_discovery_board(db, league, season_year)
        if draft_type == "discovery"
        else get_draft_board(db, league, season_year)
    )
    slot = next_open_pick(board)
    if not slot or not slot.get("owner_fpl"):
        raise RuleViolation("the draft is complete")
    owner = _resolve_manager(db, league, slot["owner_fpl"])
    queued = get_draft_queue(db, league, owner.fpl_manager_id, season_year, draft_type)
    if not queued:
        raise RuleViolation(f"{owner.display} has no queued picks")

    # This manager is down to their last slot and has no club, so only a club will
    # do — skip straight past the outfielders they queued.
    reserved = _goalie_team_required_reason(
        db, league, owner, season_year=season_year, draft_type=draft_type,
        pick_number=slot["pick"], board=board,
    )

    # exclude already-taken (kept/drafted) + ineligible players.
    # kept_all: this is a CORRECTNESS filter, not a disclosure — with the redacted
    # default the queue would hand this manager another manager's keeper, and
    # record_pick has no availability guard to catch it.
    available = search_players(
        db, league, available_year=season_year, draft_type=draft_type,
        include_taken=False, kept_all=True, for_manager_id=owner.id,
        include_teams=True, limit=10_000,
    )
    # Keyed by (kind, id), never by fpl_id alone: every club row carries fpl_id None,
    # and a set containing None makes every DEPARTED player (also fpl_id None) look
    # available again.
    available_keys = {(r["kind"], r["fpl_id"] if r["kind"] == "player" else r["team_code"])
                      for r in available}
    for entry in queued:
        if reserved and entry["kind"] != "team":
            continue
        key = (entry["kind"], entry["fpl_id"] if entry["kind"] == "player" else entry["team_code"])
        if key not in available_keys:
            continue
        ids = ({"player_fpl_id": entry["fpl_id"]} if entry["kind"] == "player"
               else {"team_code": entry["team_code"]})
        record_pick(
            db, league, season_year=season_year, pick_number=slot["pick"],
            owner_fpl=owner.fpl_manager_id, draft_type=draft_type,
            round=slot["round"], **ids,
        )
        remove_from_queue(
            db, league, fpl_manager_id=owner.fpl_manager_id,
            season_year=season_year, draft_type=draft_type, **ids,
        )
        return {"pick": slot["pick"], "owner": owner.display, "player": entry["name"]}
    if reserved:
        raise RuleViolation(
            f"{reserved} — and no goalie team is queued"
        )
    raise RuleViolation(f"{owner.display}'s queued players are all unavailable")


def pick_ownership(
    db: Session, league: League, season_year: int, draft_type: str = "main"
) -> dict:
    """SINGLE SOURCE OF TRUTH for who owns each pick in a draft year. Returns
    {(round, original_owner_person): current_owner_person} for picks that have
    changed hands. Built from the imported baseline (future_picks) + recorded
    pick trades (trades table), applied in order (latest reassignment wins).
    Shared by the draft board and the future-picks grid so they never disagree.
    """
    from models import FuturePick

    person_by_id = {
        m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)
    }
    reassigned: dict = {}
    # baseline (imported net ownership from the sheet)
    for fp in db.query(FuturePick).filter_by(
        league_id=league.id, season_year=season_year, draft_type=draft_type
    ):
        reassigned[(fp.round, fp.original_owner)] = fp.owner
    # then live pick trades, in entry order (latest wins). Ordered on created_at:
    # this used to sort on Trade.id, which is a random uuid4 — so "latest wins" was
    # actually "whichever id sorted higher" the moment a pick changed hands twice.
    for t in (
        db.query(Trade)
        .filter(Trade.league_id == league.id, Trade.pick_round.isnot(None),
                Trade.pick_season_year == season_year, Trade.pick_draft_type == draft_type)
        .order_by(Trade.created_at, Trade.id)
        .all()
    ):
        orig, to = person_by_id.get(t.pick_original_manager), person_by_id.get(t.to_manager)
        if orig and to:
            reassigned[(t.pick_round, orig)] = to
    return reassigned


def player_ownership(db: Session, league: League) -> dict:
    """SINGLE SOURCE OF TRUTH for who holds a PLAYER when the roster snapshot is
    stale. Returns {player_id: current_owner_manager_id} for players whose owner
    differs from the latest synced roster — empty when nothing has moved. The
    sibling of pick_ownership, one table over.

    A commissioner-entered trade writes a Trade row and nothing else: it can't move a
    Roster row, because rosters come from FPL and FPL never saw this trade. Rosters
    are canonical truth, so the correction is an overlay on READ — never a write.
    (Writing a fabricated Roster row would be indistinguishable from a synced one,
    and get_transactions, anti-tanking and reconcile_absences would all ingest it as
    fact.)

    The discriminator is "no fpl_trade_id and no event_gw" — a trade FPL processed
    carries the event it processed in, and every snapshot from that gameweek on
    already shows the new owner, so overlaying it would move the player a SECOND
    time. It is self-retiring: sync_trades back-fills both fields onto a matching
    manual row exactly when the feed confirms the move, which is exactly when the
    snapshots take over. So there is no phase gate — needing the overlay is a
    property of the row, not of the calendar.

    To make a corrected FPL-sourced trade take effect here, delete it and re-enter it
    via trade_player; widening the filter to include `manually_edited` would let a
    conditions-only edit move a player by coincidence.
    """
    base, owner = _owner_maps(db, league)
    return {pid: mid for pid, mid in owner.items() if base.get(pid) != mid}


def effective_owner(db: Session, league: League) -> dict:
    """{player_id: manager_id} for EVERY rostered player, trades applied. Use this
    when you need to ask "does this manager still hold him?"; player_ownership
    returns only the players who moved."""
    return _owner_maps(db, league)[1]


def _owner_maps(db: Session, league: League) -> tuple[dict, dict]:
    """(snapshot owners, owners after site trades) — shared by the two readers above
    so the seeding and the fold can't drift apart."""
    gw = latest_gameweek(db, league)
    if gw is None:
        return {}, {}
    # Seeded from the SNAPSHOT, never from Trade: a trade naming a player nobody
    # rosters must move nobody rather than conjure a phantom squad member.
    owner = {
        pid: mid
        for mid, pid in db.query(Roster.manager_id, Roster.player_id).filter_by(
            gameweek_id=gw.id
        )
    }
    base = dict(owner)
    for t in (
        db.query(Trade)
        .filter(Trade.league_id == league.id,
                Trade.player_id.isnot(None),
                Trade.pick_round.is_(None),
                Trade.fpl_trade_id.is_(None),
                Trade.event_gw.is_(None))
        .order_by(Trade.created_at, Trade.id)
        .all()
    ):
        # Only from the CURRENT owner, so a mis-entered direction fails closed —
        # the player stays where the snapshot says instead of teleporting — and two
        # edges out of one manager apply once, not twice.
        if owner.get(t.player_id) == t.from_manager:
            owner[t.player_id] = t.to_manager
    return base, owner


def effective_keeper_selections(
    db: Session, league: League, season_year: int
) -> list[KeeperSelection]:
    """Submitted selections that still COUNT — i.e. the selecting manager still holds
    the player. A manager who trades a player away after submitting him doesn't have
    the trade blocked and doesn't have the row deleted: it simply stops counting, and
    they end up one keeper short (they can re-submit while keepers are unlocked)."""
    owner = effective_owner(db, league)
    return [
        s
        for s in db.query(KeeperSelection).filter_by(
            league_id=league.id, season_year=season_year
        )
        # Two kinds of selection the roster-ownership map has no opinion about, and
        # would therefore drop on the floor — costing the manager the draft slot the
        # keeper was supposed to save:
        #
        #   - a goalie team, which has no `rosters` row at all;
        #   - the discovery (bonus 6th) keeper, which submit_keepers deliberately
        #     allows to be ANY player rather than one off the final roster — that is
        #     the whole point of the discovery draft, so being off-roster is the
        #     normal case for it, not evidence the manager lost him.
        #
        # Without the second clause the manager keeps the player (availability is
        # derived separately and still reads "kept: X", so nobody else can draft him)
        # while not being CHARGED a slot for him — an extra pick, and every later
        # pick number shifts with it.
        if s.team_id is not None
        or s.is_discovery
        or owner.get(s.player_id) == s.manager_id
    ]


def _effective_roster_pids(
    db: Session, league: League, manager_id, gw_id, moved: dict | None = None
) -> set:
    """The player ids a manager effectively holds at `gw_id`, after site trades.

    Subtracts EVERY moved player and re-adds only this manager's: any player in
    `moved` has a definitive owner, so the snapshot's opinion about them is
    irrelevant. Pass a precomputed `moved` when looping over managers.
    """
    moved = player_ownership(db, league) if moved is None else moved
    base = {
        pid for (pid,) in db.query(Roster.player_id).filter_by(
            manager_id=manager_id, gameweek_id=gw_id
        )
    }
    return (base - set(moved)) | {pid for pid, mid in moved.items() if mid == manager_id}


def _off_board_positions(shape) -> set:
    """Positions that are not draftable as individuals under this shape.

    Under the goalie-team rule that's GKP — owned through a club, so a keeper is not
    a player the prep model failed to price, and listing them as "excluded" would bury
    the handful of genuine gaps under eighty goalkeepers.
    """
    return {p for p in draftprep.FPL_SHAPE.positions if p not in shape.positions}


def _goalie_team_board(db: Session, league: League, proj: dict, shape, *, teams: int) -> dict | None:
    """The club big board: every current PL club, its keepers, and its value.

    A club's points are its keepers' AGGREGATE projection, because the whole keeper
    room is what a manager gets. Ranked by value over the best club still available
    once everyone has one — the same value-over-replacement idea as players, computed
    by `draftprep.goalie_team_values`.
    """
    if not shape.reserved_slots:
        return None
    clubs = db.query(PlTeam).filter_by(is_current_pl=True).order_by(PlTeam.name).all()
    keepers = goalie_team_keepers(db, clubs)
    recs, detail = [], {}
    for t in clubs:
        gks = keepers.get(t.id, [])
        pts = sum(proj[p.id].points for p in gks if p.id in proj)
        recs.append(draftprep.Rec(t.id, t.name, GOALIE_TEAM_POSITION, pts))
        detail[t.id] = {
            "id": t.id, "team_code": t.code, "name": t.name, "short_name": t.short_name,
            "points": round(pts, 1),
            "keepers": [
                {"name": p.name,
                 "points": round(proj[p.id].points, 1) if p.id in proj else None}
                for p in gks
            ],
        }
    values, rep = draftprep.goalie_team_values(recs, teams=teams)
    out = [{**detail[k], "value": round(v, 1)} for k, v in values.items()]
    out.sort(key=lambda r: (-r["value"], r["name"]))
    return {"clubs": out, "replacement": round(rep, 1)}


def draft_preparation(db: Session, league: League, season_year: int) -> dict:
    """Predict every manager's keepers, then who's left and roughly when they go.

    BLIND ON PURPOSE: this never reads `keeper_selections`, not even the owner's own
    and not even once keepers are revealed. Some managers have submitted and most
    haven't, so a model that mixed real and predicted sets would be trustworthy in
    patches and there'd be no way to tell which patch you were reading. It also keeps
    an owner-only tool from being an information advantage over the league — the edge
    is the projections and the model, not seeing other people's submissions.

    Deliberately does NOT consult `_ineligible_fpl_ids`: that means "added to FPL
    after the draft", a mid-season concept, and applying it to a pre-draft question
    would be wrong even when it isn't empty (which it is today).

    Pick order comes from the same helpers as get_draft_board, so the two can't
    disagree about who picks when.
    """
    proj_year = projection_season_year(db)
    if proj_year is None:
        return {"available": False, "reason": "no projections imported"}
    proj = projection_index(db, proj_year)

    # The pool, defined ONCE and used for both draftability and the replacement-level
    # denominator: still in the Premier League, and covered by the projections. Keyed
    # on having a projection rather than on Player.status — those two sets coincide
    # today by coincidence, and `status` is FPL-canonical and changes every sync.
    shape = draftprep.shape_for(league.goalie_team_mode)
    live = db.query(Player).filter(Player.fpl_id.isnot(None)).all()
    pool = [
        draftprep.Rec(p.id, p.name, p.position, proj[p.id].points)
        for p in live if p.id in proj and p.position in shape.positions
    ]
    # Goalkeepers are not "excluded players" under the goalie-team rule — they are a
    # different asset class, listed on their own board below. Filtered out BEFORE
    # `excluded` is computed, or the page reports ~80 keepers as missing projections.
    off_board = _off_board_positions(shape)
    excluded = sorted(
        p.name for p in live
        if p.position not in off_board
        and (p.id not in proj or p.position not in shape.positions)
    )
    pool_by_id = {r.player_id: r for r in pool}
    n_teams = db.query(Manager).filter_by(league_id=league.id).count() or 10
    replacement, rep_diag = draftprep.replacement_levels(
        pool, teams=n_teams, shape=shape
    )
    goalie_teams = _goalie_team_board(db, league, proj, shape, teams=n_teams)

    managers = db.query(Manager).filter_by(league_id=league.id).all()
    names = {m.id: m.display for m in managers}
    id_by_person = {m.display: m.id for m in managers}
    # No kept_for/kept_all: the defaults already redact the submitted flags, and this
    # tool must not consult them at all.
    status = _derive_keeper_status(db, league)

    # A goalkeeper on last season's roster is not "departed" — he is no longer a
    # keepable ASSET, which is a different thing and would read as a data error.
    # Taken from Player.position (this season's), like the pool itself, not from the
    # derived status, whose position is last season's.
    off_board_ids = (
        {p.id for p in live if p.position in off_board} if off_board else set()
    )

    predictions, keeper_counts, rosters = {}, {}, {}
    departed: dict = {}
    off_board_keepers: dict = {}
    for m in managers:
        rows = status.get(m.id, {})
        cands = []
        for pid, v in rows.items():
            rec = pool_by_id.get(pid)
            if rec is None:
                if pid in off_board_ids:
                    off_board_keepers.setdefault(names[m.id], []).append(v["player"])
                    continue
                # left the PL, or no projection — can't be kept and can't be drafted
                departed.setdefault(names[m.id], []).append(v["player"])
                continue
            cands.append(draftprep.Rec(
                pid, rec.name,
                # Player.position (26/27), NOT the derived one: _derive_keeper_status
                # resolves identity through season_identity, so its position is last
                # season's and would corrupt next season's quota accounting.
                rec.position, rec.points, v["acquisition"], v["eligible"],
            ))
        out = draftprep.predict_keepers(cands, replacement, shape=shape)
        predictions[m.id] = out
        keeper_counts[m.id] = len(out["keepers"])
        rosters[m.id] = out["keepers"]

    r1 = _r1_order_managers(db, league) or _reverse_standings_managers(db, league)
    rev = _reverse_standings_managers(db, league)
    slots = generate_draft_slots(
        [m.id for m in r1], [m.id for m in rev], keeper_counts,
        draft_picks_per_manager(league.goalie_team_mode),
        overrides=_order_overrides(db, league, season_year, "main"),
    )
    # Same pick-trade overlay as get_draft_board, keyed on (round, original owner).
    own = pick_ownership(db, league, season_year, "main")
    ordered = []
    for i, s in enumerate(slots, start=1):
        orig = names.get(s["manager"])
        cur = own.get((s["round"], orig), orig)
        ordered.append({"pick": i, "round": s["round"],
                        "manager": id_by_person.get(cur, s["manager"]),
                        "original": s["manager"]})

    kept_ids = {r.player_id for v in rosters.values() for r in v}
    available = [r for r in pool if r.player_id not in kept_ids]
    sim = draftprep.simulate_draft(ordered, available, rosters, replacement,
                                   shape=shape)

    gone_by = {}
    for row in sim["picks"]:
        if row["player"] is not None:
            gone_by[row["player"].player_id] = row["pick"]
    values = draftprep.player_values(pool, replacement)
    return {
        "available": True,
        "season_year": season_year,
        "projection_year": proj_year,
        "rounds": max((s["round"] for s in slots), default=0),
        "replacement": replacement,
        "replacement_diag": rep_diag,
        "names": names,
        "predictions": predictions,
        "slots": ordered,
        "sim": sim,
        "gone_by": gone_by,
        # NOT "values": Jinja resolves `.values` to the dict METHOD, not the key
        "vor": values,
        "pool": pool,
        "excluded": excluded,
        "departed": departed,
        "goalie_teams": goalie_teams,
        "off_board_keepers": off_board_keepers,
    }


def draft_preparation_live(db: Session, league: League, season_year: int) -> dict:
    """Live draft assistant: actual submitted keepers + recorded picks, simulation
    only for remaining slots.

    Called once keepers_revealed(league) is True (phase="draft" or keepers_locked).
    Returns the same outer shape as draft_preparation so the template and route
    assembly code work unchanged. Extra keys: "live": True, "picks_made": int.
    """
    proj_year = projection_season_year(db)
    if proj_year is None:
        return {"available": False, "reason": "no projections imported"}
    proj = projection_index(db, proj_year)

    shape = draftprep.shape_for(league.goalie_team_mode)
    live_players = db.query(Player).filter(Player.fpl_id.isnot(None)).all()
    pool = [
        draftprep.Rec(p.id, p.name, p.position, proj[p.id].points)
        for p in live_players if p.id in proj and p.position in shape.positions
    ]
    off_board = _off_board_positions(shape)
    excluded = sorted(
        p.name for p in live_players
        if p.position not in off_board
        and (p.id not in proj or p.position not in shape.positions)
    )
    pool_by_id = {r.player_id: r for r in pool}

    managers = db.query(Manager).filter_by(league_id=league.id).all()
    names = {m.id: m.display for m in managers}
    id_by_person = {m.display: m.id for m in managers}

    n_teams = len(managers) or 10
    replacement, rep_diag = draftprep.replacement_levels(
        pool, teams=n_teams, shape=shape
    )
    goalie_teams = _goalie_team_board(db, league, proj, shape, teams=n_teams)

    # Actual keeper selections — only ones the manager still holds
    ks_rows = effective_keeper_selections(db, league, season_year)
    actual_keepers: dict = {m.id: [] for m in managers}
    for ks in ks_rows:
        rec = pool_by_id.get(ks.player_id)
        if rec:
            actual_keepers[ks.manager_id].append(rec)
    keeper_counts = {mid: len(recs) for mid, recs in actual_keepers.items()}

    # Actual draft picks recorded so far
    draft_picks = (
        db.query(DraftPick)
        .filter_by(league_id=league.id, season_year=season_year, draft_type="main")
        .all()
    )
    picks_by_number = {dp.pick_number: dp for dp in draft_picks}
    taken_ids = {dp.player_id for dp in draft_picks if dp.player_id}
    # Clubs already off the board, and who no longer owes a reserved slot.
    club_names = {t.id: t.name for t in db.query(PlTeam)}
    club_owner = {dp.team_id: dp.manager_id for dp in draft_picks if dp.team_id}
    reserved_spent = set(club_owner.values())

    # Slots — same computation as draft_preparation
    r1 = _r1_order_managers(db, league) or _reverse_standings_managers(db, league)
    rev = _reverse_standings_managers(db, league)
    slots = generate_draft_slots(
        [m.id for m in r1], [m.id for m in rev], keeper_counts,
        draft_picks_per_manager(league.goalie_team_mode),
        overrides=_order_overrides(db, league, season_year, "main"),
    )
    own = pick_ownership(db, league, season_year, "main")
    ordered = []
    for i, s in enumerate(slots, start=1):
        orig = names.get(s["manager"])
        cur = own.get((s["round"], orig), orig)
        ordered.append({"pick": i, "round": s["round"],
                        "manager": id_by_person.get(cur, s["manager"]),
                        "original": s["manager"]})

    # Pool for simulation: minus kept AND minus already drafted
    kept_ids = {r.player_id for recs in actual_keepers.values() for r in recs}
    available = [r for r in pool
                 if r.player_id not in kept_ids and r.player_id not in taken_ids]

    # Seed each manager's roster with actual keepers + actual picks so far
    current_rosters: dict = {m.id: list(actual_keepers[m.id]) for m in managers}
    for dp in draft_picks:
        rec = pool_by_id.get(dp.player_id)
        if rec and dp.manager_id:
            current_rosters[dp.manager_id].append(rec)

    # Simulate only remaining (not yet recorded) slots
    remaining_slots = [s for s in ordered if s["pick"] not in picks_by_number]
    sim_remaining = draftprep.simulate_draft(
        remaining_slots, available, current_rosters, replacement, shape=shape,
        reserved_spent=reserved_spent,
    )
    sim_by_pick = {r["pick"]: r for r in sim_remaining["picks"]}

    # Combined picks list: actual first, then simulated future picks
    all_picks = []
    for s in ordered:
        dp = picks_by_number.get(s["pick"])
        if dp:
            all_picks.append({
                "pick": s["pick"], "round": s["round"], "manager": s["manager"],
                "player": pool_by_id.get(dp.player_id),
                # A club isn't in the player pool, so it needs its own label or the
                # row renders empty and reads as a slot nobody has used yet.
                "goalie_team": club_names.get(dp.team_id) if dp.team_id else None,
                "reason": None, "alternatives": [], "actual": True,
            })
        elif s["pick"] in sim_by_pick:
            all_picks.append({**sim_by_pick[s["pick"]], "actual": False})

    # gone_by: real pick number for taken players, sim pick for projected
    gone_by = {dp.player_id: dp.pick_number for dp in draft_picks if dp.player_id}
    for row in sim_remaining["picks"]:
        if row["player"] and row["player"].player_id not in gone_by:
            gone_by[row["player"].player_id] = row["pick"]

    values = draftprep.player_values(pool, replacement)

    # departed: rostered but no projection / not in PL — same logic as draft_preparation
    status = _derive_keeper_status(db, league)
    off_board_ids = (
        {p.id for p in live_players if p.position in off_board} if off_board else set()
    )
    departed: dict = {}
    off_board_keepers: dict = {}
    for m in managers:
        for pid, v in status.get(m.id, {}).items():
            if pool_by_id.get(pid) is not None:
                continue
            bucket = off_board_keepers if pid in off_board_ids else departed
            bucket.setdefault(names[m.id], []).append(v["player"])

    predictions = {
        m.id: {"keepers": actual_keepers[m.id], "margin": None, "binding": []}
        for m in managers
    }

    return {
        "available": True,
        "live": True,
        "picks_made": len(draft_picks),
        "season_year": season_year,
        "projection_year": proj_year,
        "rounds": max((s["round"] for s in slots), default=0),
        "replacement": replacement,
        "replacement_diag": rep_diag,
        "names": names,
        "predictions": predictions,
        "slots": ordered,
        "sim": {"picks": all_picks, "squads": sim_remaining["squads"],
                "undrafted": sim_remaining["undrafted"]},
        "gone_by": gone_by,
        "vor": values,
        "pool": pool,
        "excluded": excluded,
        "departed": departed,
        "goalie_teams": _mark_taken_clubs(goalie_teams, club_owner, names),
        "off_board_keepers": off_board_keepers,
    }


def _mark_taken_clubs(board, club_owner: dict, names: dict):
    """Annotate the club big board with who has already drafted each one (live mode)."""
    if not board:
        return board
    owner_by_id = {tid: names.get(mid) for tid, mid in club_owner.items()}
    for row in board["clubs"]:
        row["owner"] = owner_by_id.get(row.get("id"))
    return board


def get_draft_board(
    db: Session, league: League, season_year: int, draft_type: str = "main"
) -> list[dict]:
    """The draft board: slots in pick order with current owner (after pick trades)
    and any recorded selection. Computed from the R1 order + reverse standings +
    free-keeper counts, so it reflects trades the moment they're entered."""
    managers = db.query(Manager).filter_by(league_id=league.id).all()
    names = {m.id: m.display for m in managers}
    id_by_person = {m.display: m.id for m in managers}
    r1 = _r1_order_managers(db, league) or _reverse_standings_managers(db, league)
    rev = _reverse_standings_managers(db, league)

    # Only selections that still count: a manager who traded away a player he'd
    # already submitted gets that pick back rather than drafting one short.
    keeper_counts: dict = {}
    for sel in effective_keeper_selections(db, league, season_year):
        keeper_counts[sel.manager_id] = keeper_counts.get(sel.manager_id, 0) + 1

    slots = generate_draft_slots(
        [m.id for m in r1], [m.id for m in rev], keeper_counts,
        draft_picks_per_manager(league.goalie_team_mode),
        overrides=_order_overrides(db, league, season_year, draft_type),
    )
    board = [
        {"pick": i, "round": s["round"], "original_owner_id": s["manager"], "owner_id": s["manager"]}
        for i, s in enumerate(slots, start=1)
    ]

    # apply the unified pick ownership (baseline + trades)
    own = pick_ownership(db, league, season_year, draft_type)
    for b in board:
        orig_person = names.get(b["original_owner_id"])
        cur_person = own.get((b["round"], orig_person), orig_person)
        b["owner_id"] = id_by_person.get(cur_person, b["original_owner_id"])

    # overlay recorded picks by pick number
    picks = {
        dp.pick_number: dp
        for dp in db.query(DraftPick).filter_by(
            league_id=league.id, season_year=season_year, draft_type=draft_type
        )
    }
    fpl_by_id = {m.id: m.fpl_manager_id for m in db.query(Manager).filter_by(league_id=league.id)}
    pnames = player_names(db, league)
    tnames = {t.id: t.name for t in db.query(PlTeam)}
    out = []
    for b in board:
        dp = picks.get(b["pick"])
        # A slot that has already been picked keeps the manager who actually picked
        # it. `pick_number` is positional, so any change to the order — a lottery
        # edit, a standings adjustment, an override — shifts what a given number
        # means, and recomputing the owner here would silently re-attribute a
        # completed selection to whoever now occupies that position.
        owner_id = b["owner_id"]
        recorded_owner_id = dp.manager_id if dp and dp.manager_id else None
        if recorded_owner_id:
            owner_id = recorded_owner_id
        out.append({
            "pick": b["pick"],
            "round": b["round"],
            "owner": names.get(owner_id),
            "owner_fpl": fpl_by_id.get(owner_id),
            "original_owner": names.get(b["original_owner_id"]),
            "traded": owner_id != b["original_owner_id"],
            # the order moved under a pick that was already made — surface it rather
            # than paper over it
            "reassigned": bool(recorded_owner_id and recorded_owner_id != b["owner_id"]),
            # `next_open_pick` treats a falsy `player` as "still on the clock", so a
            # goalie-team pick MUST render a label here — otherwise the club is
            # recorded, the slot still looks empty, and the draft never completes.
            "player": (
                tnames.get(dp.team_id) if dp and dp.team_id
                else pnames.get(dp.player_id) if dp and dp.player_id
                else None
            ),
            "is_goalie_team": bool(dp and dp.team_id),
        })
    return out


def get_future_picks(db: Session, league: League) -> list[dict]:
    """Future pick ownership by year — only picks that have changed hands —
    computed from the same pick_ownership source as the draft board, so a newly
    entered pick trade shows up here automatically."""
    from models import FuturePick

    years = {y for (y,) in db.query(FuturePick.season_year).filter_by(league_id=league.id).distinct()}
    years |= {
        y for (y,) in db.query(Trade.pick_season_year)
        .filter(Trade.league_id == league.id, Trade.pick_season_year.isnot(None)).distinct()
    }
    out = []
    for y in sorted(years):
        entry = {"year": y}
        for dt in ("main", "discovery"):
            own = pick_ownership(db, league, y, dt)
            entry[dt] = [
                {"round": rnd, "original_owner": orig, "owner": owner}
                for (rnd, orig), owner in sorted(own.items(), key=lambda kv: (kv[0][0], kv[0][1]))
            ]
        if entry["main"] or entry["discovery"]:
            out.append(entry)
    return out


# ---- player search (for the draft board / pick + trade entry) ----
# The pseudo-position a goalie team occupies in the draft-search UI. Not a value
# `players.position` ever holds — clubs are not players — but the search, the filter
# dropdown and the result rows all key off one string, and this is it.
GOALIE_TEAM_POSITION = "TEAM"


def _goalie_team_rows(
    db: Session,
    league: League,
    *,
    q: str | None,
    available_year: int | None,
    draft_type: str,
    include_taken: bool,
    for_manager_id=None,
    stats: dict | None = None,
) -> list[dict]:
    """Draftable goalie teams, in the same row shape `search_players` returns.

    `points` is what the club's CURRENT keepers scored in the season stats_season()
    resolves to — the aggregate, because that is what the manager is buying. Clubs
    carry no price: you don't pay for a club.

    A club is unavailable if someone has drafted it, or if `for_manager_id` already
    holds one. That second case mirrors `_team_unavailable_reason` on purpose: the
    board must never offer a pick that record_pick would then refuse.
    """
    teams = db.query(PlTeam).filter_by(is_current_pl=True).order_by(PlTeam.name).all()
    if q:
        needle = q.lower()
        teams = [t for t in teams
                 if needle in t.name.lower() or needle in t.short_name.lower()]
    if not teams:
        return []

    keepers = goalie_team_keepers(db, teams)
    stats = stats if stats is not None else season_identity(db, stats_season(db, league))

    taken: dict = {}
    owner_has_team = False
    if available_year is not None:
        names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
        for tid, mid in (
            db.query(DraftPick.team_id, DraftPick.manager_id)
            .filter_by(league_id=league.id, season_year=available_year,
                       draft_type=draft_type)
            .filter(DraftPick.team_id.isnot(None))
        ):
            taken[tid] = f"drafted: {names.get(mid, '?')}"
            if for_manager_id is not None and mid == for_manager_id:
                owner_has_team = True

    out = []
    for t in teams:
        gks = keepers.get(t.id, [])
        pts = [stats[p.id].total_points for p in gks
               if p.id in stats and stats[p.id].total_points is not None]
        reason = taken.get(t.id)
        if reason is None and owner_has_team:
            reason = "you already have a goalie team"
        if reason and not include_taken:
            continue
        out.append({
            "fpl_id": None,
            "team_code": t.code,
            "kind": "team",
            "name": t.name,
            "position": GOALIE_TEAM_POSITION,
            "team": t.short_name,
            "price": None,
            "points": sum(pts) if pts else None,
            "keepers": [p.name for p in gks],
            "taken": bool(reason),
            "taken_by": reason,
            "ineligible": False,
        })
    return out


def search_players(
    db: Session,
    league: League,
    *,
    q: str | None = None,
    position: str | None = None,
    available_year: int | None = None,
    sort: str | None = None,
    include_taken: bool = False,
    draft_type: str = "main",
    kept_for: set | None = None,
    kept_all: bool = False,
    for_manager_id=None,
    include_teams: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search the player pool. A name query searches ALL players (position is
    ignored when `q` is set); `position` alone filters by position. `available_year`
    marks already-kept/drafted players: by default they're excluded, but with
    `include_taken` they're returned flagged (`taken` + `taken_by`) so a search can
    surface "already drafted" instead of empty results. `sort` = 'price', 'points',
    or 'team' (else by name).

    `kept_for` / `kept_all` control whose keeper selections count as taken, since those
    are private until revealed (see rules.keepers_revealed). Default: nobody's — so a
    caller that forgets discloses nothing. `drafted:` labels are unaffected; draft
    picks are public.

    Under the goalie-team rule the pool changes shape: goalkeepers stop being
    individually draftable and twenty clubs take their place. `position='TEAM'`
    returns those clubs, and a name query matches them too, so searching "Arsenal"
    finds the club rather than nothing. `for_manager_id` marks every club taken for a
    manager who already has one — the same rule record_pick enforces.

    `include_teams=True` forces clubs into the results whatever the query is. A caller
    using this as an availability ORACLE rather than as a search — approve_queued_pick
    — must set it, or a queued club reads as unavailable simply because nobody asked
    for clubs. Left unset it follows the query, so browsing 'All positions' stays a
    list of players."""
    # Points come from the season stats_season() resolves to — last completed season
    # while drafting, the live one once it starts. NOT from a join: an inner join
    # would drop every player with no snapshot row (new to the PL, or never matched
    # to a code), and those are draftable. `limit` is applied in Python below, so the
    # points sort can be too, without truncating first.
    stats = season_identity(db, stats_season(db, league))
    clubs_on = goalie_teams_on(league.goalie_team_mode)
    want_teams = clubs_on and (
        include_teams
        if include_teams is not None
        else (bool(q) or (position or "").upper() == GOALIE_TEAM_POSITION)
    )
    teams = (
        _goalie_team_rows(
            db, league, q=q, available_year=available_year, draft_type=draft_type,
            include_taken=include_taken, for_manager_id=for_manager_id, stats=stats,
        )
        if want_teams else []
    )
    if clubs_on and (position or "").upper() == GOALIE_TEAM_POSITION:
        return teams[:limit]

    query = db.query(Player)
    if q:
        # unaccent BOTH sides: 'Sesko' must find 'Šeško' during a live draft, and a
        # manager who does type 'Šeško' must still find him. One condition covers
        # every combination. Plain ILIKE misses these outright, and "no results"
        # is indistinguishable from "not in the pool" — see the unaccent migration.
        query = query.filter(  # search all (ignore position)
            func.unaccent(Player.name).ilike(func.unaccent(f"%{q}%"))
        )
    elif position:
        query = query.filter(Player.position == position.upper())
    if clubs_on:
        # Goalkeepers are owned through their club now, so they are not on the board
        # at all. Excluded in the QUERY rather than filtered later, so a name search
        # for a keeper comes back empty instead of offering an unpickable row.
        query = query.filter(Player.position != "GKP")

    if sort == "price":
        query = query.order_by(Player.price.desc().nulls_last(), Player.name)
    elif sort == "team":
        query = query.order_by(Player.current_team.asc().nulls_last(), Player.name)
    else:
        query = query.order_by(Player.name)  # 'points' is sorted after the fetch
    players = query.all()

    inelig = _ineligible_fpl_ids(db, league)  # post-draft non-DEF additions
    taken: dict = {}  # player_id -> label of who has them ("kept" / "drafted: X")
    if available_year is not None:
        names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
        for pid, mid in db.query(KeeperSelection.player_id, KeeperSelection.manager_id).filter_by(
            league_id=league.id, season_year=available_year
        ):
            # Not marked taken at all when hidden — an anonymous "kept" pill would
            # still tell you the player is off the board, which is the half that
            # matters. `kept_all` is also how the drafting engine asks for the truth:
            # see approve_queued_pick, where this is a correctness filter.
            if kept_all or (kept_for and mid in kept_for):
                taken[pid] = f"kept: {names.get(mid, '?')}"
        for pid, mid in (
            db.query(DraftPick.player_id, DraftPick.manager_id)
            .filter_by(league_id=league.id, season_year=available_year, draft_type=draft_type)
            .filter(DraftPick.player_id.isnot(None))
        ):
            taken[pid] = f"drafted: {names.get(mid, '?')}"

    out = []
    for p in players:
        # A player who has left the Premier League (this transfer window's departures)
        # keeps their row for history but loses their fpl_id — the same gap
        # keeper_candidates already guards against. Every write this feeds (record_pick,
        # queue/add, approve_queued_pick's availability check) resolves by fpl_id, and
        # None is not one — record_pick 500s (Player.fpl_id == None matches every
        # departed row, not zero or one) rather than raising a clean RuleViolation.
        # Treat it as its own unavailability reason, same shape as `ineligible`.
        not_in_pool = p.fpl_id is None
        ineligible = p.fpl_id in inelig
        is_taken = (p.id in taken) or ineligible or not_in_pool
        if is_taken and not include_taken:
            continue
        taken_by = (
            "ineligible (post-draft)" if ineligible
            else "no longer in the Premier League" if not_in_pool
            else taken.get(p.id)
        )
        ps = stats.get(p.id)  # None for players absent that season — still draftable
        out.append({
            "fpl_id": p.fpl_id, "name": p.name, "position": p.position, "team": p.current_team,
            "price": (p.price / 10) if p.price is not None else None,
            "points": ps.total_points if ps else None,
            "kind": "player", "team_code": None, "keepers": None,
            "taken": is_taken, "taken_by": taken_by, "ineligible": ineligible,
        })
    # Clubs matched by name go in front: a search for "Arsenal" wants the club, not
    # the twenty Arsenal players whose names happen to contain it.
    out = teams + out
    if sort == "points":
        # Same shape as the SQL orderings above: nulls last, descending, name tie-break.
        # Must run on the whole list — `limit` is applied by the slice below.
        out.sort(key=lambda r: (r["points"] is None, -(r["points"] or 0), r["name"]))
    return out[:limit]


# ---- trades view + draft helpers ----
def get_trades(db: Session, league: League) -> list[dict]:
    """All trades for the league — synced player trades and commissioner-entered
    pick/player trades — newest-ish first (by GW then id)."""
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    pnames = player_names(db, league)
    rows = db.query(Trade).filter_by(league_id=league.id).all()
    out = []
    for t in rows:
        if t.pick_round is not None:
            kind, what = "pick", t.draft_pick or f"R{t.pick_round} pick"
        else:
            kind, what = "player", pnames.get(t.player_id, "—")
        out.append({
            "id": str(t.id),
            "kind": kind,
            "what": what,
            "from": names.get(t.from_manager),
            "to": names.get(t.to_manager),
            "gw": t.event_gw,
            "source": "FPL" if t.fpl_trade_id else "site",
            "edited": bool(t.manually_edited),
        })
    out.sort(key=lambda x: (x["gw"] is None, x["gw"] or 0), reverse=True)
    return out


# ---- commissioner corrections -------------------------------------------------
# Historical records get things wrong: an import mis-keys a name, a trade is entered
# backwards, a pick is recorded against the wrong manager. Until now the only fix was
# editing the database by hand. These follow override_cup_match (edit in place) and
# delete_fine (resolve -> audit -> delete), and every one records the PREVIOUS values
# in the audit details — "what did it used to say" is the point of a correction log.

def _previous(row, fields: list[str]) -> dict:
    """Prior field values, JSON-safe, for the audit trail."""
    out = {}
    for f in fields:
        v = getattr(row, f, None)
        out[f] = str(v) if v is not None else None
    return out


def _trade_or_404(db: Session, league: League, trade_id: str) -> Trade:
    row = (
        db.query(Trade).filter_by(league_id=league.id, id=trade_id).one_or_none()
    )
    if not row:
        raise RuleViolation("trade not found")
    return row


def edit_trade(
    db: Session, league: League, trade_id: str, *,
    from_fpl: str | None = None, to_fpl: str | None = None,
    event_gw: int | None = None, conditions: str | None = None,
) -> dict:
    """Correct a trade. Only the fields passed are changed.

    Sets `manually_edited`, which stops sync_trades rewriting it back or, worse,
    re-inserting the uncorrected version as a duplicate (its reconciliation matches
    an exact from/to pair, so a flipped direction sails straight past it).
    """
    row = _trade_or_404(db, league, trade_id)
    prev = _previous(row, ["from_manager", "to_manager", "event_gw", "conditions"])

    if from_fpl:
        row.from_manager = _resolve_manager(db, league, from_fpl).id
    if to_fpl:
        row.to_manager = _resolve_manager(db, league, to_fpl).id
    if event_gw is not None:
        row.event_gw = event_gw
    if conditions is not None:
        row.conditions = conditions or None
    if row.from_manager == row.to_manager:
        raise RuleViolation("a trade needs two different managers")

    row.manually_edited = True
    record_audit(
        db, league, action="trade.edit",
        summary=f"Corrected a trade ({'FPL-sourced' if row.fpl_trade_id else 'site'})",
        manager_ids=[row.from_manager, row.to_manager],
        details={"trade_id": str(row.id), "previous": prev},
    )
    db.commit()
    return {"id": str(row.id)}


def delete_trade(db: Session, league: League, trade_id: str) -> None:
    row = _trade_or_404(db, league, trade_id)
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    record_audit(
        db, league, action="trade.delete",
        summary=(f"Deleted a trade: {names.get(row.from_manager, '?')} → "
                 f"{names.get(row.to_manager, '?')}"
                 + (f" (GW{row.event_gw})" if row.event_gw else "")),
        manager_ids=[row.from_manager, row.to_manager],
        details={"previous": _previous(row, [
            "from_manager", "to_manager", "player_id", "event_gw", "fpl_trade_id",
            "draft_pick", "pick_season_year", "pick_draft_type", "pick_round",
        ])},
    )
    db.delete(row)
    db.commit()


def _discovery_or_404(db: Session, league: League, result_id: str):
    from models import DiscoveryResult

    row = (
        db.query(DiscoveryResult)
        .filter_by(league_id=league.id, id=result_id)
        .one_or_none()
    )
    if not row:
        raise RuleViolation("discovery pick not found")
    return row


def edit_discovery_result(
    db: Session, league: League, result_id: str, *,
    manager_name: str | None = None, player_name: str | None = None,
) -> dict:
    """Correct an imported discovery pick. Both fields are free text (these rows are
    historical and predate the player table), so this is a plain text fix."""
    row = _discovery_or_404(db, league, result_id)
    prev = _previous(row, ["manager_name", "player_name"])
    if manager_name is not None:
        row.manager_name = manager_name.strip() or None
    if player_name is not None:
        row.player_name = player_name.strip() or None
    record_audit(
        db, league, action="discovery.edit",
        summary=(f"Corrected {row.season} discovery pick {row.pick_number}: "
                 f"{prev['manager_name']}/{prev['player_name']} → "
                 f"{row.manager_name}/{row.player_name}"),
        details={"result_id": str(row.id), "season": row.season, "previous": prev},
    )
    db.commit()
    return {"id": str(row.id)}


def delete_discovery_result(db: Session, league: League, result_id: str) -> None:
    row = _discovery_or_404(db, league, result_id)
    record_audit(
        db, league, action="discovery.delete",
        summary=(f"Deleted {row.season} discovery pick {row.pick_number} "
                 f"({row.manager_name} — {row.player_name})"),
        details={"season": row.season, "previous": _previous(
            row, ["round", "pick_number", "manager_name", "player_name"])},
    )
    db.delete(row)
    db.commit()


def delete_draft_pick(db: Session, league: League, pick_id: str) -> None:
    """Remove a recorded pick, freeing the slot so it can be re-recorded."""
    from models import DraftPick

    row = db.query(DraftPick).filter_by(league_id=league.id, id=pick_id).one_or_none()
    if not row:
        raise RuleViolation("draft pick not found")
    mgr = db.get(Manager, row.manager_id) if row.manager_id else None
    label = row.player_label
    if row.player_id:
        p = db.get(Player, row.player_id)
        label = p.name if p else label
    record_audit(
        db, league, action="pick.delete",
        summary=(f"Deleted {row.season_year} {row.draft_type} pick "
                 f"{row.pick_number} ({mgr.display if mgr else '?'} — {label or '—'})"),
        manager_ids=[row.manager_id] if row.manager_id else None,
        details={"previous": _previous(row, [
            "season_year", "draft_type", "round", "pick_number", "manager_id",
            "player_id", "player_label",
        ])},
    )
    db.delete(row)
    db.commit()


DISCOVERY_PICKS_PER_MANAGER = 2


def get_discovery_board(db: Session, league: League, season_year: int) -> list[dict]:
    """The discovery-draft board: a 2-round SNAKE over reverse-standings order (worst
    team picks first; round 2 reverses). All managers pick; no keepers/free picks.
    Overlays recorded discovery picks (DraftPick, draft_type='discovery')."""
    order = _reverse_standings_managers(db, league)
    if not order:
        order = db.query(Manager).filter_by(league_id=league.id).order_by(Manager.name).all()
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    fpl_by_id = {m.id: m.fpl_manager_id for m in db.query(Manager).filter_by(league_id=league.id)}

    slots = []
    for rnd in range(1, DISCOVERY_PICKS_PER_MANAGER + 1):
        seq = order if rnd % 2 == 1 else list(reversed(order))
        for m in seq:
            slots.append((rnd, m.id))

    picks = {
        dp.pick_number: dp
        for dp in db.query(DraftPick).filter_by(
            league_id=league.id, season_year=season_year, draft_type="discovery"
        )
    }
    pnames = {p.id: p.name for p in db.query(Player)}
    out = []
    for i, (rnd, mid) in enumerate(slots, start=1):
        dp = picks.get(i)
        player = None
        if dp:
            player = dp.player_label or (pnames.get(dp.player_id) if dp.player_id else None)
        out.append({
            "pick": i, "round": rnd,
            "owner": names.get(mid), "owner_fpl": fpl_by_id.get(mid),
            "player": player,
        })
    return out


def record_discovery_pick(
    db: Session, league: League, *, season_year: int, pick_number: int,
    owner_fpl: str, player_name: str, round: int = 0, overwrite: bool = False,
) -> dict:
    """Record a discovery-draft selection as a FREE-TEXT name (the player isn't in the
    league pool — they're a possible future PL arrival). Same slot/race guard as
    record_pick."""
    owner = _resolve_manager(db, league, owner_fpl)
    name = (player_name or "").strip()
    if not name:
        raise RuleViolation("enter a player name")
    existing = (
        db.query(DraftPick)
        .filter_by(league_id=league.id, season_year=season_year, draft_type="discovery", pick_number=pick_number)
        .one_or_none()
    )
    if existing:
        if (existing.player_label or existing.player_id) is not None and not overwrite:
            raise RuleViolation(f"pick {pick_number} has already been made")
        existing.manager_id, existing.player_id, existing.player_label = owner.id, None, name
    else:
        db.add(DraftPick(
            league_id=league.id, season_year=season_year, draft_type="discovery",
            pick_number=pick_number, round=round, manager_id=owner.id,
            player_id=None, player_label=name, source="discovery",
        ))
    record_audit(db, league, action="discovery.pick",
                 summary=(f"{owner.display} made discovery pick #{pick_number}: {name}"
                          + (" [overwrite]" if overwrite else "")),
                 manager_ids=[owner.id],
                 details={"season_year": season_year, "pick_number": pick_number,
                          "player_name": name, "overwrite": overwrite})
    db.commit()
    return {"pick": pick_number, "owner": owner.display, "player": name}


def next_open_pick(board: list[dict]) -> dict | None:
    """The on-the-clock slot: first board pick with no player recorded yet."""
    return next((b for b in board if not b.get("player")), None)


# ---- league history / honor roll ----
def get_history(db: Session, league: League) -> dict:
    """Season-by-season winners + career honor roll + per-season standings +
    discovery-draft results."""
    from models import DiscoveryResult, HistoricalStanding, ManagerHonors, SeasonHistory

    seasons = (
        db.query(SeasonHistory)
        .filter_by(league_id=league.id)
        .order_by(SeasonHistory.year.desc())
        .all()
    )
    honors = (
        db.query(ManagerHonors)
        .filter_by(league_id=league.id)
        .order_by(ManagerHonors.titles.desc(), ManagerHonors.cups.desc(), ManagerHonors.manager_name)
        .all()
    )
    standings_by_season: dict = {}
    for s in (
        db.query(HistoricalStanding)
        .filter_by(league_id=league.id)
        .order_by(HistoricalStanding.year.desc(), HistoricalStanding.rank)
        .all()
    ):
        standings_by_season.setdefault(s.year, []).append(
            {"rank": s.rank, "team": s.team_name, "manager": s.manager_name,
             "w": s.wins, "d": s.draws, "l": s.losses, "pf": s.points_for, "h2h": s.h2h_points}
        )
    return {
        "seasons": [
            {"year": s.year, "league": s.league_winner, "cup": s.cup_winner, "pup": s.pup_winner}
            for s in seasons
        ],
        "honors": [
            {"manager": h.manager_name, "titles": h.titles, "cups": h.cups} for h in honors
        ],
        "standings_by_season": [
            {"year": y, "rows": rows} for y, rows in standings_by_season.items()
        ],
        "discovery_by_season": _discovery_by_season(db, league),
        "cups_by_season": _cups_by_season(db, league),
    }


def _cups_by_season(db: Session, league: League) -> list[dict]:
    from models import CupMatch

    by_season: dict = {}
    for c in (
        db.query(CupMatch)
        .filter_by(league_id=league.id)
        .order_by(CupMatch.season.desc(), CupMatch.bracket, CupMatch.round, CupMatch.slot)
        .all()
    ):
        label = "Cup" if c.bracket == "cup" else "Pup Cup"
        rd = {1: "R1", 2: "Semi", 3: "Final"}.get(c.round, f"R{c.round}")
        by_season.setdefault(c.season, []).append({
            "bracket": label, "round": rd, "seed": c.seed,
            "manager": c.manager_label, "total": c.total,
        })
    return [{"year": y, "rows": rows} for y, rows in by_season.items()]


def _discovery_by_season(db: Session, league: League) -> list[dict]:
    from models import DiscoveryResult

    by_season: dict = {}
    for r in (
        db.query(DiscoveryResult)
        .filter_by(league_id=league.id)
        .order_by(DiscoveryResult.season.desc(), DiscoveryResult.pick_number)
        .all()
    ):
        by_season.setdefault(r.season, []).append(
            {"id": str(r.id), "pick": r.pick_number, "round": r.round,
             "manager": r.manager_name, "player": r.player_name}
        )
    return [{"year": y, "picks": rows} for y, rows in by_season.items()]


def corrections_data(db: Session, league: League) -> dict:
    """Everything the commissioner-corrections page edits, in one read: trades,
    imported discovery picks, and recorded draft picks."""
    from models import DraftPick

    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    picks = []
    for p in (
        db.query(DraftPick)
        .filter_by(league_id=league.id)
        .order_by(DraftPick.season_year.desc(), DraftPick.draft_type,
                  DraftPick.pick_number)
        .all()
    ):
        label = p.player_label
        if p.player_id:
            pl = db.get(Player, p.player_id)
            label = pl.name if pl else label
        picks.append({
            "id": str(p.id), "season_year": p.season_year, "draft_type": p.draft_type,
            "round": p.round, "pick_number": p.pick_number,
            "manager": names.get(p.manager_id), "player": label,
        })
    return {
        "trades": get_trades(db, league),
        "discovery": _discovery_by_season(db, league),
        "picks": picks,
        "managers": [
            {"name": m.display, "fpl": m.fpl_manager_id}
            for m in db.query(Manager).filter_by(league_id=league.id)
            .order_by(Manager.display_name)
        ],
    }


# ---- general trade entry (manager-usable, players + picks, no cap) ----
def manager_assets(db: Session, league: League, fpl_manager_id: str) -> dict:
    """A manager's tradeable assets: current-roster players + future picks they
    own (their own un-traded picks + picks acquired), across the next few years —
    plus their goalie team, which trades only for another goalie team."""
    m = _resolve_manager(db, league, fpl_manager_id)
    person = m.display
    persons = [mm.display for mm in db.query(Manager).filter_by(league_id=league.id)]

    players = []
    gw = latest_gameweek(db, league)
    if gw is not None:
        # fpl_id comes from the global `players` row (Player.fpl_id), NOT
        # PlayerSeason.fpl_id — every other write surface (search_players,
        # keeper_overrides_context) round-trips through _resolve_player, which
        # resolves against the global column. PlayerSeason's frozen id is a
        # DIFFERENT season's element id once this league is sync_locked, so using
        # it here submitted the right-looking checkbox with the wrong id underneath.
        for ps, p in (
            db.query(PlayerSeason, Player)
            .join(Roster, Roster.player_id == PlayerSeason.player_id)
            .join(Player, Player.id == PlayerSeason.player_id)
            .filter(
                Roster.manager_id == m.id,
                Roster.gameweek_id == gw.id,
                PlayerSeason.league_id == league.id,
            )
            .order_by(PlayerSeason.position, PlayerSeason.name)
        ):
            players.append({"fpl_id": p.fpl_id, "name": ps.name, "position": ps.position})

    upcoming = (league.season_year or 0) + 1
    picks = []
    for y in range(upcoming, upcoming + 6):  # next 6 seasons of future picks
        for dt, max_round in (("main", 15), ("discovery", 2)):
            own = pick_ownership(db, league, y, dt)
            for rnd in range(1, max_round + 1):
                for orig in persons:
                    if own.get((rnd, orig), orig) == person:
                        suffix = "" if orig == person else f" (orig {orig})"
                        picks.append({
                            "year": y, "draft_type": dt, "round": rnd, "original_owner": orig,
                            "label": f"{y} {dt} R{rnd}{suffix}",
                            "value": f"{y}:{dt}:{rnd}:{orig}",
                        })
    club = None
    if goalie_teams_on(league.goalie_team_mode):
        owner = goalie_team_owner(db, league)
        tid = next((t for t, fpl in owner.items() if fpl == m.fpl_manager_id), None)
        team = db.get(PlTeam, tid) if tid else None
        if team is not None:
            keepers = goalie_team_keepers(db, [team])[team.id]
            club = {"team_code": team.code, "name": team.name,
                    "short_name": team.short_name,
                    "keepers": [k.name for k in keepers]}
    return {"manager": person, "fpl": m.fpl_manager_id, "players": players,
            "picks": picks, "club": club}


def record_trade(
    db: Session, league: League, *, a_fpl: str, b_fpl: str,
    a_players: list, a_picks: list, b_players: list, b_picks: list,
    a_clubs: list | None = None, b_clubs: list | None = None,
) -> dict:
    """Record a trade between two managers: any players + picks each way, no cap.
    Each asset becomes a Trade row; pick assets reassign ownership via the shared
    pick_ownership computation. Not admin-gated.

    Goalie teams are the one asset with a cap, because the rule has one: a manager
    holds exactly one, so a club must be met by a club going the other way. A
    club-for-player trade would leave one manager with two and the other with none,
    and neither is a legal squad.
    """
    A = _resolve_manager(db, league, a_fpl)
    B = _resolve_manager(db, league, b_fpl)
    if A.id == B.id:
        raise RuleViolation("pick two different managers")
    by_person = {m.display: m for m in db.query(Manager).filter_by(league_id=league.id)}

    def add_player(frm, to, fpl):
        p = _resolve_player(db, int(fpl))
        db.add(Trade(league_id=league.id, from_manager=frm.id, to_manager=to.id, player_id=p.id))

    def add_pick(frm, to, spec):
        y, dt, rnd, orig = spec.split(":")
        owner = by_person.get(orig)
        if not owner:
            raise RuleViolation(f"unknown pick original owner '{orig}'")
        db.add(Trade(
            league_id=league.id, from_manager=frm.id, to_manager=to.id,
            pick_season_year=int(y), pick_draft_type=dt, pick_round=int(rnd),
            pick_original_manager=owner.id, draft_pick=f"{y} {dt} R{rnd} (orig {orig})",
        ))

    for fpl in a_players:
        add_player(A, B, fpl)
    for fpl in b_players:
        add_player(B, A, fpl)
    for spec in a_picks:
        add_pick(A, B, spec)
    for spec in b_picks:
        add_pick(B, A, spec)

    a_clubs, b_clubs = list(a_clubs or []), list(b_clubs or [])
    if len(a_clubs) > 1 or len(b_clubs) > 1:
        raise RuleViolation("a manager has only one goalie team to trade")
    if len(a_clubs) != len(b_clubs):
        raise RuleViolation(
            "a goalie team has to be traded for a goalie team — one each way, or "
            "neither"
        )
    if a_clubs:
        # Both legs judged against the state before EITHER happens. Sequencing them
        # would have the first leg leave B holding two clubs, and the second refuse.
        owner = goalie_team_owner(db, league)
        _add_club_trade(db, league, A, B, _resolve_team(db, int(a_clubs[0])), owner)
        _add_club_trade(db, league, B, A, _resolve_team(db, int(b_clubs[0])), owner)
    moved = (len(a_players) + len(b_players) + len(a_picks) + len(b_picks)
             + len(a_clubs) + len(b_clubs))
    # This was the one write path in the app that never wrote an audit entry, which
    # left the everyday trade form invisible in the log it exists to complete.
    record_audit(
        db, league, action="trade.record",
        summary=(f"Trade: {A.display} ↔ {B.display} ({moved} asset"
                 f"{'s' if moved != 1 else ''})"),
        manager_ids=[A.id, B.id],
        details={"a_players": list(a_players), "b_players": list(b_players),
                 "a_picks": list(a_picks), "b_picks": list(b_picks)},
    )
    db.commit()
    return {"a": A.display, "b": B.display, "assets_moved": moved}


# ---- data-quality health checks (commissioner ops) ----
def data_health(db: Session, league: League) -> list[dict]:
    """Run lightweight data-integrity checks; returns [{check, ok, detail}]."""
    from models import Gameweek, GameweekPoints, KeeperSeed

    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    mgrs = db.query(Manager).filter_by(league_id=league.id).all()
    add("10 managers", len(mgrs) == 10, f"{len(mgrs)} found")

    # A sync that refused a feed writes nothing, so no other check would notice.
    # Surface it here: silence is how a recycled league id went unseen for days.
    from models import SyncLog

    last = (
        db.query(SyncLog)
        .filter_by(kind="league")
        .order_by(SyncLog.started_at.desc())
        .first()
    )
    if last is not None and last.ok is False:
        add("last league sync", False, last.notes or "failed")
    elif league.sync_locked:
        add("season frozen against FPL sync", True,
            f"{league.season_year} is final; roll over to sync a new season")

    missing_person = [m.name for m in mgrs if not m.display_name]
    add("all managers have a person name", not missing_person,
        ", ".join(missing_person) if missing_person else "ok")

    sc = db.query(Standing).filter_by(league_id=league.id).count()
    add("standings row per manager", sc == len(mgrs), f"{sc}/{len(mgrs)}")

    gwp = (
        db.query(GameweekPoints)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(Gameweek.league_id == league.id)
        .count()
    )
    add("gameweek points populated", gwp > 0, f"{gwp} rows")

    gw = latest_gameweek(db, league)
    bad_rosters = []
    if gw is not None:
        counts: dict = {}
        for (mid,) in db.query(Roster.manager_id).filter_by(gameweek_id=gw.id):
            counts[mid] = counts.get(mid, 0) + 1
        names = {m.id: m.display for m in mgrs}
        bad_rosters = [f"{names.get(m.id)}={counts.get(m.id, 0)}" for m in mgrs if counts.get(m.id, 0) != 15]
    # Deliberately raw Roster, NOT the trade overlay: this check validates the SYNC.
    # A player-for-pick trade legitimately leaves one manager at 14 and another at 16
    # until the draft fills them back, and overlaying it here would turn a legal trade
    # into a permanent red check while hiding a genuine sync gap.
    add(f"15-man rosters (FPL, GW{gw.number if gw else '?'})", not bad_rosters,
        ", ".join(bad_rosters) if bad_rosters else "all 15")

    # Commissioner-entered trades that didn't apply, because the player isn't where
    # the trade says he is. player_ownership skips these deliberately (failing closed
    # beats teleporting someone), which means a typo'd direction otherwise does
    # nothing at all, silently.
    unapplied = []
    if gw is not None:
        owner = effective_owner(db, league)
        names = {m.id: m.display for m in mgrs}
        pnames = player_names(db, league)
        for t in (
            db.query(Trade)
            .filter(Trade.league_id == league.id, Trade.player_id.isnot(None),
                    Trade.pick_round.is_(None), Trade.fpl_trade_id.is_(None),
                    Trade.event_gw.is_(None))
            .order_by(Trade.created_at, Trade.id)
        ):
            if owner.get(t.player_id) != t.to_manager:
                unapplied.append(
                    f"{pnames.get(t.player_id, '?')} -> {names.get(t.to_manager, '?')}"
                    f" (held by {names.get(owner.get(t.player_id), 'nobody')})"
                )
    add("site trades applied", not unapplied,
        "; ".join(unapplied) if unapplied else "all applied")

    # players on the latest roster with no keeper seed (they default to fresh)
    seeded = {pid for (pid,) in db.query(KeeperSeed.player_id).filter_by(league_id=league.id)}
    on_roster = (
        {pid for (pid,) in db.query(Roster.player_id).filter_by(gameweek_id=gw.id)}
        if gw is not None else set()
    )
    unseeded = on_roster - seeded
    if goalie_teams_on(league.goalie_team_mode):
        # A goalkeeper can never be an individual keeper now, so he can never need a
        # seed. Left in, this check lists every keeper in the league, forever, and the
        # genuinely unseeded players get lost in it.
        gk_ids = {
            pid for (pid,) in db.query(Player.id).filter(Player.position == "GKP")
        }
        unseeded -= gk_ids
    add("rostered players have a keeper seed", not unseeded,
        f"{len(unseeded)} without a seed" if unseeded else "ok")

    if goalie_teams_on(league.goalie_team_mode):
        upcoming = (league.season_year or 0) + 1
        picks = (
            db.query(DraftPick)
            .filter(DraftPick.league_id == league.id,
                    DraftPick.season_year == upcoming,
                    DraftPick.draft_type == "main",
                    DraftPick.team_id.isnot(None))
            .all()
        )
        names = {m.id: m.display for m in mgrs}
        tnames = {t.id: t.name for t in db.query(PlTeam)}
        per_manager: dict = {}
        per_club: dict = {}
        for dp in picks:
            per_manager.setdefault(dp.manager_id, []).append(dp)
            per_club.setdefault(dp.team_id, []).append(dp)
        # Both are backed by partial unique indexes, so a violation means somebody
        # wrote around the service layer. Surfaced anyway: the draft is unrecoverable
        # once two managers believe they own the same club.
        doubled = [names.get(mid, "?") for mid, rows in per_manager.items() if len(rows) > 1]
        add("one goalie team per manager", not doubled,
            ", ".join(doubled) if doubled else f"{len(per_manager)}/{len(mgrs)} drafted")
        shared = [tnames.get(tid, "?") for tid, rows in per_club.items() if len(rows) > 1]
        add("no club drafted twice", not shared,
            ", ".join(shared) if shared else "ok")

        # A submitted keeper that resolves to nothing is invisible everywhere else:
        # advance_season skips it silently and the manager just quietly loses a slot.
        sel = db.query(KeeperSelection).filter_by(
            league_id=league.id, season_year=upcoming
        ).all()
        counted = {
            (x.manager_id, x.player_id, x.team_id)
            for x in effective_keeper_selections(db, league, upcoming)
        }
        stale = [names.get(x.manager_id, "?") for x in sel
                 if (x.manager_id, x.player_id, x.team_id) not in counted]
        add("submitted keepers all still count", not stale,
            f"{len(stale)} no longer held ({', '.join(sorted(set(stale)))})"
            if stale else "ok")

    # pick trades must name an original owner
    bad_picks = (
        db.query(Trade)
        .filter(Trade.league_id == league.id, Trade.pick_round.isnot(None),
                Trade.pick_original_manager.is_(None))
        .count()
    )
    add("pick trades have an original owner", bad_picks == 0,
        f"{bad_picks} malformed" if bad_picks else "ok")

    # A player who vanished from a roster near the end of the season with no IL
    # record covering it — either an ordinary drop or an unrecorded IL absence
    # (see the Šeško case). Only the commissioner can tell which; this just makes
    # sure they get asked instead of never finding out. Full detail on /admin/keepers.
    gaps = unexplained_roster_gaps(db, league)
    add(
        "no unexplained late-season roster gaps",
        not gaps,
        (f"{len(gaps)} — review at /admin/keepers: "
         + "; ".join(f"{g['manager']}: {g['player']} (last active GW{g['last_active_gw']})"
                     for g in gaps))
        if gaps else "ok",
    )

    return checks


# ---- keeper selection UI support ----
def keeper_candidates(db: Session, league: League, fpl_manager_id: str) -> dict:
    """A manager's roster players with keeper eligibility (for the selection UI):
    fpl_id, name, position, acquisition, years_remaining, eligible."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    # This manager's own screen, so their own flags are truthful. Note this is NOT the
    # access control for the route — `selected`/`is_discovery`/`discovery` below come
    # from a separate query, so the route's can_act_as check is what protects them.
    status = _derive_keeper_status(db, league, kept_for={manager.id}).get(manager.id, {})
    fpl_by_id = {p.id: p.fpl_id for p in db.query(Player)}
    # A player who has left the Premier League keeps their row but loses their
    # fpl_id (the slot goes back to FPL). The form submits by fpl_id, so without
    # this they'd render as a selectable candidate and then silently fail. Mark
    # them ineligible with a reason instead.
    items = []
    for pid, v in status.items():
        fid = fpl_by_id.get(pid)
        item = {**v, "fpl_id": fid, "_pid": pid}
        if fid is None:
            item["eligible"] = False
            item["reason"] = "no longer in the Premier League"
        items.append(item)
    items.sort(key=lambda x: (not x["eligible"], -x["years_remaining"], x["player"]))
    # current submitted selection (upcoming season) so the form can preselect
    upcoming = (league.season_year or 0) + 1
    rows_sel = db.query(KeeperSelection).filter_by(
        league_id=league.id, manager_id=manager.id, season_year=upcoming
    ).all()
    selected = {s.player_id: s.is_discovery for s in rows_sel if s.player_id}
    selected_team = next((s.team_id for s in rows_sel if s.team_id), None)
    # Match on the stable player_id, not fpl_id: a departed player's fpl_id is None,
    # and keying on that would make EVERY departed player look selected.
    for it in items:
        it["selected"] = it["_pid"] in selected
        it["is_discovery"] = selected.get(it["_pid"], False)
        del it["_pid"]
    # the saved discovery keeper may be off-roster (it can be any player), so
    # surface it independently for the search UI to pre-fill
    discovery = None
    disc_pid = next((pid for pid, d in selected.items() if d), None)
    if disc_pid is not None:
        p = db.get(Player, disc_pid)
        if p:
            discovery = {"fpl_id": p.fpl_id, "player": p.name}
    # The manager's goalie team, under 'keeper' mode. Empty dict rather than a missing
    # key so the template can ask once; `keepable` says whether the checkbox exists at
    # all, which is the difference between the two modes.
    club = _derive_gk_team_keeper_status(db, league, kept_for={manager.id}).get(manager.id)
    if club is not None:
        club = {**club, "selected": selected_team == club["team_id"]}
    return {"manager": manager.display, "fpl": manager.fpl_manager_id,
            "season": upcoming, "players": items, "discovery": discovery,
            "goalie_team": club,
            "goalie_team_keepable": goalie_team_keepable(league.goalie_team_mode)}


def get_trade_notes(db: Session, league: League) -> list[dict]:
    """Historical free-text trades (couldn't be normalized), grouped by season."""
    from models import TradeNote

    by_season: dict = {}
    for t in (
        db.query(TradeNote).filter_by(league_id=league.id)
        .order_by(TradeNote.season.desc(), TradeNote.id).all()
    ):
        by_season.setdefault(t.season, []).append(
            {"a": t.manager_a, "gives_a": t.gives_a, "b": t.manager_b, "gives_b": t.gives_b}
        )
    return [{"year": y, "trades": v} for y, v in by_season.items()]
