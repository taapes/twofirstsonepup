"""Read-only query helpers serving PRECOMPUTED data from our tables.

Per the architecture (CLAUDE.md), these never call the FPL API — they read the
synced/normalized rows. Shared by the JSON API (api.py) and the homepage
(main.py) so both render the same data.
"""

import os

from sqlalchemy import func
from sqlalchemy.orm import Session

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
    PlayerSeason,
    Roster,
    Tournament,
    TournamentMatch,
    Trade,
    V2GameweekScore,
    V2Lineup,
    V2RosterMove,
    V2WaiverClaim,
    V2WaiverState,
    V2Match,
)
from rules import (
    ANTI_TANKING_MIN_WEEKS,
    ANTI_TANKING_MIN_ZERO_PLAYERS,
    CUP_SEED_THROUGH_GW,
    CUP_SIZE,
    CUP_START_GW,
    DISCOVERY_OPEN_DAY,
    DISCOVERY_OPEN_MONTH,
    KEEPER_FRESH_REMAINING,
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
    next_phase,
    phase_features,
    h2h_standings,
    il_can_return,
    il_same_position,
    validate_lineup,
    fold_moves,
    validate_roster_add,
    validate_roster_drop,
    initial_waiver_order,
    resolve_waivers,
    advance_waiver_priority,
    season_schedule,
    ROSTER_SIZE,
    generate_draft_slots,
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
    if app_engine_on():
        return _engine_scoreboard(db, league, gw_number)
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


# ---- v2 app-owned weekly lineups (M2; see rules.validate_lineup + scoring.py) ----

def _gw_by_number(db: Session, league: League, gw_number: int) -> Gameweek | None:
    return db.query(Gameweek).filter_by(league_id=league.id, number=gw_number).one_or_none()


def lineup_locked(db: Session, league: League, gw: Gameweek) -> bool:
    """A GW's lineup locks at its deadline (its start date). On/after the start date
    the lineup is frozen; before it, the manager can still edit."""
    import datetime as _dt

    return gw.start_date is not None and _dt.date.today() >= gw.start_date


def get_lineup(db: Session, league: League, fpl_manager_id: str, gw_number: int) -> dict | None:
    """The manager's submitted XI+bench for a GW, or None if they haven't set one
    (the engine then falls back to FPL's picks for that GW). Returns
    `{starters: [fpl_id...], bench: [fpl_id...]}`."""
    manager = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    gw = _gw_by_number(db, league, gw_number)
    if not manager or not gw:
        return None
    row = (
        db.query(V2Lineup)
        .filter_by(manager_id=manager.id, gameweek_id=gw.id)
        .one_or_none()
    )
    if not row:
        return None
    return {"starters": list(row.starters or []), "bench": list(row.bench or [])}


def set_lineup(
    db: Session,
    league: League,
    fpl_manager_id: str,
    gw_number: int,
    starters: list,
    bench: list,
    *,
    allow_locked: bool = False,
) -> dict:
    """Set a manager's XI+bench for a GW, enforcing the lineup rules
    (`rules.validate_lineup`) against their 15 owned players. Locks at the GW
    deadline unless `allow_locked` (admin). Upserts + audits. Raises RuleViolation
    on an illegal lineup, a locked GW, or a missing squad."""
    manager = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not manager:
        raise RuleViolation("Unknown manager.")
    gw = _gw_by_number(db, league, gw_number)
    if not gw:
        raise RuleViolation(f"Gameweek {gw_number} not found.")
    if not allow_locked and lineup_locked(db, league, gw):
        raise RuleViolation(f"GW{gw_number} lineup is locked (deadline passed).")

    squad = _squad_players(db, manager.id, gw.id)
    if len(squad) != ROSTER_SIZE:
        raise RuleViolation(
            f"Your GW{gw_number} squad has {len(squad)} players, expected "
            f"{ROSTER_SIZE} — can't set a lineup yet."
        )
    pos_by_pid = {p.fpl_id: p.position for p in squad}
    starters = [int(x) for x in starters]
    bench = [int(x) for x in bench]

    validate_lineup(starters, bench, pos_by_pid, set(pos_by_pid))

    row = (
        db.query(V2Lineup)
        .filter_by(manager_id=manager.id, gameweek_id=gw.id)
        .one_or_none()
    )
    if row is None:
        row = V2Lineup(manager_id=manager.id, gameweek_id=gw.id)
        db.add(row)
    row.starters = starters
    row.bench = bench
    record_audit(
        db, league, action="set_lineup",
        summary=f"{manager.display} set GW{gw_number} lineup",
        manager_ids=[manager.fpl_manager_id],
        details={"gw": gw_number, "starters": starters, "bench": bench},
    )
    db.commit()
    return {"starters": starters, "bench": bench}


def get_lineup_editor(
    db: Session, league: League, fpl_manager_id: str, gw_number: int | None = None
) -> dict | None:
    """Everything the lineup-editor page needs: the manager's 15 owned players for a
    GW (position-sorted), each player's current role (start / bench N / unassigned)
    from any saved lineup, the target GW, and whether it's locked. None if the
    manager isn't found. Defaults the GW to the current one (or the latest synced)."""
    manager = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not manager:
        return None
    if gw_number is None:
        gw_number = current_gameweek(db, league)
    if gw_number is None:
        latest = latest_gameweek(db, league)
        gw_number = latest.number if latest else None
    gw = _gw_by_number(db, league, gw_number) if gw_number is not None else None
    if not gw:
        return {"manager": manager.display, "fpl": manager.fpl_manager_id,
                "gameweek": gw_number, "locked": True, "has_lineup": False,
                "players": []}

    squad = _squad_players(db, manager.id, gw.id)
    existing = get_lineup(db, league, fpl_manager_id, gw_number)
    role: dict = {}
    if existing:
        for pid in existing["starters"]:
            role[pid] = ("start", None)
        for i, pid in enumerate(existing["bench"], start=1):
            role[pid] = ("bench", i)

    players = []
    for p in squad:
        r, order = role.get(p.fpl_id, (None, None))
        players.append({"fpl_id": p.fpl_id, "name": p.name, "position": p.position,
                        "role": r, "bench_order": order})
    players.sort(key=lambda d: (_POSITION_ORDER.get(d["position"], 9), d["name"]))
    return {
        "manager": manager.display, "fpl": manager.fpl_manager_id,
        "gameweek": gw_number, "locked": lineup_locked(db, league, gw),
        "has_lineup": existing is not None, "players": players,
        "squad_size": len(squad), "roster_size": ROSTER_SIZE,
    }


# ---- v2 app-owned squad ledger (M3; see rules.fold_moves + validate_roster_*) ----

def _v2_roster_snapshots(db: Session, league: League) -> dict:
    """(manager_id, gw_number) -> set(player_id) from the synced FPL roster snapshots.
    The source we seed the ledger from and validate the fold against."""
    snaps: dict = {}
    for mid, pid, gnum in (
        db.query(Roster.manager_id, Roster.player_id, Gameweek.number)
        .join(Gameweek, Gameweek.id == Roster.gameweek_id)
        .filter(Gameweek.league_id == league.id)
    ):
        snaps.setdefault((mid, gnum), set()).add(pid)
    return snaps


def seed_v2_ledger(db: Session, league: League) -> int:
    """(Re)build the app-owned squad ledger from FPL roster history: the earliest GW
    snapshot becomes `add` (source 'initial'); each later GW's diff vs the previous
    becomes `add`/`drop` (source 'sync'). This gives the ledger a real starting point
    to test the fold; going forward, in-app ops append moves instead. Idempotent —
    clears this league's moves first. Returns the number of moves written."""
    db.query(V2RosterMove).filter_by(league_id=league.id).delete()
    snaps = _v2_roster_snapshots(db, league)
    by_manager: dict = {}
    for (mid, gnum) in snaps:
        by_manager.setdefault(mid, set()).add(gnum)

    written = 0
    for mid, gnums in by_manager.items():
        gws = sorted(gnums)
        first = gws[0]
        for pid in snaps.get((mid, first), set()):
            db.add(V2RosterMove(league_id=league.id, manager_id=mid, player_id=pid,
                                gw_number=first, action="add", source="initial"))
            written += 1
        for prev, gw in zip(gws, gws[1:]):
            before = snaps.get((mid, prev), set())
            after = snaps.get((mid, gw), set())
            for pid in (after - before):
                db.add(V2RosterMove(league_id=league.id, manager_id=mid, player_id=pid,
                                    gw_number=gw, action="add", source="sync"))
                written += 1
            for pid in (before - after):
                db.add(V2RosterMove(league_id=league.id, manager_id=mid, player_id=pid,
                                    gw_number=gw, action="drop", source="sync"))
                written += 1
    db.commit()
    return written


def _v2_moves(db: Session, league: League, manager_id=None, through_gw=None) -> list:
    """Ordered `(player_id, action)` moves for folding. Ordered by (gw, created_at,
    id) so replays are deterministic."""
    q = db.query(V2RosterMove).filter_by(league_id=league.id)
    if manager_id is not None:
        q = q.filter(V2RosterMove.manager_id == manager_id)
    if through_gw is not None:
        q = q.filter(V2RosterMove.gw_number <= through_gw)
    q = q.order_by(V2RosterMove.gw_number, V2RosterMove.created_at, V2RosterMove.id)
    return [(m.player_id, m.action) for m in q]


def get_v2_squad(db: Session, league: League, fpl_manager_id: str, as_of_gw=None) -> set:
    """A manager's app-owned squad = the fold of their ledger up to `as_of_gw`
    (default: all moves). Returns a set of player UUIDs."""
    manager = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not manager:
        return set()
    return fold_moves(_v2_moves(db, league, manager.id, as_of_gw))


def v2_ledger_diff(db: Session, league: League) -> dict:
    """Dual-run validation: at every GW, the ledger fold should reproduce the FPL
    roster snapshot exactly. Reports per-(manager, GW) mismatches (extra/missing
    player counts). A clean diff means the app-owned ledger faithfully represents
    ownership and can take over from the FPL roster sync."""
    snaps = _v2_roster_snapshots(db, league)
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    moves_by_mgr: dict = {}
    for m in (
        db.query(V2RosterMove)
        .filter_by(league_id=league.id)
        .order_by(V2RosterMove.gw_number, V2RosterMove.created_at, V2RosterMove.id)
    ):
        moves_by_mgr.setdefault(m.manager_id, []).append(m)

    checked, mismatch, rows = 0, 0, []
    for (mid, gnum), fpl_set in snaps.items():
        folded = fold_moves(
            (m.player_id, m.action) for m in moves_by_mgr.get(mid, []) if m.gw_number <= gnum
        )
        checked += 1
        if folded != fpl_set:
            mismatch += 1
            rows.append({"gw": gnum, "manager": names.get(mid),
                         "extra": len(folded - fpl_set), "missing": len(fpl_set - folded)})
    return {
        "checked": checked, "mismatch": mismatch,
        "moves": db.query(V2RosterMove).filter_by(league_id=league.id).count(),
        "rows": sorted(rows, key=lambda r: (r["gw"], r["manager"] or "")),
    }


def roster_add(db: Session, league: League, fpl_manager_id: str, player_fpl_id: int,
               gw_number: int, source: str = "free_agent") -> None:
    """App-owned squad add: append an `add` move after enforcing exclusive ownership
    and the squad cap (`rules.validate_roster_add`). Audited."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id)
    squad = fold_moves(_v2_moves(db, league, manager.id))
    owner_of = _v2_current_owner(db, league, player.id)
    validate_roster_add(
        squad_size=len(squad),
        already_owned=(owner_of == manager.id),
        owned_by_other=(owner_of is not None and owner_of != manager.id),
    )
    db.add(V2RosterMove(league_id=league.id, manager_id=manager.id, player_id=player.id,
                        gw_number=gw_number, action="add", source=source))
    record_audit(db, league, action="roster_add",
                 summary=f"{manager.display} added {player.name} (GW{gw_number})",
                 manager_ids=[manager.fpl_manager_id],
                 details={"player": player.name, "gw": gw_number, "source": source})
    db.commit()


def roster_drop(db: Session, league: League, fpl_manager_id: str, player_fpl_id: int,
                gw_number: int, source: str = "free_agent") -> None:
    """App-owned squad drop: append a `drop` move after confirming ownership. Audited."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id)
    squad = fold_moves(_v2_moves(db, league, manager.id))
    validate_roster_drop(owned=(player.id in squad))
    db.add(V2RosterMove(league_id=league.id, manager_id=manager.id, player_id=player.id,
                        gw_number=gw_number, action="drop", source=source))
    record_audit(db, league, action="roster_drop",
                 summary=f"{manager.display} dropped {player.name} (GW{gw_number})",
                 manager_ids=[manager.fpl_manager_id],
                 details={"player": player.name, "gw": gw_number, "source": source})
    db.commit()


def _v2_current_owner(db: Session, league: League, player_id):
    """Which manager (id) currently owns a player in the v2 ledger, or None. Exclusive
    ownership across the league."""
    for mid, squad in _v2_all_squads(db, league).items():
        if player_id in squad:
            return mid
    return None


def _v2_all_squads(db: Session, league: League) -> dict:
    """{manager_id: set(player_id)} — every manager's current ledger-folded squad."""
    moves: dict = {}
    for m in (
        db.query(V2RosterMove)
        .filter_by(league_id=league.id)
        .order_by(V2RosterMove.gw_number, V2RosterMove.created_at, V2RosterMove.id)
    ):
        moves.setdefault(m.manager_id, []).append((m.player_id, m.action))
    return {mid: fold_moves(ms) for mid, ms in moves.items()}


def _v2_all_owned(db: Session, league: League) -> set:
    """All player ids currently owned by any manager (unavailable to add)."""
    owned: set = set()
    for squad in _v2_all_squads(db, league).values():
        owned |= squad
    return owned


def _append_move(db: Session, league: League, manager_id, player_id, gw_number: int,
                 action: str, source: str) -> None:
    """Append one ledger move (no commit — caller commits)."""
    db.add(V2RosterMove(league_id=league.id, manager_id=manager_id, player_id=player_id,
                        gw_number=gw_number, action=action, source=source))


# ---- v2 waivers + free agency + trades (M4) ----

def get_waiver_order(db: Session, league: League) -> dict:
    """Current waiver priority `{manager_id: priority}` (0 = first). Initialised from
    reverse standings (worst team first) the first time it's needed."""
    rows = db.query(V2WaiverState).filter_by(league_id=league.id).all()
    if rows:
        return {r.manager_id: r.priority for r in rows}
    standings = db.query(Standing).filter_by(league_id=league.id).all()
    if standings:
        worst_to_best = [
            r.manager_id for r in sorted(standings, key=lambda r: (r.total or 0, r.points_for or 0))
        ]
    else:
        worst_to_best = [
            m.id for m in db.query(Manager).filter_by(league_id=league.id).order_by(Manager.display_name)
        ]
    order = initial_waiver_order(worst_to_best)
    for mid, prio in order.items():
        db.add(V2WaiverState(league_id=league.id, manager_id=mid, priority=prio))
    db.commit()
    return order


def _save_waiver_order(db: Session, league: League, order: dict) -> None:
    for r in db.query(V2WaiverState).filter_by(league_id=league.id):
        if r.manager_id in order:
            r.priority = order[r.manager_id]


def submit_waiver_claim(db: Session, league: League, fpl_manager_id: str,
                        add_fpl: int, drop_fpl: int | None, gw_number: int) -> None:
    """Submit a blind waiver claim: add a free agent, dropping one of your players.
    Validates the add is currently a free agent and the drop is on your squad. The
    claim is resolved later by `process_waivers`. Audited."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    add_player = _resolve_player(db, add_fpl)
    squad = fold_moves(_v2_moves(db, league, manager.id))
    if _v2_current_owner(db, league, add_player.id) is not None:
        raise RuleViolation(f"{add_player.name} is already on a squad — not a free agent.")
    drop_player = None
    if drop_fpl is not None:
        drop_player = _resolve_player(db, drop_fpl)
        if drop_player.id not in squad:
            raise RuleViolation(f"You can't drop {drop_player.name} — not on your squad.")
    elif len(squad) >= ROSTER_SIZE:
        raise RuleViolation("Your squad is full; a claim must name a player to drop.")
    db.add(V2WaiverClaim(
        league_id=league.id, manager_id=manager.id, gw_number=gw_number,
        add_player_id=add_player.id,
        drop_player_id=drop_player.id if drop_player else None,
        status="pending",
    ))
    record_audit(db, league, action="waiver_claim",
                 summary=f"{manager.display} claimed {add_player.name} (GW{gw_number})",
                 manager_ids=[manager.fpl_manager_id],
                 details={"add": add_player.name,
                          "drop": drop_player.name if drop_player else None, "gw": gw_number})
    db.commit()


def process_waivers(db: Session, league: League, gw_number: int) -> dict:
    """Process all pending claims for a GW in priority order, apply winners to the
    ledger, mark each claim won/lost, and roll winners to the back of the waiver
    order. Returns a summary. The core decision is the pure `rules.resolve_waivers`."""
    import datetime as _dt

    order = get_waiver_order(db, league)
    claims = (
        db.query(V2WaiverClaim)
        .filter_by(league_id=league.id, gw_number=gw_number, status="pending")
        .all()
    )
    claims.sort(key=lambda c: (order.get(c.manager_id, 10_000), c.created_at))
    squads = _v2_all_squads(db, league)
    owned = set().union(*squads.values()) if squads else set()

    decide_input = [{
        "id": c.id, "add": c.add_player_id,
        "drop_owned": (c.drop_player_id is None
                       or c.drop_player_id in squads.get(c.manager_id, set())),
    } for c in claims]
    results = resolve_waivers(decide_input, owned)

    now = _dt.datetime.now(_dt.timezone.utc)
    winners, won = [], 0
    for c in claims:
        status, reason = results[c.id]
        c.status, c.reason, c.processed_at = status, reason, now
        if status == "won":
            if c.drop_player_id is not None:
                _append_move(db, league, c.manager_id, c.drop_player_id, gw_number, "drop", "waiver")
            _append_move(db, league, c.manager_id, c.add_player_id, gw_number, "add", "waiver")
            winners.append(c.manager_id)
            won += 1
    _save_waiver_order(db, league, advance_waiver_priority(order, winners))
    record_audit(db, league, action="waivers_processed",
                 summary=f"Processed {len(claims)} waiver claims for GW{gw_number} ({won} won)",
                 details={"gw": gw_number, "won": won, "total": len(claims)})
    db.commit()
    return {"processed": len(claims), "won": won, "lost": len(claims) - won}


def free_agent_move(db: Session, league: League, fpl_manager_id: str,
                    add_fpl: int, drop_fpl: int | None, gw_number: int,
                    allow_out_of_window: bool = False) -> None:
    """Immediate free-agency add/drop (final 24h before a GW). Unlike waivers this is
    first-come, applied instantly. Enforces the FA window (admin bypass via
    `allow_out_of_window`), exclusive ownership, and the squad cap. Audited."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    add_player = _resolve_player(db, add_fpl)
    if not allow_out_of_window and waiver_window(db, league).get("state") != "free_agency":
        raise RuleViolation("Free agency isn't open yet (still the waiver period).")
    squad = fold_moves(_v2_moves(db, league, manager.id))
    owner = _v2_current_owner(db, league, add_player.id)
    drop_player = None
    if drop_fpl is not None:
        drop_player = _resolve_player(db, drop_fpl)
        validate_roster_drop(owned=(drop_player.id in squad))
    size_after_drop = len(squad) - (1 if drop_player else 0)
    validate_roster_add(
        squad_size=size_after_drop,
        already_owned=(owner == manager.id),
        owned_by_other=(owner is not None and owner != manager.id),
    )
    if drop_player is not None:
        _append_move(db, league, manager.id, drop_player.id, gw_number, "drop", "free_agent")
    _append_move(db, league, manager.id, add_player.id, gw_number, "add", "free_agent")
    record_audit(db, league, action="free_agent_move",
                 summary=f"{manager.display} signed {add_player.name}"
                         + (f", dropped {drop_player.name}" if drop_player else "")
                         + f" (GW{gw_number})",
                 manager_ids=[manager.fpl_manager_id],
                 details={"add": add_player.name,
                          "drop": drop_player.name if drop_player else None, "gw": gw_number})
    db.commit()


def v2_execute_trade(db: Session, league: League, a_fpl: str, a_player_fpl: int,
                     b_fpl: str, b_player_fpl: int, gw_number: int) -> None:
    """Execute a player-for-player trade against the ledger: each side must own the
    player they're sending. Appends the four moves (drop+add per player) and audits."""
    a = _resolve_manager(db, league, a_fpl)
    b = _resolve_manager(db, league, b_fpl)
    pa = _resolve_player(db, a_player_fpl)
    pb = _resolve_player(db, b_player_fpl)
    squad_a = fold_moves(_v2_moves(db, league, a.id))
    squad_b = fold_moves(_v2_moves(db, league, b.id))
    if pa.id not in squad_a:
        raise RuleViolation(f"{a.display} doesn't own {pa.name}.")
    if pb.id not in squad_b:
        raise RuleViolation(f"{b.display} doesn't own {pb.name}.")
    _append_move(db, league, a.id, pa.id, gw_number, "drop", "trade")
    _append_move(db, league, b.id, pa.id, gw_number, "add", "trade")
    _append_move(db, league, b.id, pb.id, gw_number, "drop", "trade")
    _append_move(db, league, a.id, pb.id, gw_number, "add", "trade")
    record_audit(db, league, action="v2_trade",
                 summary=f"Trade: {a.display} {pa.name} ↔ {b.display} {pb.name} (GW{gw_number})",
                 manager_ids=[a.fpl_manager_id, b.fpl_manager_id],
                 details={"a": a.display, "a_player": pa.name,
                          "b": b.display, "b_player": pb.name, "gw": gw_number})
    db.commit()


def get_waivers_view(db: Session, league: League, fpl_manager_id: str) -> dict | None:
    """Everything the waivers page needs: the current waiver priority order, the
    window state, the manager's squad (drop options), the free-agent pool (add
    options), and the manager's pending claims. None if the manager isn't found."""
    manager = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not manager:
        return None
    gw = current_gameweek(db, league)
    if gw is None:
        latest = latest_gameweek(db, league)
        gw = latest.number if latest else 1
    window = waiver_window(db, league)
    order_map = get_waiver_order(db, league)
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    order = sorted(
        [{"name": names.get(mid), "priority": prio, "is_me": mid == manager.id}
         for mid, prio in order_map.items()],
        key=lambda r: r["priority"],
    )
    squads = _v2_all_squads(db, league)
    owned = set().union(*squads.values()) if squads else set()
    my_ids = squads.get(manager.id, set())
    players = db.query(Player).all()
    my_squad = sorted(
        [{"fpl_id": p.fpl_id, "name": p.name, "position": p.position}
         for p in players if p.id in my_ids],
        key=lambda d: (_POSITION_ORDER.get(d["position"], 9), d["name"]),
    )
    free_agents = sorted(
        [{"fpl_id": p.fpl_id, "label": f"{p.name} · {p.position} · {p.current_team}"}
         for p in players if p.id not in owned],
        key=lambda d: d["label"],
    )
    pname = {p.id: p.name for p in players}
    my_claims = [
        {"add": pname.get(c.add_player_id), "drop": pname.get(c.drop_player_id),
         "status": c.status, "reason": c.reason, "gw": c.gw_number}
        for c in db.query(V2WaiverClaim)
        .filter_by(league_id=league.id, manager_id=manager.id)
        .order_by(V2WaiverClaim.created_at.desc())
    ]
    return {
        "manager": manager.display, "fpl": manager.fpl_manager_id, "gameweek": gw,
        "window": window, "order": order, "my_squad": my_squad,
        "free_agents": free_agents, "my_claims": my_claims,
    }


def pending_waiver_claims(db: Session, league: League, gw_number: int) -> list:
    """Admin view: pending claims for a GW in current priority order, with names."""
    order = get_waiver_order(db, league)
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    pname = {p.id: p.name for p in db.query(Player)}
    claims = (
        db.query(V2WaiverClaim)
        .filter_by(league_id=league.id, gw_number=gw_number, status="pending")
        .all()
    )
    claims.sort(key=lambda c: order.get(c.manager_id, 10_000))
    return [
        {"manager": names.get(c.manager_id), "priority": order.get(c.manager_id),
         "add": pname.get(c.add_player_id), "drop": pname.get(c.drop_player_id)}
        for c in claims
    ]


# ---- v2 app-owned H2H schedule + standings (M5; see rules.season_schedule) ----

def _v2_managers_ordered(db: Session, league: League) -> list:
    """Manager ids in a stable order (for a deterministic generated schedule)."""
    return [
        m.id for m in db.query(Manager).filter_by(league_id=league.id)
        .order_by(Manager.display_name, Manager.name)
    ]


def generate_v2_schedule(db: Session, league: League, through_gw: int | None = None) -> int:
    """(Re)generate the app-owned H2H schedule as a double round-robin across GWs
    1..through_gw (default: the number of synced gameweeks, else 38). Idempotent —
    clears this league's fixtures first. Returns the number of fixtures written."""
    if through_gw is None:
        latest = latest_gameweek(db, league)
        through_gw = latest.number if latest else SEASON_LAST_GW
    ids = _v2_managers_ordered(db, league)
    db.query(V2Match).filter_by(league_id=league.id).delete()
    sched = season_schedule(ids, through_gw)
    written = 0
    for gw, pairs in sched.items():
        for home, away in pairs:
            db.add(V2Match(league_id=league.id, gw_number=gw,
                           home_manager_id=home, away_manager_id=away))
            written += 1
    db.commit()
    return written


def _v2_engine_totals(db: Session, league: League) -> dict:
    """{(manager_id, gw_number): total} from persisted engine scores. Populate via
    compute_v2_scores first."""
    out: dict = {}
    for score, gw in (
        db.query(V2GameweekScore, Gameweek)
        .join(Gameweek, Gameweek.id == V2GameweekScore.gameweek_id)
        .filter(Gameweek.league_id == league.id)
    ):
        out[(score.manager_id, gw.number)] = score.total
    return out


def get_v2_standings(db: Session, league: League) -> dict:
    """League standings computed entirely in-app: the generated H2H schedule
    (v2_matches) scored by the v2 engine (v2_gameweek_scores). A fixture counts once
    both managers have an engine score for that GW. Returns an ordered table plus how
    many fixtures were scored — no FPL standings involved."""
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    totals = _v2_engine_totals(db, league)
    tbl = {mid: {"w": 0, "d": 0, "l": 0, "pf": 0, "pa": 0, "played": 0} for mid in names}
    scored = 0
    for mt in db.query(V2Match).filter_by(league_id=league.id):
        ht = totals.get((mt.home_manager_id, mt.gw_number))
        at = totals.get((mt.away_manager_id, mt.gw_number))
        if ht is None or at is None:
            continue
        scored += 1
        for mid, gf, ga in ((mt.home_manager_id, ht, at), (mt.away_manager_id, at, ht)):
            row = tbl[mid]
            row["pf"] += gf
            row["pa"] += ga
            row["played"] += 1
            if gf > ga:
                row["w"] += 1
            elif gf < ga:
                row["l"] += 1
            else:
                row["d"] += 1
    table = sorted(
        [{"manager": names.get(mid), "points": 3 * r["w"] + r["d"], **r}
         for mid, r in tbl.items()],
        key=lambda r: (-r["points"], -r["pf"], r["manager"] or ""),
    )
    return {"scored_fixtures": scored, "table": table,
            "fixtures": db.query(V2Match).filter_by(league_id=league.id).count()}


def get_v2_schedule(db: Session, league: League, gw_number: int) -> list:
    """The app-owned fixtures for one GW, with engine scores if available."""
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    totals = _v2_engine_totals(db, league)
    out = []
    for mt in db.query(V2Match).filter_by(league_id=league.id, gw_number=gw_number):
        out.append({
            "home": names.get(mt.home_manager_id),
            "away": names.get(mt.away_manager_id),
            "home_score": totals.get((mt.home_manager_id, gw_number)),
            "away_score": totals.get((mt.away_manager_id, gw_number)),
        })
    return sorted(out, key=lambda r: (r["home"] or ""))


# ---- v2 cutover switch (M6): serve engine reads when APP_ENGINE=on ----

def app_engine_on() -> bool:
    """The v2 cutover flag. When APP_ENGINE=on, the public read paths (standings,
    scoreboard, My Team roster, transactions) are served from the app-owned engine
    instead of FPL-sourced tables. OFF by default — flipping it is a deliberate,
    reversible operational act; the FPL sync keeps running as the raw player/points/
    fixtures feed regardless."""
    import os

    return os.getenv("APP_ENGINE", "off").lower() == "on"


def _engine_standings(db: Session, league: League) -> list[dict]:
    """`get_standings` shape, computed from the generated schedule + engine scores
    (commissioner StandingAdjustment deltas still apply on top)."""
    from models import StandingAdjustment

    mgrs = {m.id: m for m in db.query(Manager).filter_by(league_id=league.id)}
    totals = _v2_engine_totals(db, league)
    tbl = {mid: {"w": 0, "d": 0, "l": 0, "pf": 0, "pa": 0} for mid in mgrs}
    for mt in db.query(V2Match).filter_by(league_id=league.id):
        ht = totals.get((mt.home_manager_id, mt.gw_number))
        at = totals.get((mt.away_manager_id, mt.gw_number))
        if ht is None or at is None:
            continue
        for mid, gf, ga in ((mt.home_manager_id, ht, at), (mt.away_manager_id, at, ht)):
            r = tbl[mid]
            r["pf"] += gf
            r["pa"] += ga
            r["w" if gf > ga else "l" if gf < ga else "d"] += 1
    dt, dpf = {}, {}
    for a in db.query(StandingAdjustment).filter_by(league_id=league.id):
        dt[a.manager_id] = dt.get(a.manager_id, 0) + a.total_delta
        dpf[a.manager_id] = dpf.get(a.manager_id, 0) + a.points_for_delta
    out = []
    for mid, m in mgrs.items():
        r = tbl[mid]
        out.append({
            "manager": m.display, "fpl": m.fpl_manager_id,
            "total": (3 * r["w"] + r["d"]) + dt.get(mid, 0),
            "points_for": r["pf"] + dpf.get(mid, 0), "points_against": r["pa"],
            "matches_won": r["w"], "matches_drawn": r["d"], "matches_lost": r["l"],
            "total_delta": dt.get(mid, 0), "points_for_delta": dpf.get(mid, 0),
            "adjusted": bool(dt.get(mid) or dpf.get(mid)),
        })
    out.sort(key=lambda x: (-(x["total"] or 0), -(x["points_for"] or 0), x["manager"]))
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


def _engine_scoreboard(db: Session, league: League, gw_number: int | None) -> dict:
    """`get_scoreboard` shape, from the generated schedule + engine scores."""
    gw_number = gw_number or current_gameweek(db, league)
    if gw_number is None:
        return {"gameweek": None, "matches": []}
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    totals = _v2_engine_totals(db, league)
    matches = []
    for mt in db.query(V2Match).filter_by(league_id=league.id, gw_number=gw_number):
        hs = totals.get((mt.home_manager_id, gw_number))
        as_ = totals.get((mt.away_manager_id, gw_number))
        matches.append({
            "home": names.get(mt.home_manager_id), "away": names.get(mt.away_manager_id),
            "home_score": hs, "away_score": as_,
            "finished": hs is not None and as_ is not None,
            "leader": (names.get(mt.home_manager_id) if (hs or 0) > (as_ or 0)
                       else names.get(mt.away_manager_id) if (as_ or 0) > (hs or 0) else None),
        })
    matches.sort(key=lambda x: (x["home"] or ""))
    return {"gameweek": gw_number, "matches": matches}


def _engine_transactions(db: Session, league: League) -> list[dict]:
    """`get_transactions` shape, from the app-owned ledger (excludes the initial seed)."""
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    pnames = {p.id: p.name for p in db.query(Player)}
    by_gw: dict = {}
    for mv in db.query(V2RosterMove).filter_by(league_id=league.id):
        if mv.source == "initial":
            continue
        by_gw.setdefault(mv.gw_number, []).append({
            "manager": names.get(mv.manager_id), "player": pnames.get(mv.player_id, "?"),
            "action": "added" if mv.action == "add" else "dropped",
        })
    return [
        {"gameweek": gw, "moves": sorted(by_gw[gw], key=lambda x: (x["manager"] or "", x["action"]))}
        for gw in sorted(by_gw, reverse=True)
    ]


# ---- v2 in-app scoring engine (dual-run; see scoring.py + the v2 roadmap) ----

def _v2_lineup_from_points(entries: list[dict], pos_by_fpl: dict) -> dict:
    """Reconstruct a manager's submitted lineup from a `gameweek_points.player_points`
    JSONB list. Each entry is `{fpl_id, position(1-15), is_starting, minutes, points}`
    — FPL position 1-11 = starters (in order), 12-15 = bench (in priority order).
    Returns `{starters, bench, players}` shaped for `scoring.score_lineup` (pids are
    fpl_ids; players carry pos/minutes/points). Entries whose player position is
    unknown are skipped so a legal formation can still be evaluated."""
    ordered = sorted(entries or [], key=lambda e: (e.get("position") or 99))
    starters, bench, players = [], [], {}
    for e in ordered:
        pid = e.get("fpl_id")
        pos = pos_by_fpl.get(pid)
        if pid is None or pos is None:
            continue
        players[pid] = {"pos": pos, "minutes": e.get("minutes") or 0,
                        "points": e.get("points") or 0}
        slot = e.get("position") or 99
        (starters if slot <= 11 else bench).append(pid)
    return {"starters": starters, "bench": bench, "players": players}


def _v2_score_gp(gp: GameweekPoints, pos_by_fpl: dict, lineup: dict | None = None) -> dict:
    """Run the pure engine over one synced `GameweekPoints` row → engine result
    (total + resolved XI). Uses the manager's app-submitted `lineup` (M2) when given
    and valid; otherwise falls back to FPL's submitted picks (positions 1-15), so the
    dual-run still works for GWs with no app lineup. Player points/minutes always come
    from the synced `player_points`; the lineup only re-designates who starts."""
    import scoring

    lu = _v2_lineup_from_points(gp.player_points or [], pos_by_fpl)
    players = lu["players"]
    if lineup:
        starters = [p for p in lineup.get("starters", []) if p in players]
        bench = [p for p in lineup.get("bench", []) if p in players]
        if len(starters) == scoring.XI_SIZE and len(bench) == scoring.SQUAD_SIZE - scoring.XI_SIZE:
            return scoring.score_lineup(starters, bench, players)
    return scoring.score_lineup(lu["starters"], lu["bench"], players)


def _v2_lineups_by_key(db: Session, league: League) -> dict:
    """Preload app lineups for the league keyed by (manager_id, gameweek_id) →
    {starters, bench} (fpl ids), so the engine can prefer them over FPL picks."""
    out: dict = {}
    for lp in (
        db.query(V2Lineup)
        .join(Gameweek, Gameweek.id == V2Lineup.gameweek_id)
        .filter(Gameweek.league_id == league.id)
        .all()
    ):
        out[(lp.manager_id, lp.gameweek_id)] = {
            "starters": list(lp.starters or []), "bench": list(lp.bench or [])
        }
    return out


def compute_v2_scores(db: Session, league: League, gw_number: int | None = None) -> int:
    """Compute + persist v2 engine scores for one GW (or the latest with data) into
    `v2_gameweek_scores`. Additive/idempotent (upsert by manager+GW); never touches
    the FPL-sourced `gameweek_points`. Returns the number of manager rows written."""
    q = (
        db.query(GameweekPoints, Gameweek)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(Gameweek.league_id == league.id)
    )
    if gw_number is not None:
        q = q.filter(Gameweek.number == gw_number)
    pos_by_fpl = {p.fpl_id: p.position for p in db.query(Player)}
    lineups = _v2_lineups_by_key(db, league)
    written = 0
    for gp, _gw in q.all():
        res = _v2_score_gp(gp, pos_by_fpl, lineups.get((gp.manager_id, gp.gameweek_id)))
        row = (
            db.query(V2GameweekScore)
            .filter_by(manager_id=gp.manager_id, gameweek_id=gp.gameweek_id)
            .one_or_none()
        )
        if row is None:
            row = V2GameweekScore(manager_id=gp.manager_id, gameweek_id=gp.gameweek_id)
            db.add(row)
        row.total = res["total"]
        row.breakdown = {"resolved_xi": res["resolved_xi"]}
        row.team_goals = res["team_goals"]
        row.team_assists = res["team_assists"]
        row.team_clean_sheets = res["team_clean_sheets"]
        written += 1
    db.commit()
    return written


def v2_score_diff(db: Session, league: League) -> dict:
    """Dual-run validation: compare the v2 engine against FPL's synced numbers.
    Computes engine totals on the fly (no dependency on a prior backfill) and diffs
    them against `gameweek_points.total_points` per manager/GW, and engine H2H
    winners against `matches.winner_id`. Returns summary counts + the mismatches."""
    import scoring

    pos_by_fpl = {p.fpl_id: p.position for p in db.query(Player)}
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    lineups = _v2_lineups_by_key(db, league)
    app_lineups_used = 0

    engine: dict = {}  # (manager_id, gameweek_id) -> engine total
    point_rows, point_mismatch = [], 0
    for gp, gw in (
        db.query(GameweekPoints, Gameweek)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(Gameweek.league_id == league.id)
        .all()
    ):
        lineup = lineups.get((gp.manager_id, gp.gameweek_id))
        if lineup:
            app_lineups_used += 1
        eng = _v2_score_gp(gp, pos_by_fpl, lineup)["total"]
        engine[(gp.manager_id, gp.gameweek_id)] = eng
        fpl = gp.total_points
        ok = (fpl is not None and eng == fpl)
        if not ok:
            point_mismatch += 1
            point_rows.append({
                "gw": gw.number, "manager": names.get(gp.manager_id),
                "engine": eng, "fpl": fpl,
                "delta": (eng - fpl) if fpl is not None else None,
            })

    winner_rows, winner_mismatch, winner_checked = [], 0, 0
    for mt in (
        db.query(Match)
        .filter_by(league_id=league.id, finished=True)
        .all()
    ):
        ht = engine.get((mt.home_manager_id, mt.gameweek_id))
        at = engine.get((mt.away_manager_id, mt.gameweek_id))
        if ht is None or at is None:
            continue
        winner_checked += 1
        outcome = scoring.h2h_result(ht, at)
        eng_winner = {"home": mt.home_manager_id, "away": mt.away_manager_id}.get(outcome)
        if eng_winner != mt.winner_id:
            winner_mismatch += 1
            gwn = db.get(Gameweek, mt.gameweek_id)
            winner_rows.append({
                "gw": gwn.number if gwn else None,
                "home": names.get(mt.home_manager_id),
                "away": names.get(mt.away_manager_id),
                "engine": names.get(eng_winner) if eng_winner else "draw",
                "fpl": names.get(mt.winner_id) if mt.winner_id else "draw",
            })

    return {
        "points_checked": len(engine),
        "points_mismatch": point_mismatch,
        "app_lineups_used": app_lineups_used,
        "point_rows": sorted(point_rows, key=lambda r: (r["gw"], r["manager"] or "")),
        "winners_checked": winner_checked,
        "winners_mismatch": winner_mismatch,
        "winner_rows": sorted(winner_rows, key=lambda r: (r["gw"] or 0)),
    }


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
    }


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
        if (p.position or "").upper() == "DEF":  # defenders added later stay eligible
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
    seeded = 0
    for ks in db.query(KeeperSelection).filter_by(
        league_id=old_league.id, season_year=new_league.season_year
    ):
        om = db.get(Manager, ks.manager_id)
        nm = new_mgrs.get(om.fpl_manager_id) if om else None
        if not nm:
            continue
        prior = (
            status.get(ks.manager_id, {}).get(ks.player_id, {}).get("years_remaining")
        )
        prior = prior if prior is not None else KEEPER_FRESH_REMAINING
        new_remaining = max(prior - 1, 0)
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
    if app_engine_on():
        return _engine_standings(db, league)
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
    out = []
    for m in managers:
        players = []
        if gw is not None:
            players = (
                db.query(PlayerSeason)
                .join(Roster, Roster.player_id == PlayerSeason.player_id)
                .filter(
                    Roster.manager_id == m.id,
                    Roster.gameweek_id == gw.id,
                    PlayerSeason.league_id == league.id,
                )
                .order_by(PlayerSeason.position, PlayerSeason.name)
                .all()
            )
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


def _squad_players(db: Session, league: League, manager_id, gw_id) -> list[PlayerSeason]:
    """`league` is positional and has no default on purpose: a missed caller must
    fail loudly rather than silently filter on league_id IS NULL."""
    if gw_id is None:
        return []
    return (
        db.query(PlayerSeason)
        .join(Roster, Roster.player_id == PlayerSeason.player_id)
        .filter(
            Roster.manager_id == manager_id,
            Roster.gameweek_id == gw_id,
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
    if app_engine_on():
        # Squad from the app-owned ledger (fold) rather than the FPL roster snapshot.
        # Yields PlayerSeason like the other branch: the shared code below reads
        # .player_id, which a Player row doesn't have.
        squad_ids = get_v2_squad(db, league, fpl_manager_id)
        players = (
            db.query(PlayerSeason)
            .filter(
                PlayerSeason.league_id == league.id,
                PlayerSeason.player_id.in_(squad_ids),
            )
            .order_by(PlayerSeason.position, PlayerSeason.name)
            .all()
            if squad_ids
            else []
        )
    else:
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
    counts = (
        _tanking_counts_by_manager(db, league).get(manager.id, {}).get("counts", {})
    )
    streak = current_tanking_streak(counts)
    flagged = bool(tanking_windows(counts))
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


def _tanking_counts_by_manager(db: Session, league: League) -> dict:
    """manager_id -> {"manager": Manager, "counts": {gw_number: zero_minute_count}}."""
    rows = (
        db.query(GameweekPoints, Gameweek, Manager)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .join(Manager, Manager.id == GameweekPoints.manager_id)
        .filter(Manager.league_id == league.id)
        .all()
    )
    per_manager: dict = {}
    for gp, gw, m in rows:
        entry = per_manager.setdefault(m.id, {"manager": m, "counts": {}})
        entry["counts"][gw.number] = zero_minute_count(gp.player_points or [])
    return per_manager


def get_flags(db: Session, league: League) -> list[dict]:
    """Anti-tanking flags across all synced gameweeks (precomputed read). Flags a
    manager when >=3 of their rostered players posted 0 minutes in each of >=3
    consecutive gameweeks. Each window carries `cleared` (commissioner-dismissed).
    """
    from models import TankingFlagClear

    cleared = {
        (c.manager_id, c.window)
        for c in db.query(TankingFlagClear).filter_by(league_id=league.id)
    }
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
    p = db.query(Player).filter_by(fpl_id=fpl_id).one_or_none()
    if not p:
        raise RuleViolation(f"player {fpl_id} not found")
    return p


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
    if app_engine_on():
        return _engine_transactions(db, league)
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    pnames = {pid: ps.name for pid, ps in season_identity(db, league).items()}
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


def player_portal(db: Session, league: League) -> list[dict]:
    """Every player with every stat + league context (owner, on-IL, ineligible, keeper
    acquisition/years/eligibility) for the admin data portal. One row per player."""
    gw = latest_gameweek(db, league)
    owner_by_pid: dict = {}
    if gw:
        for mid, pid in db.query(Roster.manager_id, Roster.player_id).filter_by(gameweek_id=gw.id):
            owner_by_pid[pid] = mid
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

    rows = []
    for p in db.query(Player).order_by(Player.name):
        owner_mid = owner_by_pid.get(p.id)
        ks = kstatus.get(owner_mid, {}).get(p.id) if owner_mid else None
        rows.append({
            "fpl_id": p.fpl_id, "name": p.name, "position": p.position,
            "team": p.current_team, "status": p.status, "news": p.news,
            "price": (p.price / 10) if p.price is not None else None,
            "last_season_points": p.last_season_points, "total_points": p.total_points,
            "form": _f(p.form), "points_per_game": _f(p.points_per_game),
            "goals_scored": p.goals_scored, "assists": p.assists,
            "clean_sheets": p.clean_sheets, "bonus": p.bonus, "minutes": p.minutes,
            "ict_index": _f(p.ict_index), "selected_by_percent": _f(p.selected_by_percent),
            "owner": names.get(owner_mid), "rostered": owner_mid is not None,
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

    # Anti-tanking: flagged (in violation) or at risk (one GW short of the threshold)
    for info in _tanking_counts_by_manager(db, league).values():
        counts = info["counts"]
        if tanking_windows(counts):
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

    Resolves recipient slots (league 1/2/3 + last from standings; cup 1/2/3 and
    pup champion from the brackets) and applies the configured payout structure.
    Pulls per-manager fines from the fines table (winner collects the pool); each
    manager's `net` is their payout minus the buy-in (overall winnings). Every
    manager is listed (those with no payout show net = -entry_fee - fines).
    """
    by_rank = sorted(
        db.query(Standing, Manager)
        .join(Manager, Manager.id == Standing.manager_id)
        .filter(Standing.league_id == league.id)
        .all(),
        key=lambda sm: sm[0].rank if sm[0].rank is not None else 999,
    )
    recipients: dict = {}
    if len(by_rank) >= 1:
        recipients["league_1"] = by_rank[0][1].id
    if len(by_rank) >= 2:
        recipients["league_2"] = by_rank[1][1].id
    if len(by_rank) >= 3:
        recipients["league_3"] = by_rank[2][1].id
    if by_rank:
        recipients["last_place"] = by_rank[-1][1].id

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


def _derive_keeper_status(db: Session, league: League) -> dict:
    """Core keeper derivation, shared by the report and selection validation.
    Returns {manager_id: {player_id: {player, position, acquisition,
    keeper_years, eligible}}} for players on each manager's final-GW roster."""
    gw = latest_gameweek(db, league)
    if gw is None:
        return {}
    last_n = gw.number
    # season-scoped identity: `players` is global and always holds the
    # latest season, so names/positions off it are wrong for a past one.
    players = season_identity(db, league)

    # Full per-GW roster presence so we can detect a DROP (a gap in a manager's
    # tenure of a player) vs continuous keeping.
    presence: dict = {}  # (manager_id, player_id) -> set of GW numbers rostered
    for mid, pid, gnum in (
        db.query(Roster.manager_id, Roster.player_id, Gameweek.number)
        .join(Gameweek, Gameweek.id == Roster.gameweek_id)
        .filter(Gameweek.league_id == league.id)
        .all()
    ):
        presence.setdefault((mid, pid), set()).add(gnum)

    from models import InternationalList

    # GW numbers a player was OFF the roster but covered (not a drop): the IL and the
    # international (AFCON / Asia Cup) list both preserve keeper eligibility.
    il: dict = {}  # (manager_id, player_id) -> covered GW numbers
    for e in (
        db.query(InjuryList)
        .join(Manager, Manager.id == InjuryList.manager_id)
        .filter(Manager.league_id == league.id)
        .all()
    ):
        il.setdefault((e.manager_id, e.player_id), set()).update(
            range(e.start_gw, (e.end_gw or last_n) + 1)
        )
    for e in (
        db.query(InternationalList)
        .join(Manager, Manager.id == InternationalList.manager_id)
        .filter(Manager.league_id == league.id)
        .all()
    ):
        il.setdefault((e.manager_id, e.player_id), set()).update(
            range(e.start_gw, (e.end_gw or last_n) + 1)
        )

    final_candidates = [k for k, gws in presence.items() if last_n in gws]
    traded_in = {
        (t.to_manager, t.player_id)
        for t in db.query(Trade).filter_by(league_id=league.id)
    }
    seed_remaining: dict = {}  # player_id -> imported years remaining
    for s in db.query(KeeperSeed).filter_by(league_id=league.id):
        seed_remaining[s.player_id] = s.years_remaining

    # submitted keepers for the upcoming season (so rosters can flag them locked)
    upcoming = (league.season_year or 0) + 1
    kept = {
        (s.manager_id, s.player_id): s.is_discovery
        for s in db.query(KeeperSelection).filter_by(league_id=league.id, season_year=upcoming)
    }

    def _dropped(mid, pid) -> bool:
        gws = presence[(mid, pid)]
        il_gws = il.get((mid, pid), set())
        first = min(gws)
        # a gap between first appearance and the final GW, not covered by the IL,
        # means the player was dropped (to FA) and later re-acquired
        return any(g not in gws and g not in il_gws for g in range(first, last_n + 1))

    status: dict = {}
    for mid, pid in final_candidates:
        acq, remaining = keeper_status(
            1 in presence[(mid, pid)],   # started_with_manager (on GW1 roster)
            (mid, pid) in traded_in,
            _dropped(mid, pid),
            seed_remaining.get(pid),
        )
        p = players.get(pid)
        status.setdefault(mid, {})[pid] = {
            "player": p.name if p else str(pid),
            "position": p.position if p else None,
            "acquisition": acq,
            "years_remaining": remaining,
            "eligible": keeper_eligible(remaining),
            "kept": (mid, pid) in kept,  # submitted keeper for next season
            "kept_discovery": kept.get((mid, pid), False),
        }
    return status


def get_keepers(db: Session, league: League) -> list[dict]:
    """Per-manager keeper eligibility for the upcoming selection, derived from
    roster continuity (drops reset the clock; IL and trades are explained moves),
    acquisition type, and Option-B seeds. Precomputed read; no FPL calls."""
    status = _derive_keeper_status(db, league)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.name).all()
    )
    out = []
    for m in managers:
        items = list(status.get(m.id, {}).values())
        items.sort(key=lambda x: (not x["eligible"], -x["years_remaining"], x["player"]))
        out.append({"manager": m.display, "manager_fpl": m.fpl_manager_id, "players": items})
    return out


def submit_keepers(
    db: Session,
    league: League,
    *,
    fpl_manager_id: str,
    keeper_fpl_ids: list[int],
    season_year: int,
    discovery_fpl_id: int | None = None,
) -> dict:
    """Validate and persist a manager's keeper selection for `season_year`.
    Enforces eligibility + caps (<=5, +1 discovery, <=2 waiver). Replaces any
    prior selection for that manager/season."""
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
                st = {"player": player.name, "eligible": True,
                      "acquisition": "discovery",
                      "years_remaining": KEEPER_FRESH_REMAINING}
            else:
                raise RuleViolation(
                    f"{player.name} is not on {manager.name}'s final roster"
                )
        selections.append({**st, "fpl_id": fid, "player_id": player.id,
                           "is_discovery": is_discovery})

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
                season_year=season_year,
                is_discovery=s["is_discovery"],
            )
        )
    record_audit(db, league, action="keeper.submit",
                 summary=(f"{manager.display} submitted {len(selections)} keeper(s) for "
                          f"{season_year}: " + ", ".join(s["player"] for s in selections)),
                 manager_ids=[manager.id],
                 details={"season_year": season_year,
                          "keeper_fpl_ids": all_fids, "discovery_fpl_id": discovery_fpl_id})
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


def get_keeper_selections(db: Session, league: League, season_year: int) -> list[dict]:
    """Submitted keeper selections for a season, grouped by manager.

    Identity resolves via the SUBMITTING league (`league`), not `season_year`:
    selections name the season being kept FOR, whose league row doesn't exist yet
    at submission time. So this shows the player as they were when picked, which
    is the only data available and the right context anyway.
    """
    rows = (
        db.query(KeeperSelection, Manager, PlayerSeason)
        .join(Manager, Manager.id == KeeperSelection.manager_id)
        .join(PlayerSeason, PlayerSeason.player_id == KeeperSelection.player_id)
        .filter(KeeperSelection.league_id == league.id,
                KeeperSelection.season_year == season_year,
                PlayerSeason.league_id == league.id)
        .all()
    )
    by_manager: dict = {}
    for sel, m, p in rows:
        by_manager.setdefault(m.display, []).append(
            {"player": p.name, "position": p.position, "is_discovery": sel.is_discovery}
        )
    return [{"manager": k, "keepers": v} for k, v in sorted(by_manager.items())]


# ---- drafts (board generation + commissioner-entered pick/player trades) ----
def _reverse_standings_managers(db: Session, league: League) -> list[Manager]:
    rows = (
        db.query(Standing, Manager)
        .join(Manager, Manager.id == Standing.manager_id)
        .filter(Standing.league_id == league.id)
        .all()
    )
    rows.sort(key=lambda sm: -(sm[0].rank or 0))  # worst (10th) first
    return [m for _, m in rows]


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
    of the (season, draft_type, round) slot originally belonging to original_fpl."""
    frm = _resolve_manager(db, league, from_fpl)
    to = _resolve_manager(db, league, to_fpl)
    orig = _resolve_manager(db, league, original_fpl)
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


def record_pick(
    db: Session, league: League, *, season_year: int, pick_number: int,
    owner_fpl: str, player_fpl_id: int, draft_type: str = "main", round: int = 0,
    overwrite: bool = False,
) -> dict:
    """Record a selection at a board slot (live). Upsert by slot. With concurrent
    devices, a slot that already has a player is NOT silently overwritten — raises
    RuleViolation (a clean "pick already made") unless `overwrite` (admin correction)."""
    owner = _resolve_manager(db, league, owner_fpl)
    player = _resolve_player(db, player_fpl_id)
    existing = (
        db.query(DraftPick)
        .filter_by(league_id=league.id, season_year=season_year, draft_type=draft_type, pick_number=pick_number)
        .one_or_none()
    )
    if existing:
        if existing.player_id is not None and not overwrite:
            raise RuleViolation(f"pick {pick_number} has already been made")
        existing.manager_id, existing.player_id = owner.id, player.id
    else:
        db.add(DraftPick(
            league_id=league.id, season_year=season_year, draft_type=draft_type,
            pick_number=pick_number, round=round, manager_id=owner.id,
            player_id=player.id, source="draft",
        ))
    record_audit(db, league, action="draft.pick",
                 summary=(f"{owner.display} drafted {player.name} "
                          f"({draft_type} #{pick_number})"
                          + (" [overwrite]" if overwrite else "")),
                 manager_ids=[owner.id],
                 details={"season_year": season_year, "pick_number": pick_number,
                          "draft_type": draft_type, "player_fpl_id": player_fpl_id,
                          "overwrite": overwrite})
    db.commit()
    return {"pick": pick_number, "owner": owner.name, "player": player.name}


def get_draft_queue(
    db: Session, league: League, fpl_manager_id: str, season_year: int, draft_type: str = "main"
) -> list[dict]:
    """A manager's ranked autodraft queue (player name/fpl_id in rank order)."""
    from models import DraftQueue

    manager = _resolve_manager(db, league, fpl_manager_id)
    rows = (
        db.query(DraftQueue, Player)
        .join(Player, Player.id == DraftQueue.player_id)
        .filter(
            DraftQueue.league_id == league.id, DraftQueue.season_year == season_year,
            DraftQueue.draft_type == draft_type, DraftQueue.manager_id == manager.id,
        )
        .order_by(DraftQueue.rank)
        .all()
    )
    return [{"fpl_id": p.fpl_id, "name": p.name, "position": p.position} for _q, p in rows]


def add_to_queue(
    db: Session, league: League, *, fpl_manager_id: str, player_fpl_id: int,
    season_year: int, draft_type: str = "main",
) -> None:
    """Append a player to the manager's queue (idempotent; no-op if already queued)."""
    from models import DraftQueue

    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id)
    exists = (
        db.query(DraftQueue).filter_by(
            league_id=league.id, season_year=season_year, draft_type=draft_type,
            manager_id=manager.id, player_id=player.id,
        ).one_or_none()
    )
    if exists:
        return
    next_rank = (
        db.query(func.coalesce(func.max(DraftQueue.rank), -1)).filter_by(
            league_id=league.id, season_year=season_year, draft_type=draft_type,
            manager_id=manager.id,
        ).scalar()
    ) + 1
    db.add(DraftQueue(
        league_id=league.id, season_year=season_year, draft_type=draft_type,
        manager_id=manager.id, player_id=player.id, rank=next_rank,
    ))
    db.commit()


def remove_from_queue(
    db: Session, league: League, *, fpl_manager_id: str, player_fpl_id: int,
    season_year: int, draft_type: str = "main",
) -> None:
    from models import DraftQueue

    manager = _resolve_manager(db, league, fpl_manager_id)
    player = _resolve_player(db, player_fpl_id)
    db.query(DraftQueue).filter_by(
        league_id=league.id, season_year=season_year, draft_type=draft_type,
        manager_id=manager.id, player_id=player.id,
    ).delete(synchronize_session=False)
    db.commit()


def approve_queued_pick(
    db: Session, league: League, *, season_year: int, draft_type: str = "main"
) -> dict:
    """Admin: fill the on-the-clock slot from its owner's queue — picks their top
    still-available, eligible queued player. Raises RuleViolation if the draft is
    complete or the on-the-clock manager has no usable queued player."""
    from models import DraftQueue

    board = (
        get_discovery_board(db, league, season_year)
        if draft_type == "discovery"
        else get_draft_board(db, league, season_year)
    )
    slot = next_open_pick(board)
    if not slot or not slot.get("owner_fpl"):
        raise RuleViolation("the draft is complete")
    owner = _resolve_manager(db, league, slot["owner_fpl"])
    queued = (
        db.query(DraftQueue, Player)
        .join(Player, Player.id == DraftQueue.player_id)
        .filter(
            DraftQueue.league_id == league.id, DraftQueue.season_year == season_year,
            DraftQueue.draft_type == draft_type, DraftQueue.manager_id == owner.id,
        )
        .order_by(DraftQueue.rank)
        .all()
    )
    if not queued:
        raise RuleViolation(f"{owner.display} has no queued picks")
    # exclude already-taken (kept/drafted) + ineligible players
    available = search_players(
        db, league, available_year=season_year, draft_type=draft_type,
        include_taken=False, limit=10_000,
    )
    available_ids = {r["fpl_id"] for r in available}
    for _q, p in queued:
        if p.fpl_id in available_ids:
            record_pick(
                db, league, season_year=season_year, pick_number=slot["pick"],
                owner_fpl=owner.fpl_manager_id, player_fpl_id=p.fpl_id,
                draft_type=draft_type, round=slot["round"],
            )
            remove_from_queue(
                db, league, fpl_manager_id=owner.fpl_manager_id, player_fpl_id=p.fpl_id,
                season_year=season_year, draft_type=draft_type,
            )
            return {"pick": slot["pick"], "owner": owner.display, "player": p.name}
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
    # then live pick trades, in entry order (latest wins)
    for t in (
        db.query(Trade)
        .filter(Trade.league_id == league.id, Trade.pick_round.isnot(None),
                Trade.pick_season_year == season_year, Trade.pick_draft_type == draft_type)
        .order_by(Trade.id)
        .all()
    ):
        orig, to = person_by_id.get(t.pick_original_manager), person_by_id.get(t.to_manager)
        if orig and to:
            reassigned[(t.pick_round, orig)] = to
    return reassigned


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

    keeper_counts: dict = {}
    for sel in db.query(KeeperSelection).filter_by(league_id=league.id, season_year=season_year):
        keeper_counts[sel.manager_id] = keeper_counts.get(sel.manager_id, 0) + 1

    slots = generate_draft_slots(
        [m.id for m in r1], [m.id for m in rev], keeper_counts, ROSTER_SIZE
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
    pnames = {pid: ps.name for pid, ps in season_identity(db, league).items()}
    out = []
    for b in board:
        dp = picks.get(b["pick"])
        out.append({
            "pick": b["pick"],
            "round": b["round"],
            "owner": names.get(b["owner_id"]),
            "owner_fpl": fpl_by_id.get(b["owner_id"]),
            "original_owner": names.get(b["original_owner_id"]),
            "traded": b["owner_id"] != b["original_owner_id"],
            "player": pnames.get(dp.player_id) if dp and dp.player_id else None,
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
    limit: int = 50,
) -> list[dict]:
    """Search the player pool. A name query searches ALL players (position is
    ignored when `q` is set); `position` alone filters by position. `available_year`
    marks already-kept/drafted players: by default they're excluded, but with
    `include_taken` they're returned flagged (`taken` + `taken_by`) so a search can
    surface "already drafted" instead of empty results. `sort` = 'price', 'points',
    or 'team' (else by name)."""
    query = db.query(Player)
    if q:
        query = query.filter(Player.name.ilike(f"%{q}%"))  # search all (ignore position)
    elif position:
        query = query.filter(Player.position == position.upper())

    if sort == "price":
        query = query.order_by(Player.price.desc().nulls_last(), Player.name)
    elif sort == "points":
        query = query.order_by(Player.last_season_points.desc().nulls_last(), Player.name)
    elif sort == "team":
        query = query.order_by(Player.current_team.asc().nulls_last(), Player.name)
    else:
        query = query.order_by(Player.name)
    players = query.all()

    inelig = _ineligible_fpl_ids(db, league)  # post-draft non-DEF additions
    taken: dict = {}  # player_id -> label of who has them ("kept" / "drafted: X")
    if available_year is not None:
        names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
        for pid, mid in db.query(KeeperSelection.player_id, KeeperSelection.manager_id).filter_by(
            league_id=league.id, season_year=available_year
        ):
            taken[pid] = f"kept: {names.get(mid, '?')}"
        for pid, mid in (
            db.query(DraftPick.player_id, DraftPick.manager_id)
            .filter_by(league_id=league.id, season_year=available_year, draft_type=draft_type)
            .filter(DraftPick.player_id.isnot(None))
        ):
            taken[pid] = f"drafted: {names.get(mid, '?')}"

    out = []
    for p in players:
        ineligible = p.fpl_id in inelig
        is_taken = (p.id in taken) or ineligible
        if is_taken and not include_taken:
            continue
        taken_by = "ineligible (post-draft)" if ineligible else taken.get(p.id)
        out.append({
            "fpl_id": p.fpl_id, "name": p.name, "position": p.position, "team": p.current_team,
            "price": (p.price / 10) if p.price is not None else None,
            "points": p.last_season_points,
            "taken": is_taken, "taken_by": taken_by, "ineligible": ineligible,
        })
    return out[:limit]


# ---- trades view + draft helpers ----
def get_trades(db: Session, league: League) -> list[dict]:
    """All trades for the league — synced player trades and commissioner-entered
    pick/player trades — newest-ish first (by GW then id)."""
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    pnames = {pid: ps.name for pid, ps in season_identity(db, league).items()}
    rows = db.query(Trade).filter_by(league_id=league.id).all()
    out = []
    for t in rows:
        if t.pick_round is not None:
            kind, what = "pick", t.draft_pick or f"R{t.pick_round} pick"
        else:
            kind, what = "player", pnames.get(t.player_id, "—")
        out.append({
            "kind": kind,
            "what": what,
            "from": names.get(t.from_manager),
            "to": names.get(t.to_manager),
            "gw": t.event_gw,
            "source": "FPL" if t.fpl_trade_id else "site",
        })
    out.sort(key=lambda x: (x["gw"] is None, x["gw"] or 0), reverse=True)
    return out


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
            {"pick": r.pick_number, "round": r.round, "manager": r.manager_name, "player": r.player_name}
        )
    return [{"year": y, "picks": rows} for y, rows in by_season.items()]


# ---- general trade entry (manager-usable, players + picks, no cap) ----
def manager_assets(db: Session, league: League, fpl_manager_id: str) -> dict:
    """A manager's tradeable assets: current-roster players + future picks they
    own (their own un-traded picks + picks acquired), across the next few years."""
    m = _resolve_manager(db, league, fpl_manager_id)
    person = m.display
    persons = [mm.display for mm in db.query(Manager).filter_by(league_id=league.id)]

    players = []
    gw = latest_gameweek(db, league)
    if gw is not None:
        for p in (
            db.query(PlayerSeason)
            .join(Roster, Roster.player_id == PlayerSeason.player_id)
            .filter(
                Roster.manager_id == m.id,
                Roster.gameweek_id == gw.id,
                PlayerSeason.league_id == league.id,
            )
            .order_by(PlayerSeason.position, PlayerSeason.name)
        ):
            players.append({"fpl_id": p.fpl_id, "name": p.name, "position": p.position})

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
    return {"manager": person, "fpl": m.fpl_manager_id, "players": players, "picks": picks}


def record_trade(
    db: Session, league: League, *, a_fpl: str, b_fpl: str,
    a_players: list, a_picks: list, b_players: list, b_picks: list,
) -> dict:
    """Record a trade between two managers: any players + picks each way, no cap.
    Each asset becomes a Trade row; pick assets reassign ownership via the shared
    pick_ownership computation. Not admin-gated."""
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
    db.commit()
    moved = len(a_players) + len(b_players) + len(a_picks) + len(b_picks)
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
    add(f"15-man rosters (GW{gw.number if gw else '?'})", not bad_rosters,
        ", ".join(bad_rosters) if bad_rosters else "all 15")

    # players on the latest roster with no keeper seed (they default to fresh)
    seeded = {pid for (pid,) in db.query(KeeperSeed.player_id).filter_by(league_id=league.id)}
    on_roster = (
        {pid for (pid,) in db.query(Roster.player_id).filter_by(gameweek_id=gw.id)}
        if gw is not None else set()
    )
    unseeded = on_roster - seeded
    add("rostered players have a keeper seed", not unseeded,
        f"{len(unseeded)} without a seed" if unseeded else "ok")

    # pick trades must name an original owner
    bad_picks = (
        db.query(Trade)
        .filter(Trade.league_id == league.id, Trade.pick_round.isnot(None),
                Trade.pick_original_manager.is_(None))
        .count()
    )
    add("pick trades have an original owner", bad_picks == 0,
        f"{bad_picks} malformed" if bad_picks else "ok")

    return checks


# ---- keeper selection UI support ----
def keeper_candidates(db: Session, league: League, fpl_manager_id: str) -> dict:
    """A manager's roster players with keeper eligibility (for the selection UI):
    fpl_id, name, position, acquisition, years_remaining, eligible."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    status = _derive_keeper_status(db, league).get(manager.id, {})
    fpl_by_id = {p.id: p.fpl_id for p in db.query(Player)}
    items = [{**v, "fpl_id": fpl_by_id.get(pid)} for pid, v in status.items()]
    items.sort(key=lambda x: (not x["eligible"], -x["years_remaining"], x["player"]))
    # current submitted selection (upcoming season) so the form can preselect
    upcoming = (league.season_year or 0) + 1
    selected = {
        s.player_id: s.is_discovery
        for s in db.query(KeeperSelection).filter_by(
            league_id=league.id, manager_id=manager.id, season_year=upcoming
        )
    }
    sel_fpl = {fpl_by_id.get(pid): disc for pid, disc in selected.items()}
    for it in items:
        it["selected"] = it["fpl_id"] in sel_fpl
        it["is_discovery"] = sel_fpl.get(it["fpl_id"], False)
    # the saved discovery keeper may be off-roster (it can be any player), so
    # surface it independently for the search UI to pre-fill
    discovery = None
    disc_pid = next((pid for pid, d in selected.items() if d), None)
    if disc_pid is not None:
        p = db.get(Player, disc_pid)
        if p:
            discovery = {"fpl_id": p.fpl_id, "player": p.name}
    return {"manager": manager.display, "fpl": manager.fpl_manager_id,
            "season": upcoming, "players": items, "discovery": discovery}


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
