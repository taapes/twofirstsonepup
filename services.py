"""Read-only query helpers serving PRECOMPUTED data from our tables.

Per the architecture (CLAUDE.md), these never call the FPL API — they read the
synced/normalized rows. Shared by the JSON API (api.py) and the homepage
(main.py) so both render the same data.
"""

import difflib
import os
import re
import unicodedata
import uuid

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
    CONDITION_MET,
    CONDITION_NOT_MET,
    CONDITION_PENDING,
    CONDITION_PLAYER_METRICS,
    CONDITION_MANUAL,
    CONDITION_LOGIC_ALL,
    CONDITION_LOGIC_ANY,
    CONDITION_EFFECT_ESCALATE,
    CONDITION_EFFECT_TRANSFER,
    CONDITION_THRESHOLD_METRICS,
    CUP_SEED_THROUGH_GW,
    CUP_SIZE,
    CUP_START_GW,
    DISCOVERY_OPEN_DAY,
    DISCOVERY_OPEN_MONTH,
    KEEPER_FRESH_DRAFT,
    KEEPER_FRESH_WAIVER,
    MIN_IL_STAY_GWS,
    ROSTER_SIZE,
    PAYOUT_STRUCTURE,
    PHASE_IN_SEASON,
    PHASE_OFFSEASON,
    PHASE_PRESEASON,
    RuleViolation,
    SEASON_LAST_GW,
    TRADE_DEADLINE_DAY,
    TRADE_DEADLINE_MONTH,
    LIVE_FIXTURE_WINDOW_HOURS,
    combine_condition_states,
    compare_condition,
    compute_payouts,
    current_tanking_streak,
    decide_sync,
    OUTFIELD_POSITION_LIMITS,
    SQUAD_POSITION_LIMITS,
    fixture_status,
    project_auto_subs,
    squad_quota_reason,
    keepers_revealed as _keepers_revealed_rule,
    validate_condition_term,
    validate_pick_condition,
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
    """The gameweek every ownership/roster/keeper reader treats as "the final one in
    view" -- capped at the date-derived CURRENT gameweek (see
    _derived_current_gameweek) when that's lower than the highest existing row.

    Regression for 2026-08-21, the 26/27 season's actual GW1: sync_gameweek_dates
    pulls the WHOLE season's calendar from FPL's bootstrap-static feed and creates
    all 38 Gameweek rows up front (needed for the waiver window and the "next 3
    fixtures" display), so the naive "highest existing row" can name a gameweek
    whose window hasn't even started -- with zero synced Roster rows. Every reader
    of _owner_maps (get_rosters /my-team, player_portal, manager_assets, the draft
    slot math, the keepers page) silently read an EMPTY snapshot at that phantom
    gameweek: every squad, everywhere, showed blank. This held in every prior
    season only because Gameweek rows were created ONE AT A TIME as each week's
    sync ran, so "highest existing row" and "highest row with real data" were the
    same number by construction; bulk-creating the calendar broke that coincidence
    without anyone changing this function.

    Uses _derived_current_gameweek, NOT the public current_gameweek() wrapper: the
    demo sandbox's override (current_gameweek) would otherwise leak into every
    ownership read in demo mode, for a feature (a mid-season Upcoming/Scores view
    over a copied FINISHED season) that has nothing to do with this.

    Falls through to the naive max when no date-derived value exists -- e.g. every
    test fixture in this codebase, which creates a full 1..38 Gameweek scaffold with
    no start_date at all, and relies on "the final GW in view" meaning 38 for
    season-end keeper/ownership derivation. That contract is unchanged here.
    """
    naive = (
        db.query(Gameweek)
        .filter_by(league_id=league.id)
        .order_by(Gameweek.number.desc())
        .first()
    )
    if naive is None:
        return None
    cur_number = _derived_current_gameweek(db, league)
    if cur_number is not None and cur_number < naive.number:
        return (
            db.query(Gameweek)
            .filter_by(league_id=league.id, number=cur_number)
            .one_or_none()
            or naive
        )
    return naive


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


# ---- matchup analysis --------------------------------------------------------
# A plain-English line per H2H tie: what does the trailing manager still need?
#
# DETERMINISTIC, and the arithmetic lives here rather than in a template or a model.
# When the pundit layer arrives (backlog Item 19) it dresses up facts it was handed —
# a number in this sentence must never be something an LLM inferred.


def _name_list(names, cap=3) -> str:
    """"A, B and C" — capped, because a manager can have eleven players left."""
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) > cap:
        return f"{', '.join(names[:cap])} +{len(names) - cap} more"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _pts(n: int) -> str:
    return f"{n} pt" if n == 1 else f"{n} pts"


def _cover_clause(side) -> str:
    """"— Hall covers Sesko from the bench (11 pts)" / "(vs SPU, Mon 19:00)".

    Cover is looked up BY the at-risk player, because who replaces him depends on his
    position — naming "the next bench player" would name the backup keeper nearly every
    time, and he can only ever replace the keeper.

    Names the cover's banked points if he has played, otherwise the fixture he still
    has, which is the more useful fact: it says when the uncertainty resolves.
    """
    ids = [p.get("fpl_id") for p in side.get("remaining_players") or []]
    cover_map = side.get("cover") or {}
    for fid in ids:
        cover = cover_map.get(fid)
        if not cover or not cover.get("name"):
            continue
        who = next(
            (p["name"] for p in side["remaining_players"] if p.get("fpl_id") == fid),
            None,
        )
        if cover.get("played"):
            detail = _pts(cover["points"])
        elif cover.get("opponent"):
            ko = cover.get("kickoff_time")
            detail = f"vs {cover['opponent']}" + (f", {ko:%a %H:%M}" if ko else "")
        else:
            detail = "yet to play"
        return f" — {cover['name']} covers {who} from the bench ({detail})"
    return ""


def matchup_analysis(match: dict) -> str:
    """One sentence describing what the trailing manager needs.

    Four states, and the branching does NOT explode with auto-subs in play: what is
    uncertain is WHICH player fills a slot, not how many slots there are — a manager
    always ends on eleven. So the contingency is one clause naming the next bench
    player, never a tree of outcomes.

    Points are treated as monotonic, which is what makes "settled" decidable: if the
    trailing manager has nobody left, the leader can only gain, so the result is already
    known. (A stat correction can technically reduce a score; that is rare enough not to
    warrant hedging every sentence.)
    """
    home, away = match.get("home"), match.get("away")
    hs, as_ = match.get("home_score"), match.get("away_score")
    if hs is None or as_ is None:
        return ""
    def _side(name, key):
        rem = match.get(f"{key}_remaining") or {}
        players = rem.get("remaining_players") or []
        return {
            "name": name,
            "remaining": rem.get("remaining") or 0,
            "players": [p["name"] for p in players],
            "remaining_players": players,
            "cover": match.get(f"{key}_cover") or {},
        }

    h_side, a_side = _side(home, "home"), _side(away, "away")

    if hs == as_:
        if not h_side["remaining"] and not a_side["remaining"]:
            return f"{hs}–{as_} — a draw."
        parts = []
        for side in (h_side, a_side):
            parts.append(
                f"{side['name']} has {_name_list(side['players']) or 'nobody'} left"
                if side["remaining"] else f"{side['name']} has nobody left"
            )
        return f"Level at {hs} — {parts[0]}, {parts[1]}."

    leader, trailer = (h_side, a_side) if hs > as_ else (a_side, h_side)
    lead = abs(hs - as_)

    # Points only go up, so a trailing manager with nobody left cannot catch up.
    if not trailer["remaining"]:
        return f"{leader['name']} wins {max(hs, as_)}–{min(hs, as_)}."

    names = _name_list(trailer["players"])
    tail = _cover_clause(trailer)
    if not leader["remaining"]:
        return (f"{trailer['name']} needs {lead + 1} from {names} to win, "
                f"{lead} to draw.{tail}")
    return (f"{trailer['name']} needs {names} to outscore "
            f"{_name_list(leader['players'])} by {lead + 1} to win, "
            f"{lead} to draw.{tail}")


def get_scoreboard(db: Session, league: League, gw_number: int | None = None) -> dict:
    """Current-GW H2H scoreboard: each matchup with live scores (from gameweek_points,
    falling back to the match's stored points) and whether it's finished.

    `finished` is `Match.finished` — H2H scoring-lock, sourced verbatim from FPL's
    Draft API for that pairing. It is NOT a real-match concept and must never be
    labelled "live"/"final" in a template; that word belongs to `fixtures`/
    `home_remaining`/`away_remaining` below, which are about actual PL matches.
    """
    gw_number = gw_number or current_gameweek(db, league)
    if gw_number is None:
        return {"gameweek": None, "matches": [], "fixtures": None, "synced_at": None}
    gw = (
        db.query(Gameweek).filter_by(league_id=league.id, number=gw_number).one_or_none()
    )
    if not gw:
        return {"gameweek": gw_number, "matches": [], "fixtures": None, "synced_at": None}
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    # PROJECTED, not FPL's raw live total. FPL applies bench substitutions only when a
    # gameweek is finalised, so its mid-week number shows a manager carrying a hole it
    # will later fill — measured on 2026-08-30, four of ten managers were understated,
    # one by six points. Once a gameweek IS finalised the two agree exactly, which is
    # the invariant the tests key on.
    projected = projected_points_by_manager(db, league, gw_number)
    live = {mid: p["points"] for mid, p in projected.items()}
    remaining = players_remaining_by_manager(db, league, gw_number)
    matches = []
    for mt in db.query(Match).filter_by(league_id=league.id, gameweek_id=gw.id):
        hs = live.get(mt.home_manager_id, mt.home_points)
        as_ = live.get(mt.away_manager_id, mt.away_points)
        home_r = remaining.get(mt.home_manager_id)
        away_r = remaining.get(mt.away_manager_id)
        matches.append({
            "home": names.get(mt.home_manager_id),
            "away": names.get(mt.away_manager_id),
            "home_score": hs, "away_score": as_,
            "finished": bool(mt.finished),
            "leader": (names.get(mt.home_manager_id) if (hs or 0) > (as_ or 0)
                       else names.get(mt.away_manager_id) if (as_ or 0) > (hs or 0) else None),
            "home_remaining": home_r,
            "away_remaining": away_r,
            # Display only, and present even when empty so a template can iterate
            # without guarding. Each entry carries the outgoing and incoming player.
            "home_subs": (projected.get(mt.home_manager_id) or {}).get("subs") or [],
            "away_subs": (projected.get(mt.away_manager_id) or {}).get("subs") or [],
            "home_cover": (projected.get(mt.home_manager_id) or {}).get("cover") or [],
            "away_cover": (projected.get(mt.away_manager_id) or {}).get("cover") or [],
            "home_playing_now": (home_r or {}).get("playing_now", []),
            "away_playing_now": (away_r or {}).get("playing_now", []),
        })
    matches.sort(key=lambda x: (x["home"] or ""))
    # "Closest match": the smallest score margin among matches still in progress —
    # a match FPL has already finalized isn't a "live impact" fact worth flagging.
    live_margins = [
        abs(m["home_score"] - m["away_score"])
        for m in matches
        if not m["finished"] and m["home_score"] is not None and m["away_score"] is not None
    ]
    closest_margin = min(live_margins) if live_margins else None
    for m in matches:
        m["analysis"] = matchup_analysis(m)
        m["closest"] = (
            closest_margin is not None
            and not m["finished"]
            and m["home_score"] is not None and m["away_score"] is not None
            and abs(m["home_score"] - m["away_score"]) == closest_margin
        )
    return {
        "gameweek": gw_number,
        "matches": matches,
        "fixtures": gw_fixture_progress(db, league, gw_number),
        "synced_at": scoreboard_freshness(db),
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


def suggest_manager_pairing(db: Session, old_league: League, new_league: League) -> dict:
    """{new_manager_id: old_manager_id or None} — a BEST GUESS from the team names.

    FPL reissues `entry_id` between seasons (see the module note on advance_season),
    and team names change too, so nothing pairs the two rows automatically. This
    narrows the commissioner's job rather than doing it: on the real 25/26 -> 26/27
    data it resolves six of ten ("Fighting Franckes", "Pep's Scraps", "Sid Hefty
    +III" -> "Sid Hefty", the emoji run, "Booyaka", "João") and leaves four blank.

    Greedy over the best-scoring pairs so one strong match can't be stolen by a
    weaker one elsewhere. Reuses the discovery matcher's normalisation
    (`_match_norm` / `_match_tokens`) rather than adding a third one.

    NEVER applied automatically — same rule as the discovery pick matcher, for the
    same reason: a wrong pairing hands one manager another's whole season.
    """
    old_mgrs = db.query(Manager).filter_by(league_id=old_league.id).all()
    new_mgrs = db.query(Manager).filter_by(league_id=new_league.id).all()

    def _raw(s):
        # Emoji and punctuation survive here but not `_match_norm`, which keeps only
        # a-z. A team called "🐶☕️🤴" normalises to the empty string, so the
        # alphabetic path can't see it at all — yet it is literally a substring of
        # next season's "Culver City HS🐶☕️🤴", which is about as strong a signal as
        # this function ever gets.
        return "".join((s or "").split()).casefold()

    scored = []
    for nm in new_mgrs:
        for om in old_mgrs:
            a, b = _match_norm(nm.name), _match_norm(om.name)
            ra, rb = _raw(nm.name), _raw(om.name)
            score = 0.0

            if ra and rb and (ra in rb or rb in ra) and min(len(ra), len(rb)) >= 2:
                score = 1.2
            else:
                # Tokens of 1-2 characters are dropped: "de", "le", "fc", "the" and a
                # possessive "s" are shared by unrelated names and were enough on
                # their own to match "Le Roi De Coupe" to "Smashers de Puppies".
                ta = {t for t in _match_tokens(nm.name) if len(t) > 2}
                tb = {t for t in _match_tokens(om.name) if len(t) > 2}
                shared = ta & tb
                if shared:
                    score = len(shared) / max(len(ta | tb), 1) + 0.5
                elif a and b:
                    # Pure string similarity, with NO shared word, is weak evidence
                    # and needs a high bar. Measured on this year's real renames, the
                    # four genuinely unmatchable pairs score 0.20-0.48 (the worst
                    # being "Smashers de Puppies" vs "Le Roi De Coupe" at 0.483),
                    # while both true positives also share a substantive token and so
                    # are already caught above. So this only fires on a near-identical
                    # string — a typo'd rename — and otherwise leaves the row blank.
                    ratio = difflib.SequenceMatcher(None, a, b).ratio()
                    score = ratio if ratio >= 0.75 else 0.0
            if score >= 0.45:
                scored.append((score, nm.id, om.id))

    scored.sort(key=lambda t: -t[0])
    out = {nm.id: None for nm in new_mgrs}
    taken = set()
    for _score, new_id, old_id in scored:
        if out[new_id] is None and old_id not in taken:
            out[new_id] = old_id
            taken.add(old_id)
    return out


def advance_season(
    db: Session, old_league: League, new_league: League, *,
    pairing: dict | None = None, force: bool = False,
) -> dict:
    """Roll the league over to a new season (Preseason). The new league row must
    already be synced (the route runs sync for the new FPL id first). Carries forward:
      1. identity — display_name + password_hash + discord_user_id (fills blanks on the
         new rows),
      2. keeper state — for players kept for the new season, a KeeperSeed on the new
         league with years_remaining decremented by 1 (so the clock ticks),
      3. the draft-day player-pool snapshot for the new league,
    then flips `is_current` to the new league and sets it to the preseason phase.
    Idempotent: safe to re-run.

    `pairing` is {new_manager_id: old_manager_id}, supplied by the commissioner via
    the rollover mapping page. **This used to be derived here from
    `managers.fpl_manager_id`, and that is not a stable identity.** At the 26/27
    rollover FPL reissued every entry id (25/26: 5520-268927; 26/27: a contiguous
    58528-58537 block, overlap ZERO), so all three carries matched nothing, and each
    one `continue`d on the miss: ten NULL display names, ten NULL password hashes —
    every login broken — and zero keeper seeds against 152 on the old row. The
    function returned `managers_carried=0, keepers_seeded=0` and audited it; nothing
    failed and nobody looked.

    So an incomplete pairing is now a `RuleViolation` naming every unpaired manager
    on both sides. `force=True` is the escape hatch for a season where the roster
    genuinely changed — surfaced in the UI as a checkbox next to those names, never
    a silent default.

    `pairing=None` falls back to entry-id matching, which keeps every existing
    programmatic caller working — but it is now VALIDATED rather than trusted, so the
    silent-no-op path no longer exists.
    """
    from models import PlayerPoolSnapshot

    if old_league.id == new_league.id:
        raise RuleViolation("new season must be a different league")

    old_rows = db.query(Manager).filter_by(league_id=old_league.id).all()
    new_rows = db.query(Manager).filter_by(league_id=new_league.id).all()
    if pairing is None:
        by_entry = {m.fpl_manager_id: m.id for m in old_rows}
        pairing = {nm.id: by_entry.get(nm.fpl_manager_id) for nm in new_rows}

    old_by_id = {m.id: m for m in old_rows}
    paired_old = {oid for oid in pairing.values() if oid is not None}
    unpaired_new = [nm.display for nm in new_rows if not pairing.get(nm.id)]
    unpaired_old = [om.display for om in old_rows if om.id not in paired_old]
    if (unpaired_new or unpaired_old) and not force:
        raise RuleViolation(
            "manager pairing is incomplete, so identity and keeper clocks would be "
            "silently dropped. "
            + (f"No counterpart for new: {', '.join(sorted(unpaired_new))}. "
               if unpaired_new else "")
            + (f"Unmatched from last season: {', '.join(sorted(unpaired_old))}. "
               if unpaired_old else "")
            + "Confirm the mapping, or tick 'a manager joined or left' to proceed."
        )

    # The absence overlay is NOT self-retiring, unlike the trade overlay: nothing closes
    # an entry when the player leaves the PL, is claimed by someone else, or the manager
    # simply never clicks return. Roll over with one open and it sits on the frozen row
    # asserting ownership forever, and the manager's last keeper selection was taken with
    # sixteen candidates. Same fail-loudly shape as the pairing check above, same hatch.
    open_absences = unresolved_absences(db, old_league)
    if open_absences and not force:
        who = ", ".join(sorted(
            f"{(db.get(Manager, e.manager_id).display if db.get(Manager, e.manager_id) else '—')}"
            f" ({db.get(Player, e.player_id).name if db.get(Player, e.player_id) else '—'})"
            for e in open_absences
        ))
        raise RuleViolation(
            "these managers still have someone on an absence list, so their squads "
            f"aren't back to {ROSTER_SIZE} and their keeper selections were made with "
            f"an extra candidate: {who}. Have them return or release on My Team, or "
            "tick the override to proceed."
        )

    # 1. identity carry (only fill blanks, so re-running can't clobber)
    carried = 0
    for nm in new_rows:
        om = old_by_id.get(pairing.get(nm.id))
        if not om:
            continue
        if om.display_name and not nm.display_name:
            nm.display_name = om.display_name
        if om.password_hash and not nm.password_hash:
            nm.password_hash = om.password_hash
        # Same family as the two above, and the same reason: `managers` holds one row
        # per manager per season, so an identity the LEAGUE owns has to be carried or
        # it is lost at every rollover. Without this the Discord author->manager map
        # silently empties and every inbound proposal quietly loses its one certain
        # identity, with nothing reporting it — the silent-inert pattern.
        if om.discord_user_id and not nm.discord_user_id:
            nm.discord_user_id = om.discord_user_id
        carried += 1
    # The keeper carry below walks the OLD row's selections, so it needs the reverse
    # direction to find each seller's new row.
    new_mgrs = {
        om.fpl_manager_id: db.get(Manager, nid)
        for nid, oid in pairing.items()
        if oid is not None and (om := old_by_id.get(oid)) is not None
    }

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
                          f"{carried} identities, seeded {seeded} keepers"
                          + (f" [FORCED, unpaired: "
                             f"{', '.join(sorted(unpaired_new + unpaired_old))}]"
                             if (unpaired_new or unpaired_old) else "")),
                 details={"new_season": new_league.season_year, "managers_carried": carried,
                          "keepers_seeded": seeded, "pool_snapshot": snapped,
                          "forced": bool(unpaired_new or unpaired_old),
                          "unpaired_new": unpaired_new, "unpaired_old": unpaired_old})
    db.commit()
    return {
        "new_season": new_league.season_year,
        "managers_carried": carried,
        "keepers_seeded": seeded,
        "pool_snapshot": snapped,
        # Returned so the route can SHOW them. The old version returned counts too;
        # the information was never the problem, surfacing it was.
        "unpaired_new": unpaired_new,
        "unpaired_old": unpaired_old,
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

    PRE-EXISTING BUG, found 2026-08-20 by the rollover rehearsal: this function has
    never imported `PlayerPoolSnapshot`. The name is imported function-locally in
    `flag_ineligible` and `advance_season`, neither of which is in scope here, so
    every call raised NameError. Its only caller is the last statement of the
    rollover route, so the failure looked like a 500 at the very end of a rollover
    that had otherwise succeeded — and the consequence was silent: no pool was ever
    captured, and `flag_ineligible` returns 0 on an empty snapshot by design, so the
    ineligible-player rule has never fired for any season.
    """
    from models import PlayerPoolSnapshot

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


def _rich_player_rows(
    db: Session, league: League, manager: "Manager", players: list
) -> list[dict]:
    """The shared rendering core of get_my_team / get_my_team_in_progress: a
    resolved list of Player-or-PlayerSeason rows (duck-typed, same as
    _player_stat_dict) -> rich per-player dicts with stats, a recent-points trend,
    and the upcoming-season keeper badge.

    Takes already-resolved ROWS, not player ids: get_my_team's roster-based lookup
    and get_my_team_in_progress's keeper+draft-pick lookup resolve ids to rows
    differently (the former via PlayerSeason only, the latter falling back to the
    global Player row for anyone with no snapshot yet), and that difference has to
    happen before this shared part.
    """
    if not players:
        return []
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

    # Post-draft additions: keyed on THIS season's element id, so compare against the
    # row's season fpl_id, not the global Player.fpl_id (which means whoever holds that
    # id now). A global Player row reached the fallback path with no snapshot, and a
    # player with no PlayerSeason row can't have been flagged anyway.
    inelig = _ineligible_fpl_ids(db, league)

    out_players = []
    for p in players:
        d = _player_stat_dict(p)
        # p may be a PlayerSeason row (.id is the snapshot's own PK, NOT
        # players.id — its player_id is) or a global Player row (.id IS
        # players.id, for anyone with no season snapshot yet).
        pid = p.player_id if isinstance(p, PlayerSeason) else p.id
        d["trend"] = trend.get(pid, [])
        d["is_keeper"] = pid in keeper_pids
        d["ineligible"] = isinstance(p, PlayerSeason) and p.fpl_id in inelig
        out_players.append(d)
    out_players.sort(key=lambda d: (_POSITION_ORDER.get(d["position"], 9), d["name"]))
    return out_players


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
    # The same projection the scoreboard shows, so a manager checking their own page
    # and the H2H table see one number rather than two. Absent when there is no points
    # data for the gameweek yet, which the template treats as "nothing to say".
    cur = current_gameweek(db, league)
    projected = (
        (projected_points_by_manager(db, league, cur) or {}).get(manager.id)
        if cur else None
    )
    return {
        "manager": manager.display,
        "fpl": manager.fpl_manager_id,
        "gameweek": gw.number if gw else None,
        "players": _rich_player_rows(db, league, manager, players),
        "status": _manager_status(db, league, manager),
        "projected": projected,
        "projected_gameweek": cur,
    }


def get_my_team_in_progress(
    db: Session, league: League, fpl_manager_id: str
) -> dict | None:
    """Like get_my_team, but for the draft/preseason window before FPL rosters
    exist: shows kept players UNION draft picks so far, instead of last season's
    finished roster. None if the manager isn't found — matching get_my_team.

    Reuses get_teams_in_progress' cross-row resolution for WHICH players this
    manager holds, then get_my_team's rich per-player rendering for HOW to show
    them. Clubs (goalie teams) are out of scope, matching get_my_team today, which
    has no club concept on the My Team page at all.
    """
    manager = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not manager:
        return None

    draft_year, sel_league, selections, picks, manager_for = _in_progress_bridge(
        db, league
    )
    pids: set = set()
    for s in selections:
        if s.player_id is None:
            continue  # a kept club — no club concept on My Team, see docstring
        target = manager_for(s.manager_id)
        if target and target[0] == manager.id:
            pids.add(s.player_id)
    for p in picks:
        if p.player_id is None:
            continue  # a drafted club, or (shouldn't happen) neither set
        target = manager_for(p.manager_id)
        if target and target[0] == manager.id:
            pids.add(p.player_id)

    ps_map = season_identity(db, league, pids) if pids else {}
    rows = list(ps_map.values())
    missing = pids - set(ps_map)
    if missing:
        rows += db.query(Player).filter(Player.id.in_(missing)).all()

    return {
        "manager": manager.display,
        "fpl": manager.fpl_manager_id,
        "gameweek": None,
        "players": _rich_player_rows(db, league, manager, rows),
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


def gw_fixture_progress(db: Session, league: League, gw_number: int) -> dict:
    """Real-life PL fixtures for `gw_number`: the per-fixture list (sorted by
    kickoff) plus aggregate counts. `status` is per-fixture, from `rules.
    fixture_status` — this is the accurate, per-match answer `Match.finished`
    (an H2H scoring-lock flag, unrelated to any real match) can't give."""
    fixtures = (
        db.query(Fixture)
        .filter(Fixture.league_id == league.id, Fixture.event == gw_number)
        .order_by(Fixture.kickoff_time)
        .all()
    )
    rows = [
        {
            "home": fx.home_team,
            "away": fx.away_team,
            "kickoff_time": fx.kickoff_time,
            "status": fixture_status(fx),
            "home_score": fx.home_score,
            "away_score": fx.away_score,
            "minutes": fx.minutes,
        }
        for fx in fixtures
    ]
    counts = {"total": len(rows), "finished": 0, "in_progress": 0, "not_started": 0}
    for r in rows:
        counts[r["status"]] += 1
    return {"fixtures": rows, "counts": counts}


_STATUS_ORDER = {"not_started": 0, "in_progress": 1, "finished": 2}


def _club_status_by_gw(db: Session, league: League, gw_number: int) -> dict:
    """club short-name -> 'not_started'|'in_progress'|'finished' for `gw_number`.

    A double-GW club (two Fixture rows the same event) collapses to the EARLIEST
    non-finished status — one leg finished and one not still reads as whichever
    of in_progress/not_started applies, never silently 'finished', because a
    player from that club still has a scoring opportunity. A club with NO fixture
    that GW (a blank) is simply absent from this dict — that's a different
    question (see `_gw_fixture_teams`), not this function's job to answer."""
    by_club: dict = {}
    for fx in (
        db.query(Fixture)
        .filter(Fixture.league_id == league.id, Fixture.event == gw_number)
    ):
        status = fixture_status(fx)
        for club in (fx.home_team, fx.away_team):
            if club is None:
                continue
            current = by_club.get(club)
            if current is None or _STATUS_ORDER[status] < _STATUS_ORDER[current]:
                by_club[club] = status
    return by_club


def _club_next_kickoff_by_gw(db: Session, league: League, gw_number: int) -> dict:
    """club short-name -> kickoff_time of its EARLIEST not-yet-finished fixture
    this GW (skipping any fixture with no known kickoff time). Absent from the
    dict if every fixture for that club this GW is already finished, or has no
    recorded kickoff — feeds the "kicks off HH:MM" detail for a remaining
    player who hasn't started yet; an already-in-progress player is shown via
    `playing_now` instead, where a kickoff time is no longer the interesting
    fact."""
    by_club: dict = {}
    for fx in (
        db.query(Fixture)
        .filter(Fixture.league_id == league.id, Fixture.event == gw_number)
    ):
        if fx.finished or fx.kickoff_time is None:
            continue
        for club in (fx.home_team, fx.away_team):
            if club is None:
                continue
            current = by_club.get(club)
            if current is None or fx.kickoff_time < current:
                by_club[club] = fx.kickoff_time
    return by_club


def _club_opponent_by_gw(db: Session, league: League, gw_number: int) -> dict:
    """club short-name -> the club it faces in its earliest unfinished fixture this GW.

    Sibling of `_club_next_kickoff_by_gw` and filtered identically, so the two always
    describe the SAME fixture — the analysis line quotes an opponent and a kickoff
    together, and picking them from different legs of a double gameweek would read as a
    fixture that doesn't exist.
    """
    chosen: dict = {}
    for fx in (
        db.query(Fixture)
        .filter(Fixture.league_id == league.id, Fixture.event == gw_number)
    ):
        if fx.finished or fx.kickoff_time is None:
            continue
        for club, other in ((fx.home_team, fx.away_team), (fx.away_team, fx.home_team)):
            if club is None or other is None:
                continue
            current = chosen.get(club)
            if current is None or fx.kickoff_time < current[0]:
                chosen[club] = (fx.kickoff_time, other)
    return {club: other for club, (_ko, other) in chosen.items()}


def _ruled_out_ids(entries, season, club_status, playing_clubs) -> set:
    """Which of these picks definitively cannot score any more this gameweek.

    A player is ruled out only when he has 0 minutes AND his club is done — either the
    fixture is finished or the club has no fixture at all. A 0-minute player whose match
    is still in progress or hasn't kicked off is NOT ruled out; he may yet play.

    `playing_clubs` guards the blank test the way `_tanking_counts_by_manager` does: a
    gameweek with NO fixture rows at all is missing data, not twenty blank clubs. Without
    that check an unsynced fixture table would rule out every player in the league and
    the projection would confidently report nonsense.
    """
    out = set()
    for entry in entries or []:
        if (entry.get("minutes") or 0) != 0:
            continue
        ps = season.get(entry.get("fpl_id"))
        if ps is None:
            continue
        blank = bool(playing_clubs) and ps.current_team not in playing_clubs
        if blank or club_status.get(ps.current_team) == "finished":
            out.add(entry["fpl_id"])
    return out


def projected_points_by_manager(db: Session, league: League, gw_number: int) -> dict:
    """manager_id -> {"points", "xi", "subs", "short"} with auto-subs projected.

    FPL applies bench substitutions only when a gameweek is FINALISED, so its live
    total shows a manager carrying a hole it will later fill. This fills it now. See
    `rules.project_auto_subs` for the substitution rule and why it is not FPL's literal
    one.

    Each sub carries the incoming player's name, position, points so far, and whether
    he has played yet — everything the analysis line and the scoreboard need without
    re-resolving element ids.
    """
    gw = db.query(Gameweek).filter_by(league_id=league.id, number=gw_number).one_or_none()
    if not gw:
        return {}
    season = _season_by_fpl_id(db, league)
    positions = {fid: (ps.position or "").upper() for fid, ps in season.items()}
    club_status = _club_status_by_gw(db, league, gw_number)
    playing_clubs = _gw_fixture_teams(db, league).get(gw_number) or set()
    next_kickoff = _club_next_kickoff_by_gw(db, league, gw_number)
    opponents = _club_opponent_by_gw(db, league, gw_number)

    out: dict = {}
    for gp in db.query(GameweekPoints).filter_by(gameweek_id=gw.id):
        entries = gp.player_points or []
        ruled = _ruled_out_ids(entries, season, club_status, playing_clubs)
        res = project_auto_subs(entries, positions=positions, ruled_out=ruled)
        by_id = {e.get("fpl_id"): e for e in entries}

        def _describe(fid):
            ps = season.get(fid)
            entry = by_id.get(fid) or {}
            club = ps.current_team if ps else None
            return {
                "fpl_id": fid,
                "name": ps.name if ps else None,
                "position": positions.get(fid),
                "points": entry.get("points") or 0,
                "played": (entry.get("minutes") or 0) > 0,
                "status": club_status.get(club) if club else None,
                # For the analysis line's cover clause: who he still has to face, and
                # when. Both absent once his fixtures are done, which is the signal to
                # quote his points instead.
                "opponent": opponents.get(club) if club else None,
                "kickoff_time": next_kickoff.get(club) if club else None,
            }

        # Who would come on if a given player in the effective XI blanks — keyed BY
        # that player, because the answer depends on his position. Naming "the next
        # bench player" instead is wrong nearly every time: slot 12 is almost always
        # the backup keeper, and he can only ever replace the keeper.
        #
        # Answered by re-running the projection with the player hypothetically ruled
        # out, so the formation rules decide it rather than a second, drifting copy of
        # them living here.
        cover = {}
        for fid in res["xi"]:
            if fid in ruled:
                continue
            ps = season.get(fid)
            if ps is not None and club_status.get(ps.current_team) == "finished":
                continue    # he has played; he cannot blank now
            hypo = project_auto_subs(
                entries, positions=positions, ruled_out=ruled | {fid}
            )
            replacement = next(
                (x["in"] for x in hypo["subs"] if x["out"] == fid), None
            )
            if replacement is not None:
                cover[fid] = _describe(replacement)
        out[gp.manager_id] = {
            "points": res["points"],
            "xi": res["xi"],
            "short": res["short"],
            "cover": cover,
            "subs": [
                {"out": _describe(s["out"]), "in": _describe(s["in"])}
                for s in res["subs"]
            ],
        }
    return out


def players_remaining_by_manager(db: Session, league: League, gw_number: int) -> dict:
    """manager_id -> {"total", "remaining", "in_progress", "by_position",
    "playing_now", "remaining_players"} for a gameweek's EFFECTIVE XI — the picked
    starters with auto-subs projected, so this agrees with the score on the same page.

    That matters for more than tidiness: a bench player who is covering a blank and
    whose own match is still to come is a player this manager genuinely has left. On the
    picked XI he appears nowhere, because the bench is excluded — so the count
    understated exactly the managers the sub projection exists to help. Such a player is
    flagged `sub: True` in `remaining_players`.

    (The previous docstring claimed the starter filter matched "the starter filter
    `_tanking_counts_by_manager` already uses". It never did — anti-tanking reads the
    whole 15-man squad deliberately and has no starter filter at all.)

    A player whose club has NO
    fixture that GW (a blank) is excluded from both `total` and `remaining`
    entirely — he isn't "done," he's simply not counted, so a blank-GW player
    can never make a manager look further along than they are. A double-GW
    player with at least one leg still unfinished counts as remaining
    (`_club_status_by_gw` already resolves the per-club status this way).
    `remaining_players` lists each not-yet-started remaining player's name and
    upcoming kickoff time, sorted soonest first — an in-progress player is
    already covered by `playing_now` and has no useful "kicks off at" to show."""
    gw = db.query(Gameweek).filter_by(league_id=league.id, number=gw_number).one_or_none()
    if not gw:
        return {}
    season = _season_by_fpl_id(db, league)
    club_status = _club_status_by_gw(db, league, gw_number)
    next_kickoff = _club_next_kickoff_by_gw(db, league, gw_number)
    projected = projected_points_by_manager(db, league, gw_number)
    out: dict = {}
    for gp in db.query(GameweekPoints).filter_by(gameweek_id=gw.id):
        buckets = {
            pos: {"total": 0, "remaining": 0, "in_progress": 0}
            for pos in ("GKP", "DEF", "MID", "FWD")
        }
        total = remaining = in_progress = 0
        playing_now = []
        remaining_players = []
        proj = projected.get(gp.manager_id) or {}
        effective = set(proj.get("xi") or [])
        subbed_in = {s["in"]["fpl_id"] for s in (proj.get("subs") or [])}
        for entry in gp.player_points or []:
            fid = entry.get("fpl_id")
            # The EFFECTIVE XI, not the picked one — a projected sub belongs here and a
            # starter he replaced does not. Falls back to the picked XI when there is no
            # projection for this manager, so nothing regresses on missing data.
            in_xi = fid in effective if effective else bool(entry.get("is_starting"))
            if not in_xi:
                continue
            ps = season.get(fid)
            if ps is None:
                continue
            status = club_status.get(ps.current_team)
            if status is None:  # blank GW for this club — excluded entirely
                continue
            pos = (ps.position or "").upper() or "FWD"
            bucket = buckets.setdefault(pos, {"total": 0, "remaining": 0, "in_progress": 0})
            total += 1
            bucket["total"] += 1
            if status != "finished":
                remaining += 1
                bucket["remaining"] += 1
            if status == "in_progress":
                in_progress += 1
                bucket["in_progress"] += 1
                playing_now.append(ps.name)
            elif status == "not_started":
                remaining_players.append({
                    "fpl_id": fid,
                    "name": ps.name,
                    "position": pos,
                    "kickoff_time": next_kickoff.get(ps.current_team),
                    # Marked so the page can say WHY someone who wasn't picked to start
                    # is listed as still to play.
                    "sub": fid in subbed_in,
                })
        remaining_players.sort(
            key=lambda p: (p["kickoff_time"] is None, p["kickoff_time"])
        )
        out[gp.manager_id] = {
            "total": total,
            "remaining": remaining,
            "in_progress": in_progress,
            "by_position": buckets,
            "playing_now": playing_now,
            "remaining_players": remaining_players,
        }
    return out


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


def unresolved_absences(db: Session, league: League, manager_id=None) -> list:
    """Absence entries still 'active' once the season is over — the ones whose manager
    owes a Return-or-Release decision.

    An open absence past GW38 means the manager is carrying the absentee PLUS a full 15
    into keeper selection, so they would choose five keepers from sixteen. Callers use
    this to block that, and to nag. Returns [] before season end, when an open absence
    is simply an absence.

    "Season over" is the PHASE or the gameweek, not the gameweek alone. `current_gameweek`
    derives from stored deadline dates and returns None when they are missing — on a
    frozen or freshly-imported row, for instance — and a bare `>= SEASON_LAST_GW` test
    would then quietly return [] and switch this guard off exactly where it matters most.
    """
    if (league.phase or "") != "offseason" and (
        current_gameweek(db, league) or 0
    ) < SEASON_LAST_GW:
        return []
    rows = [e for e in _absence_rows(db, league)
            if (e.status or "").strip().lower() == "active"]
    if manager_id is not None:
        rows = [e for e in rows if e.manager_id == manager_id]
    return rows


def _validate_absence_eligibility(
    db: Session, league: League, manager: Manager, player: Player, *, what: str
) -> bool:
    """Confirms `manager` may place `player` on the given absence list. Returns True
    when the answer came from roster HISTORY rather than the current roster — a
    self-reported historical placement — so the caller can flag the entry as such.

    Placement resolves the player globally, so without this a manager could name
    anyone. That was survivable while an absence only granted keeper candidacy; now
    that it grants OWNERSHIP through the overlay (see _owner_maps), an unchecked
    placement would let a manager claim any un-rostered player as theirs — including
    in draft slot math.

    ORDINARY case: he's on the manager's current effective roster (a commissioner
    trade counts too, same as before) — returns False.

    HISTORICAL case: he's NOT on the current roster, but `presence`
    (_roster_presence_and_il_coverage) shows the manager held him at some point THIS
    season — the "drafted him, he got hurt, dropped him for a replacement before ever
    recording it" gap. Fails closed on either half of that check: never held by this
    manager at all raises the same message as the ordinary refusal; held now by a
    DIFFERENT manager (he was later claimed for real) raises a distinct message,
    because silently accepting the claim and letting _owner_maps' own "only if
    unowned" fold guard quietly no-op it would be a worse failure mode than refusing
    up front — every other guard in this subsystem fails loud, not quiet.

    A league with no gameweeks yet (preseason) can't answer either question, so it
    doesn't refuse — same as before.
    """
    gw = latest_gameweek(db, league)
    if gw is None:
        return False
    if player.id in _effective_roster_pids(db, league, manager.id, gw.id):
        return False

    presence, _il = _roster_presence_and_il_coverage(db, league, gw.number)
    if not presence.get((manager.id, player.id)):
        raise RuleViolation(
            f"{player.name} isn't on {manager.display}'s roster, so they can't go on "
            f"the {what}"
        )
    holder_id = effective_owner(db, league).get(player.id)
    if holder_id is not None and holder_id != manager.id:
        holder = db.get(Manager, holder_id)
        raise RuleViolation(
            f"{player.name} is currently rostered by "
            f"{holder.display if holder else 'another manager'}, so {manager.display} "
            f"can't put him on the {what}"
        )
    return True


def place_on_il(
    db: Session,
    league: League,
    *,
    fpl_manager_id: str,
    injured_fpl_id: int,
    replacement_fpl_id: int,
    start_gw: int,
    require_roster: bool = True,
) -> dict:
    """Place a manager's injured player on the IL with a same-position replacement.

    Enforces: one active IL player per manager; replacement same position; the injured
    player is actually the manager's.

    `require_roster=False` is for the commissioner's HISTORICAL backfill only, and skips
    _validate_absence_eligibility entirely — no roster-history check either, since a
    genuinely old season's row may have none. Manager self-service (the default) runs
    that check, which itself now accepts either the current roster OR this manager's
    OWN roster history this season (drafted him, he got hurt, dropped him for a
    replacement before ever recording it here) — the exact gap that used to force a
    manager to ask the commissioner for something the site could verify itself.
    """
    manager = _resolve_manager(db, league, fpl_manager_id)
    injured = _resolve_player(db, injured_fpl_id)
    replacement = _resolve_player(db, replacement_fpl_id)
    if injured.id == replacement.id:
        raise RuleViolation("replacement must be a different player")
    self_reported = False
    if require_roster:
        self_reported = _validate_absence_eligibility(
            db, league, manager, injured, what="injury list"
        )

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
        self_reported=self_reported,
    )
    db.add(entry)
    record_audit(db, league, action="il.place",
                 summary=(f"{manager.display} placed {injured.name} "
                          f"({injured.position}) on IL → {replacement.name} (GW{start_gw})"
                          + (" [self-reported, off-roster]" if self_reported else "")),
                 manager_ids=[manager.id],
                 details={"injured_fpl_id": injured_fpl_id,
                          "replacement_fpl_id": replacement_fpl_id, "start_gw": start_gw,
                          "self_reported": self_reported})
    db.commit()
    db.refresh(entry)
    return _il_to_dict(entry, injured, replacement)


def il_return_eligible_gw(start_gw: int) -> int:
    """Earliest GW an IL'd player may return (min stay, capped at season end)."""
    return min(start_gw + MIN_IL_STAY_GWS, SEASON_LAST_GW)


def _resolve_absence_release(
    db: Session, league: League, entry, return_gw: int, released_fpl_id: int | None
):
    """The player a manager gives up to bring an absentee back after GW38.

    Mid-season this is nobody's business but FPL's: the manager makes the swap in the
    app and the next sync shows it. After GW38 the roster is frozen and no further
    snapshot arrives, so without this the manager would carry BOTH the absentee and a
    full 15 into keeper selection — choosing five keepers from sixteen while everyone
    else chooses from fifteen. `requirements.md` already required a post-GW38 return or
    waiver; this is the return half of it.

    Manager-designated, never inferred. FPL records no paired add/drop, so which arrival
    replaced which departure is genuinely unknowable from the data — see
    docs/DESIGN_IL_OWNERSHIP.md §5.
    """
    if return_gw < SEASON_LAST_GW:
        if released_fpl_id is not None:
            raise RuleViolation(
                "a player is only released to make room at season end — mid-season, "
                "make the swap in the FPL app and it will sync"
            )
        return None
    if released_fpl_id is None:
        raise RuleViolation(
            "the season is over and your roster is frozen, so bringing this player "
            "back needs someone to make room — choose who leaves, or Release instead"
        )
    released = _resolve_player(db, released_fpl_id)
    if released.id == entry.player_id:
        raise RuleViolation(
            "that's the player you're bringing back — Release them instead if you "
            "don't want them"
        )
    gw = latest_gameweek(db, league)
    if gw is not None and released.id not in _effective_roster_pids(
        db, league, entry.manager_id, gw.id
    ):
        raise RuleViolation(f"{released.name} isn't on your roster")
    return released


def return_from_il(
    db: Session, league: League, il_id: str, return_gw: int, via: str = "manual",
    released_fpl_id: int | None = None,
) -> dict:
    """Return an active IL player. Enforces the minimum-stay rule (a return at or
    after the season's last GW is automatic). `via='waiver'` marks a waiver return.

    A return at season end must also name the player who leaves to make room
    (`released_fpl_id`) — see `_resolve_absence_release`. A waiver return never does:
    the absentee himself is what leaves.
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
    released = (
        None if via == "waiver"
        else _resolve_absence_release(db, league, entry, return_gw, released_fpl_id)
    )

    entry.end_gw = return_gw
    entry.status = "waived" if via == "waiver" else "returned"
    entry.released_player_id = released.id if released else None
    injured = db.get(Player, entry.player_id)
    replacement = db.get(Player, entry.replacement_id) if entry.replacement_id else None
    mgr = db.get(Manager, entry.manager_id)
    record_audit(db, league, action="il.return",
                 summary=(f"{mgr.display if mgr else '—'} returned "
                          f"{injured.name if injured else '—'} from IL "
                          f"(GW{return_gw}, {entry.status})"),
                 manager_ids=[entry.manager_id],
                 details={"il_id": str(il_id), "return_gw": return_gw, "via": via,
                          "released": released.name if released else None})
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
    eligibility is preserved while away (covered in the keeper-drop derivation).

    Same self-reported historical path as place_on_il: if `away` isn't on the manager's
    current roster, _validate_absence_eligibility falls back to this manager's own
    roster history this season before refusing — the AFCON/Asia Cup twin of the "drafted
    him, he got hurt, dropped him before recording it" gap.
    """
    from models import InternationalList

    manager = _resolve_manager(db, league, fpl_manager_id)
    away = _resolve_player(db, away_fpl_id)
    replacement = _resolve_player(db, replacement_fpl_id)
    if away.id == replacement.id:
        raise RuleViolation("replacement must be a different player")
    self_reported = _validate_absence_eligibility(
        db, league, manager, away, what="international list"
    )
    _refuse_goalkeeper_list_move(league, away, replacement, what="international list")
    if not il_same_position(away.position, replacement.position):
        raise RuleViolation(
            f"replacement is {replacement.position}, must match the away "
            f"player's position {away.position}"
        )
    # NO one-active-entry cap, unlike the IL. A manager may have as many players away
    # as are actually called up, each with its own replacement: the league cannot
    # control call-ups, so a cap would arbitrarily punish whoever drafted African or
    # Asian internationals. This function used to refuse a second entry, contradicting
    # the rule (decided 2026-08-20; CLAUDE.md always said "one replacement per ABSENCE").
    if (
        db.query(InternationalList)
        .filter_by(manager_id=manager.id, player_id=away.id, status="active")
        .first()
    ):
        raise RuleViolation(f"{away.name} is already on the international list")
    entry = InternationalList(
        player_id=away.id, manager_id=manager.id, start_gw=start_gw,
        replacement_id=replacement.id, tournament=tournament or None, status="active",
        self_reported=self_reported,
    )
    db.add(entry)
    record_audit(db, league, action="intl.place",
                 summary=(f"{manager.display} placed {away.name} ({away.position}) on the "
                          f"international list → {replacement.name} (GW{start_gw}"
                          + (f", {tournament}" if tournament else "") + ")"
                          + (" [self-reported, off-roster]" if self_reported else "")),
                 manager_ids=[manager.id],
                 details={"away_fpl_id": away_fpl_id, "replacement_fpl_id": replacement_fpl_id,
                          "start_gw": start_gw, "tournament": tournament,
                          "self_reported": self_reported})
    db.commit()
    db.refresh(entry)
    return {"player": away.name, "replacement": replacement.name, "start_gw": start_gw}


def return_from_intl(db: Session, league: League, intl_id: str, return_gw: int,
                     released_fpl_id: int | None = None) -> dict:
    """Re-add a returning player (their nation was eliminated). No minimum stay — the
    replacement is dropped to make room (the manager picks the returner back up)."""
    from models import InternationalList

    entry = db.get(InternationalList, intl_id)
    if not entry:
        raise RuleViolation("international-list entry not found")
    if entry.status != "active":
        raise RuleViolation(f"international-list entry is already '{entry.status}'")
    # Same season-end rule as the IL: after GW38 the roster is frozen, so bringing him
    # back needs someone named to make room.
    released = _resolve_absence_release(db, league, entry, return_gw, released_fpl_id)
    entry.end_gw = return_gw
    entry.status = "returned"
    entry.released_player_id = released.id if released else None
    away = db.get(Player, entry.player_id)
    mgr = db.get(Manager, entry.manager_id)
    record_audit(db, league, action="intl.return",
                 summary=(f"{mgr.display if mgr else '—'} returned "
                          f"{away.name if away else '—'} from the international list "
                          f"(GW{return_gw})"),
                 manager_ids=[entry.manager_id],
                 details={"intl_id": str(intl_id), "return_gw": return_gw,
                          "released": released.name if released else None})
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


def get_all_transactions(db: Session) -> list[dict]:
    """get_transactions, across every season, newest season first.

    A thin wrapper, not a reimplementation: GW numbers repeat 1-38 every season,
    so diffing roster snapshots by bare GW number across league rows would
    compare unrelated gameweeks. Each row's per-league derivation is untouched;
    this only groups the results by season for the page.
    """
    out = []
    for lg in db.query(League).order_by(League.season_year.desc()):
        weeks = get_transactions(db, lg)
        if weeks:
            out.append({"year": lg.season_year, "weeks": weeks})
    return out


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


def scoreboard_freshness(db: Session) -> dict:
    """When gameweek points and real-life PL fixtures were each last successfully
    synced — the honest 'as of' timestamp for the Scores page. Two separate sync
    sub-tasks, so they can legitimately drift out of step; show both rather than
    picking one and implying more freshness than the other actually has."""
    from models import SyncLog

    def _last(kind):
        return (
            db.query(SyncLog)
            .filter(SyncLog.kind == kind, SyncLog.ok.is_(True))
            .order_by(SyncLog.started_at.desc())
            .first()
        )

    points = _last("gameweek_points")
    fixtures = _last("fixtures")
    return {
        "points_synced_at": points.started_at if points else None,
        "fixtures_synced_at": fixtures.started_at if fixtures else None,
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
    # effective_owner, not a local re-seed from Roster plus player_ownership on top:
    # that is exactly what this function used to do, byte-for-byte the same as the
    # helper but as a private copy that could drift from it. It must move in step with
    # _derive_keeper_status below, because this row renders the owner and the keeper
    # facts side by side and overlaying only one of them shows the new owner with a
    # blank keeper column.
    owner_by_pid: dict = effective_owner(db, league) if gw else {}
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
    # The on_il BADGE, distinct from ownership above. Reads the same rows as the fold
    # (_absence_rows) so the badge and the owner column can't tell different stories,
    # but keeps its own "currently away" predicate — a returned-at-season-end absentee
    # is still owned and no longer away.
    il_pids = {
        e.player_id for e in _absence_rows(db, league)
        if (e.status or "").strip().lower() == "active"
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


def _return_required_entries(db: Session, league: League) -> list[dict]:
    """Active absence entries where the player is playing again but still off the
    manager's roster — the must-return alert (docs/DESIGN_IL_OWNERSHIP.md §6).

    The trigger is `last_played_gw` (set by `sync.sync_gameweek_points` from data it
    already fetches): once it's set, the absentee has logged real minutes for his club
    since being parked. Only 'active' entries are considered — the moment a manager
    genuinely re-adds him, `reconcile_absences` closes the entry on the same sync, so an
    active entry with logged minutes really does mean he's still sitting there.

    Gated, for the IL only, on `il_return_eligible_gw` having passed: the 4-GW minimum
    stay holds even if he recovers sooner, so an early return-to-play is not yet a
    violation. The international list has no minimum stay, so it fires as soon as he
    plays. Shared by `flagged_actions` (the homepage nag) and `data_health`, so the two
    can't disagree about who's overdue.
    """
    from models import InternationalList

    cur = current_gameweek(db, league)
    out: list[dict] = []
    for model, kind in ((InjuryList, "Injury list"), (InternationalList, "International")):
        for e, m, p in (
            db.query(model, Manager, PlayerSeason)
            .join(Manager, Manager.id == model.manager_id)
            .join(PlayerSeason, PlayerSeason.player_id == model.player_id)
            .filter(Manager.league_id == league.id, model.status == "active",
                    model.last_played_gw.isnot(None), PlayerSeason.league_id == league.id)
        ):
            if model is InjuryList:
                eligible = il_return_eligible_gw(e.start_gw)
                if cur is None or cur < eligible:
                    continue  # still inside the minimum stay — not yet a violation
                overdue_gws = cur - eligible + 1
            else:
                overdue_gws = (cur - e.start_gw + 1) if cur is not None else None
            out.append({
                "kind": kind, "manager": m.display, "player": p.name,
                "last_played_gw": e.last_played_gw, "overdue_gws": overdue_gws,
            })
    return out


def flagged_actions(db: Session, league: League) -> list[dict]:
    """League attention items for the home page: IL/international players that must be
    returned at season end, players on the IL 4+ GWs (eligible to return), players
    playing again but still parked, and teams flagged or at risk of an anti-tanking
    violation."""
    from models import InternationalList

    cur = current_gameweek(db, league)
    season_over = cur is not None and cur >= SEASON_LAST_GW
    out: list[dict] = []

    # Playing again but still off the roster — checked first, since it's the one that
    # can fire mid-season and needs the most urgent attention.
    for e in _return_required_entries(db, league):
        gws = f" ({e['overdue_gws']} GW{'s' if e['overdue_gws'] != 1 else ''} overdue)"             if e["overdue_gws"] else ""
        out.append({"category": e["kind"], "manager": e["manager"],
                    "detail": f"{e['player']} is playing again (GW{e['last_played_gw']}) "
                              f"but still parked{gws} — return them"})

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

    # Rostering a player who was added to FPL after the draft. Add/drops happen in the
    # FPL app, so nothing here can BLOCK the pickup — the rule's only teeth are a
    # rejected keeper submission months later, by which point the manager has already
    # paid to acquire him. Surfacing ownership now is the whole point: the homepage
    # report lists ineligible PLAYERS but never says who holds one.
    inelig = _ineligible_fpl_ids(db, league)
    if inelig:
        owner = effective_owner(db, league)
        mgr = {m.id: m for m in db.query(Manager).filter_by(league_id=league.id)}
        # PlayerIneligibility is keyed on the SEASON's element id, so resolve through
        # PlayerSeason rather than the global Player.fpl_id (which now means whoever
        # holds that id this season).
        for fid, pid, pname in (
            db.query(PlayerSeason.fpl_id, PlayerSeason.player_id, PlayerSeason.name)
            .filter(PlayerSeason.league_id == league.id,
                    PlayerSeason.fpl_id.in_(inelig))
        ):
            mid = owner.get(pid)
            m = mgr.get(mid)
            if m is None:
                continue
            out.append({"category": "Ineligible player", "manager": m.display,
                        "detail": f"{pname} was added to FPL after the draft — "
                                  "cannot be kept this season"})

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

    `acquisition` is one of `rules.KEEPER_ACQUISITIONS` ('draft' | 'waiver' | 'trade' |
    'discovery'); both fields are optional, and only what's passed is changed.
    Overriding acquisition matters because the =<2 waiver keeper cap counts on it, and
    the derivation calls any unexplained roster gap a drop — a missing injury-list
    record is enough to cost someone a waiver slot.
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
    held against you). They must agree on what "covered" means.

    Deliberately STATUS-BLIND and range-based: a closed entry still explains the weeks
    it spanned. That is what makes it the wrong question for ownership — see
    `_absence_held`, which reads the same rows (`_absence_rows`) and must not be merged
    with this."""
    cover: dict = {}
    for e in _absence_rows(db, league):
        cover.setdefault((e.manager_id, e.player_id), set()).update(
            range(e.start_gw, (e.end_gw or last_n) + 1)
        )
    return cover


def _absence_rows(db: Session, league: League) -> list:
    """Every injury-list and international-list row for this league, one query per table.

    The single source both absence questions read, so they can never drift onto
    different data. They are DIFFERENT QUESTIONS and legitimately differ in predicate:
    `_absence_cover` asks "was he excused that gameweek" (range-based, status-blind),
    `_absence_held` asks "does his manager still hold him" (status-aware). Sharing the
    rows is the invariant; sharing the predicate would be wrong.
    """
    from models import InternationalList

    out = []
    for model in (InjuryList, InternationalList):
        out.extend(
            db.query(model)
            .join(Manager, Manager.id == model.manager_id)
            .filter(Manager.league_id == league.id)
            # Deterministic: two entries can name the same player, and _owner_maps
            # resolves ties by first-wins. player_ownership is called more than once in
            # a single request (player_portal, then again inside _derive_keeper_status),
            # so an unordered query could make two panels on one page disagree.
            .order_by(model.start_gw, model.id)
            .all()
        )
    return out


def _absence_held(db: Session, league: League, last_n: int) -> tuple[list, list]:
    """(held, released) — the ownership half of the absence rules.

    LISTS, not sets, and ordered by (start_gw, id): `_owner_maps` resolves a contested
    player first-wins, so set iteration order would make the winner vary between calls.

    `held` is [(manager_id, player_id)] for absentees the manager still holds even
    though the FPL roster shows their replacement instead. `released` is
    {(manager_id, player_id)} for the players given up to bring an absentee back after
    GW38 (see InjuryList.released_player_id).

    HELD, and why the obvious predicate is wrong. `status == 'active'` alone forks from
    `_absence_cover` on every season-end return, silently and permanently: cover folds
    an open-ended entry through `last_n` and is status-blind, so a 'returned' entry with
    `end_gw >= last_n` is still COVERED while a status test says not held. At GW38
    `last_n` stops advancing, so the disagreement never expires — and it lands on the
    worst pair, `submit_keepers` (validates against the coverage-based candidate set, so
    it ACCEPTS the keeper) versus `effective_keeper_selections` (validates against
    ownership, so it DROPS the selection). The manager submits a keeper the site accepts
    and silently loses a draft slot. Hence: held while active, and still held after a
    return that lands at or beyond the final gameweek in view.

    'waived' is never held past `end_gw` — waiving IS the manager giving him up. A NULL
    status is not held; `data_health` reports those rather than guessing.
    """
    from models import InternationalList  # noqa: F401  (via _absence_rows)

    held, released = [], []
    for e in _absence_rows(db, league):
        status = (e.status or "").strip().lower()
        if status == "active" or (
            status == "returned" and e.end_gw is not None and last_n <= e.end_gw
        ):
            held.append((e.manager_id, e.player_id))
        if e.released_player_id is not None:
            released.append((e.manager_id, e.released_player_id))
    return held, released


def record_absentee_minutes(
    db: Session, league: League, live_stats: dict, gw_number: int
) -> None:
    """Feed the must-return alert (docs/DESIGN_IL_OWNERSHIP.md §6): for every ACTIVE
    absence in this league, if the absent player logged real minutes this gameweek,
    record it as `last_played_gw`.

    Called from `sync.sync_gameweek_points` with the `elements` map it already fetched
    from `/event/{gw}/live` — that payload carries minutes for EVERY player in the
    game, not just the ones on a manager's roster, so an absentee's minutes are sitting
    in data already in hand. Split out as a plain function of a session, a league, and
    a plain dict so it's testable without an HTTP call or a live gameweek — no `sync`
    import needed here, and no test needs one either.

    Deliberately does NOT touch `GameweekPoints.player_points`: that list means "FPL's
    lineup" to `rules.zero_minute_count`, and folding absentees into it would silently
    change what the anti-tanking rule excuses.

    Commits. Called mid-sync, same as every other write in `sync.py`.
    """
    from models import InternationalList

    def _minutes(fpl_id: int) -> int:
        return (live_stats.get(str(fpl_id), {}).get("stats", {}) or {}).get("minutes", 0)

    for model in (InjuryList, InternationalList):
        for entry, player in (
            db.query(model, Player)
            .join(Manager, Manager.id == model.manager_id)
            .join(Player, Player.id == model.player_id)
            .filter(Manager.league_id == league.id, model.status == "active")
        ):
            if player.fpl_id is not None and _minutes(player.fpl_id) > 0:
                entry.last_played_gw = gw_number
    db.commit()


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


def dropped_players_for_manager(db: Session, league: League, manager: Manager) -> list[dict]:
    """Players this manager held at some point THIS season but no longer holds and
    nobody else currently holds either — candidates for a self-reported historical IL
    or international-list placement (a player drafted, hurt, and dropped for a
    replacement before the manager ever recorded the injury here).

    Built from the same `presence` dict `_derive_keeper_status` and
    `unexplained_roster_gaps` already share, scoped to one manager, so this is a third
    reader rather than a new query shape. A player currently held by a DIFFERENT
    manager is excluded here too — same rule `_validate_absence_eligibility` enforces
    at write time, so the picker never offers a choice the write path would refuse.

    Returns [{"fpl_id", "name", "label", "suggested_start_gw"}], newest-dropped first,
    for the self-service form's picker — keyed on `fpl_id` like every other self-service
    IL/international picker, and so excludes anyone who has since left the league
    entirely (a rare combination on top of an already-rare gap; see the still-open
    "IL backfill form must search by player name, not FPL id" backlog item, which
    should pick this picker up too if that ever needs closing).
    """
    gw = latest_gameweek(db, league)
    if gw is None:
        return []
    last_n = gw.number
    presence, _il = _roster_presence_and_il_coverage(db, league, last_n)
    on_roster_now = _effective_roster_pids(db, league, manager.id, gw.id)
    holder = effective_owner(db, league)
    candidates = {}
    for (mid, pid), gws in presence.items():
        if mid != manager.id or pid in on_roster_now:
            continue
        held_by = holder.get(pid)
        if held_by is not None and held_by != manager.id:
            continue
        candidates[pid] = max(gws)
    if not candidates:
        return []
    out = []
    for p in db.query(Player).filter(Player.id.in_(candidates)):
        if p.fpl_id is None:
            continue
        last_held_gw = candidates[p.id]
        out.append({
            "fpl_id": p.fpl_id,
            "name": p.name,
            "label": f"{p.name} · {p.current_team}" if p.current_team else p.name,
            "suggested_start_gw": min(last_held_gw + 1, last_n),
        })
    out.sort(key=lambda r: -r["suggested_start_gw"])
    return out


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


def _drafted_this_season(db: Session, league: League, presence: dict) -> tuple[set, set]:
    """(drafted, trusted) for the main draft that stocked THIS season's rosters.

    `drafted` is {(manager_id, player_id)} actually selected. `trusted` is the set of
    managers for whom that answer is COMPLETE — only for them may a caller read "not in
    `drafted`" as "not drafted".

    Why the second return value. `started_with_manager` ("on the GW1 roster") was only
    ever a proxy for "drafted", and it over-grants: a preseason free-agent signing lands
    on GW1 too and collects a draft-length clock. Consulting DraftPick fixes that, but
    ONLY where the picks are actually recorded. Absence of a pick is not evidence of a
    free-agent signing when nobody recorded any picks at all — seasons before 2026
    predate the live draft board entirely, and treating their silence as "undrafted"
    would regress every historical keeper to 'waiver'. So a manager is `trusted` only if
    at least one of his main-draft picks for this season is on record.

    Label-only picks. A handful of real picks carry free text and no `player_id` (three
    in the 26/27 draft: Ruben Dias, Alex Scott, Braithwaite) because the player had no
    `players` row when the pick was made. Those are resolved against THAT MANAGER'S OWN
    GW1 roster — a ~15-player haystack, not the global pool — by exact normalised
    full-name first, then a token-subset match, which is how a typed "Ruben Dias" reaches
    a web_name of "Dias". Reuses the discovery matcher's `_match_norm`/`_match_tokens`
    rather than adding a fourth normalisation.

    An UNRESOLVED label pick removes its manager from `trusted`, deliberately: the pick
    we couldn't read might be the very player being asked about, so the honest answer is
    "no complete evidence" and the caller keeps the proxy. Failing the other way would
    silently cost a genuinely drafted keeper a year.

    Queried with NO `league_id` filter and bridged by PERSON — same precedent as
    `_goalie_team_history` and `_in_progress_bridge`. A draft run before a rollover lives
    on the outgoing row under that row's manager uuids, and FPL reissued every entry id
    at the 26/27 rollover (overlap zero), so `display_name` leads and `fpl_manager_id` is
    only the fallback, exactly as `_manager_bridge` does it.
    """
    season = league.season_year or 0
    picks = (
        db.query(DraftPick)
        .filter(DraftPick.season_year == season, DraftPick.draft_type == "main")
        .all()
    )
    if not picks:
        return set(), set()

    here = db.query(Manager).filter_by(league_id=league.id).all()
    by_person = {
        (m.display_name or "").strip().casefold(): m.id
        for m in here if (m.display_name or "").strip()
    }
    by_fpl = {m.fpl_manager_id: m.id for m in here}
    src = {
        m.id: m for m in db.query(Manager).filter(
            Manager.id.in_({p.manager_id for p in picks})
        )
    }

    def _bridge(manager_id):
        m = src.get(manager_id)
        if m is None:
            return None
        person = (m.display_name or "").strip().casefold()
        return (by_person.get(person) if person else None) or by_fpl.get(m.fpl_manager_id)

    # GW1 roster per manager — the haystack a free-text label is resolved against.
    gw1 = {}
    for (mid, pid), gws in presence.items():
        if 1 in gws:
            gw1.setdefault(mid, set()).add(pid)
    names = {
        p.id: p for p in db.query(Player).filter(
            Player.id.in_({pid for pids in gw1.values() for pid in pids})
        )
    } if gw1 else {}

    drafted, has_pick, unresolved = set(), set(), set()
    for dp in picks:
        if dp.team_id is not None:
            # A goalie-team pick names a CLUB, and says nothing about which PLAYERS this
            # manager drafted — so it must not count as evidence. Counting it made a
            # manager whose only recorded pick was his club `trusted` with an empty
            # drafted set, and every outfielder on his GW1 roster read as an undrafted
            # free agent (caught by test_goalie_team_keepers, which does exactly that).
            continue
        mid = _bridge(dp.manager_id)
        if mid is None:
            continue
        has_pick.add(mid)
        if dp.player_id is not None:
            drafted.add((mid, dp.player_id))
            continue
        if not dp.player_label:
            continue
        hit = _resolve_label_on_roster(dp.player_label, gw1.get(mid, set()), names)
        if hit is None:
            unresolved.add(mid)
        else:
            drafted.add((mid, hit))
    return drafted, has_pick - unresolved


def _resolve_label_on_roster(label: str, pids: set, names: dict):
    """The single player on `pids` that `label` names, or None if it's not exactly one.

    Exact normalised full name first; failing that, a token-subset match (the player's
    tokens are a subset of the label's, so a short web_name "Dias" is found by the typed
    "Ruben Dias"). Ambiguity returns None rather than guessing — the caller treats that
    as missing evidence, which is the safe direction.
    """
    lab_n, lab_t = _match_norm(label), _match_tokens(label)
    if not lab_n:
        return None
    exact = [
        pid for pid in pids
        if _match_norm(getattr(names.get(pid), "full_name", "") or "") == lab_n
        or _match_norm(getattr(names.get(pid), "name", "") or "") == lab_n
    ]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    subset = []
    for pid in pids:
        p = names.get(pid)
        if p is None:
            continue
        for field in (p.full_name, p.name):
            toks = _match_tokens(field or "")
            if toks and toks <= lab_t:
                subset.append(pid)
                break
    return subset[0] if len(subset) == 1 else None


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

    # A candidate is either on the active roster at the final GW, OR still held through
    # an IL/international entry — a player swapped out for a replacement and never
    # returned is genuinely still theirs for keeper purposes, even though the
    # FPL-synced roster shows the replacement in that slot at the final GW, not him.
    #
    # This asks _absence_held, the SAME predicate the ownership overlay uses, and not
    # `il` (that is _absence_cover, the "was he excused" question). Asking cover here
    # was the old behaviour and it forks from ownership on a season-end return: cover is
    # status-blind, so a 'returned' entry keeps granting candidacy while the overlay has
    # already let go. submit_keepers would accept a keeper that effective_keeper_selections
    # then silently drops, costing the manager a draft slot.
    absence_held, absence_released = _absence_held(db, league, last_n)
    final_candidates = {k for k, gws in presence.items() if last_n in gws}
    # Filtered through the ownership map, not taken raw: _owner_maps only honours an
    # absence when NOBODY rosters the player, and candidacy has to agree with it. An
    # entry left open after the player was dropped and claimed by someone else would
    # otherwise put him on both managers' keeper boards — he is genuinely the claimant's.
    absence_owner = effective_owner(db, league)
    final_candidates |= {
        (mid, pid) for mid, pid in absence_held if absence_owner.get(pid) == mid
    }
    # ...minus anyone given up to bring an absentee back after GW38. He is still on the
    # frozen snapshot, so without this he stays keepable by the manager who released him.
    final_candidates -= set(absence_released)
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
    #
    # Two sets, deliberately: `discovery_flagged` is EVERY is_discovery selection, on
    # roster or off, and is what the acquisition channel below reads. `discovery_only`
    # is the off-roster subset, and is the one that widens the candidate set — an
    # on-roster pick is already a candidate and must not be added twice.
    discovery_flagged = {
        (m, p) for (m, p), is_disc in kept.items() if is_disc and p is not None
    }
    discovery_only = discovery_flagged - final_candidates
    final_candidates |= discovery_only

    # A discovery pick LINKED to a real player (services.link_discovery_pick) is the
    # only way this derivation can see the September discovery draft at all: it reads
    # rosters and trades, and a discovery pick is neither. Without it a player taken in
    # September who joins the PL in January is on nobody's GW1 roster and has no Trade
    # row, so he falls through rules.keeper_status to ("waiver", 3) — a keeper year
    # short of the draft-length clock a discovery acquisition earns.
    #
    # This is NOT redundant with `discovery_flagged` above. That set comes from
    # KeeperSelection and so is empty for any caller with no viewer — including
    # submit_keepers, which is exactly when the label matters most. This one comes from
    # DraftPick, which is public draft history under no privacy gate at all, so it
    # works for every caller. They cover each other's blind spot.
    #
    # Keyed through Manager.fpl_manager_id, never managers.id: `managers` has one row
    # per manager PER SEASON, and a pick made before a rollover lives on the OUTGOING
    # league row under that row's own manager uuid. Same hazard, same fix, as
    # _goalie_team_history.
    mine = {
        m.fpl_manager_id: m.id
        for m in db.query(Manager).filter_by(league_id=league.id)
    }
    discovery_linked = set()
    for pid_linked, fpl in (
        db.query(DraftPick.player_id, Manager.fpl_manager_id)
        .join(Manager, Manager.id == DraftPick.manager_id)
        .filter(DraftPick.draft_type == "discovery",
                DraftPick.player_id.isnot(None))
    ):
        mid_here = mine.get(fpl)
        if mid_here is not None:
            discovery_linked.add((mid_here, pid_linked))

    # ---- "was he DRAFTED, or just on the GW1 roster?" -----------------------
    # `started_with_manager` below is "on the GW1 roster", which was only ever a PROXY
    # for "drafted". A player signed in preseason free agency AFTER the draft also
    # lands on GW1, and the proxy hands him ("draft", 4) — a full year more than the
    # waiver rule allows. Consulting real DraftPick rows draws the line properly.
    #
    # GUARDED on the season actually having recorded main-draft picks. Only 2026 onward
    # does; everything earlier predates the live draft board, so applying the
    # distinction there would find NO picks and regress every historical keeper to
    # 'waiver'. When there are no picks to consult, the GW1 proxy stands unchanged.
    drafted, drafted_trusted = _drafted_this_season(db, league, presence)

    # ---- who held this player immediately before? ---------------------------
    # The clock follows the PLAYER through a drop: if A held him and B claimed him off
    # waivers, B inherits A's clock (capped at the waiver fresh cap by keeper_status).
    # Built from `presence`, which is already loaded — no extra query.
    held_at: dict = {}
    for (mid_h, pid_h), gws_h in presence.items():
        for g in gws_h:
            held_at.setdefault(pid_h, {})[g] = mid_h

    def _previous_holder(mid, pid, first_gw):
        """(manager_id, their last GW) for whoever held `pid` most recently before
        `first_gw`, or None. Skips `mid` himself — a manager who dropped and re-added
        his own player is the `dropped` case, and his own seed is already the answer."""
        by_gw = held_at.get(pid) or {}
        for g in range(first_gw - 1, 0, -1):
            other = by_gw.get(g)
            if other is not None and other != mid:
                return other, g
        return None

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

        The CLOCK BELONGS TO THE PLAYER, not the pair. Three sources for it, in strict
        precedence order — a commissioner seed for this manager, then a trade sender,
        then whoever last held him before this manager claimed him off waivers. Each is
        consulted only if the one before it said nothing, and `is None` is the test
        every time: a deliberate seed of 0 ("maxed out, cannot be kept") is falsy and
        must not fall through to a later source that would hand the years back.
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
        my_gws = presence.get((mid, pid), set())
        # Dropped by someone else and claimed here: inherit their clock. Evaluated with
        # `upto` = their last gameweek, exactly as the trade path does — asked about
        # their FULL tenure their empty tail after the drop reads as a drop of their
        # own, and every carry would collapse to the waiver cap instead of the real
        # number. keeper_status applies min(prev, KEEPER_FRESH_WAIVER) on top, so an
        # exhausted clock arrives as 0 and the claimant simply cannot keep him.
        if carried is None and my_gws and (mid, pid) not in seen:
            prior = _previous_holder(mid, pid, min(my_gws))
            if prior is not None:
                carried = _status_for(
                    prior[0], pid, prior[1], seen + ((mid, pid),)
                )[1]
        # "On the GW1 roster" is only a PROXY for "drafted", and it over-grants to a
        # preseason free-agent signing. Where the season's picks are actually recorded
        # (see _drafted_this_season), require the pick; elsewhere the proxy stands.
        #
        # ONLY refines the no-seed case. On GW1 WITH a seed already means "kept", not
        # "drafted" — and a kept player has no DraftPick row, because he never went
        # through the draft. Without this clause every keeper in the league derives as
        # a waiver pickup: 60 of 150 players on the live 26/27 rosters flipped, which
        # would also blow the =<2 waiver-keeper cap for everyone.
        started = 1 in my_gws
        if (
            started
            and seed_remaining.get((mid, pid)) is None
            and mid in drafted_trusted
        ):
            started = (mid, pid) in drafted
        was_dropped = _dropped(mid, pid, upto)
        memo[key] = keeper_status(
            started,
            (mid, pid) in traded_in,
            was_dropped,
            carried,
            # The discovery draft IS an acquisition, worth a full draft-length clock —
            # but it leaves no trace in rosters or trades, so without an explicit label
            # here every discovery player derives as an ordinary waiver pickup. Two
            # independent witnesses to it (see where each is built): the manager's own
            # is_discovery selection, and a commissioner-linked discovery DraftPick.
            #
            # Gated on `not was_dropped` so the label can only ever ADD the missing
            # story, never erase one the roster actually tells: a player genuinely
            # dropped and re-acquired is a waiver pickup no matter how he first
            # arrived, and `acquisition=` short-circuits that branch entirely. An
            # off-roster discovery keeper has no roster history at all, so _dropped is
            # False for him and this gate is transparent to the case it matters for.
            #
            # A commissioner seed still wins over all of it, same as always.
            acquisition=seed_acq.get((mid, pid)) or (
                "discovery"
                if not was_dropped and (
                    (mid, pid) in discovery_flagged
                    or (mid, pid) in discovery_linked
                )
                else None
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
        # `moved` can say None — a player released to make room at season end belongs to
        # nobody, and a None key here would create a phantom manager bucket that every
        # caller of this dict would then have to know about.
        #
        # Deliberately redundant with the `-= absence_released` line above, and NOT dead
        # code: the two fail differently. Drop the subtraction and this catches it; drop
        # player_ownership's None-injection instead and the subtraction catches it, since
        # `moved.get(pid, hist)` would then quietly answer `hist`. Removing either alone
        # is invisible in the tests, which is exactly why both are here.
        if owner is None:
            continue
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


def goalie_team_owner(
    db: Session, league: League, *, season_year: int | None = None
) -> dict:
    """{team_id: fpl_manager_id} — who holds each goalie team right now.

    Base is the season's draft pick or keeper selection; commissioner-entered club
    trades are then applied in `created_at` order, the same overlay-on-read shape
    `player_ownership` uses and for the same reason — the draft pick is a record of
    what happened and must not be rewritten by a later trade.

    Applied only when `from_manager` currently holds the club, so a typo'd direction
    moves nobody instead of teleporting a club (`/admin/health` surfaces the ones that
    didn't apply). `created_at` is the only reliable ordering: `date` is NULL on
    commissioner rows and the PK is a random uuid4.

    `season_year` overrides `league.season_year`, for the same reason
    `_derive_gk_team_keeper_status` grew the same parameter: pre-rollover, `league`
    (current) is still the outgoing row, one year behind the draft actually running.
    Every existing caller omits it and gets the prior behavior exactly.
    """
    cur = season_year if season_year is not None else (league.season_year or 0)
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
    season_year: int | None = None,
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

    `season_year` decouples "which season's clock" from "which league row scopes the
    managers" — every existing caller omits it and gets `league.season_year` exactly
    as before. get_teams_in_progress needs it: pre-rollover, `league` (current) is
    still the OUTGOING row, whose own `season_year` is one behind the draft actually
    running, while `_goalie_team_history` is keyed by the real draft year regardless
    of which row is current.
    """
    if not goalie_team_keepable(league.goalie_team_mode):
        return {}
    cur = season_year if season_year is not None else (league.season_year or 0)
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

    owner = goalie_team_owner(db, league, season_year=cur)
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


def _draft_year_for(league: League) -> int:
    """The season a draft-in-progress view (or anything else needing 'the draft
    that's either running now or just finished') should read.

    The main draft runs on the OUTGOING league row, pre-rollover — 'offseason' (the
    draft hasn't started; the pool being prepared is next season) and 'draft' both
    point one year past this row's own `season_year`. Once the rollover has run,
    the CURRENT row's `season_year` already IS that same draft year — 'preseason'
    and 'in_season' read it directly. Centralizing this here (rather than the
    `season_year + 1` expression scattered across templates/routes/scripts) means a
    later season-alignment migration only has to change this one function.
    """
    if league.phase in ("offseason", "draft"):
        return (league.season_year or 0) + 1
    return league.season_year or 0


def _in_progress_bridge(db: Session, league: League):
    """Cross-row lookup for the draft/preseason in-progress squad view.

    The 2026-style main draft (and the keeper selections that feed its board) can
    live on a DIFFERENT league row than `league` — pre-rollover it runs on the
    outgoing row. This resolves: the draft year; the league row that actually holds
    this year's `KeeperSelection` rows; this year's main-draft `DraftPick` rows
    (queried with NO league filter, the same precedent `_goalie_team_history`
    already uses); and a manager bridge from whatever row a selection/pick's
    `manager_id` belongs to onto `league`'s own Manager row, matched by the stable
    `fpl_manager_id` — never `managers.id`, which is minted fresh per season.

    Returns (draft_year, sel_league, selections, picks, manager_for), where
    manager_for(old_manager_id) -> (bucket_id, display_name, fpl_manager_id) or
    None. A manager with no counterpart on `league` yet renders under their OWN
    row's identity rather than being dropped.
    """
    draft_year = _draft_year_for(league)

    sel_league = league
    if not db.query(KeeperSelection).filter_by(
        league_id=league.id, season_year=draft_year
    ).first():
        other = (
            db.query(League)
            .join(KeeperSelection, KeeperSelection.league_id == League.id)
            .filter(KeeperSelection.season_year == draft_year, League.id != league.id)
            .first()
        )
        if other:
            sel_league = other
    selections = effective_keeper_selections(db, sel_league, draft_year)

    picks = (
        db.query(DraftPick)
        .filter(DraftPick.season_year == draft_year, DraftPick.draft_type == "main")
        .all()
    )

    ref_ids = {s.manager_id for s in selections} | {p.manager_id for p in picks}
    ref_managers = (
        {m.id: m for m in db.query(Manager).filter(Manager.id.in_(ref_ids))}
        if ref_ids else {}
    )
    cur_by_fpl = {
        m.fpl_manager_id: m for m in db.query(Manager).filter_by(league_id=league.id)
    }

    def manager_for(manager_id):
        src = ref_managers.get(manager_id)
        if src is None:
            return None
        target = cur_by_fpl.get(src.fpl_manager_id) or src
        return target.id, target.display, target.fpl_manager_id

    return draft_year, sel_league, selections, picks, manager_for


def get_teams_in_progress(db: Session, league: League) -> list[dict]:
    """Like get_keepers, but for the draft/preseason window before FPL rosters
    exist: each manager's kept players UNION their draft picks so far, instead of
    last season's finished roster. Output shape matches get_keepers' per-manager
    dict (manager/manager_fpl/players) — the routes swap between the two with no
    template change.

    Keeper privacy is moot here: by the time this view is used, enter_draft_phase
    has already set keepers_locked=True, so selections are fully revealed — this
    always derives with kept_all=True.

    Kept players/clubs render their REAL derived acquisition/years/eligible facts.
    A freshly drafted player has no tenure to derive from, so he renders the fact
    he WILL have the moment a season starts: a fresh 4-year draft acquisition
    (rules.KEEPER_FRESH_DRAFT) — not None, which the shared _roster_card.html
    template compares with `> 0` and would crash on.
    """
    draft_year, sel_league, selections, picks, manager_for = _in_progress_bridge(
        db, league
    )

    kept_status = _derive_keeper_status(db, sel_league, kept_all=True)
    names = player_names(db, league)
    positions = {p.id: p.position for p in db.query(Player)}
    pl_teams = {t.id: t for t in db.query(PlTeam)}
    clubs_keepable = goalie_team_keepable(league.goalie_team_mode)
    club_status = (
        _derive_gk_team_keeper_status(
            db, league, kept_all=True, season_year=draft_year
        )
        if clubs_keepable else {}
    )

    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.name).all()
    )
    by_manager = {
        m.id: {"manager": m.display, "manager_fpl": m.fpl_manager_id, "players": []}
        for m in managers
    }

    # A kept club (goalie_team_mode == 'keeper') is fully described by club_status,
    # keyed on this league's own managers already — no bridging needed.
    for mid, club in club_status.items():
        if mid in by_manager:
            by_manager[mid]["players"].append(club)

    def _drafted_player(pid):
        return {
            "player": names.get(pid, str(pid)), "position": positions.get(pid),
            "acquisition": "draft", "years_remaining": KEEPER_FRESH_DRAFT,
            "eligible": True, "reason": None, "kept": False, "kept_discovery": False,
        }

    def _drafted_club(tid):
        team = pl_teams.get(tid)
        return {
            "player": team.name if team else str(tid), "position": GOALIE_TEAM_POSITION,
            "acquisition": "draft", "years_remaining": 0, "eligible": False,
            "reason": "not kept in redraft mode", "kept": False, "kept_discovery": False,
        }

    for s in selections:
        if s.team_id is not None:
            continue  # keeper-mode clubs are already in "players" via club_status
        target = manager_for(s.manager_id)
        if target is None:
            continue
        mid, display, fpl = target
        bucket = by_manager.setdefault(
            mid, {"manager": display, "manager_fpl": fpl, "players": []}
        )
        fact = kept_status.get(s.manager_id, {}).get(s.player_id)
        bucket["players"].append(fact if fact else {
            "player": names.get(s.player_id, str(s.player_id)),
            "position": positions.get(s.player_id), "acquisition": None,
            "years_remaining": 0, "eligible": False,
            "reason": "keeper status could not be derived",
            "kept": True, "kept_discovery": s.is_discovery,
        })

    for p in picks:
        target = manager_for(p.manager_id)
        if target is None:
            continue
        mid, display, fpl = target
        bucket = by_manager.setdefault(
            mid, {"manager": display, "manager_fpl": fpl, "players": []}
        )
        if p.team_id is not None:
            if not clubs_keepable:
                bucket["players"].append(_drafted_club(p.team_id))
            # else: already in "players" via club_status above
        elif p.player_id is not None:
            bucket["players"].append(_drafted_player(p.player_id))
        # else: a main-draft row with neither set shouldn't exist (the model's
        # CHECK is deliberately narrow, per models.py) — skip rather than crash.

    out = []
    seen = set()
    for m in managers:
        entry = by_manager[m.id]
        entry["players"].sort(
            key=lambda x: (not x["eligible"], -x["years_remaining"], x["player"])
        )
        out.append(entry)
        seen.add(m.id)
    # Any manager bridged from an older row with no current-row counterpart:
    # never dropped, rendered under their own identity at the end.
    for mid, entry in by_manager.items():
        if mid in seen:
            continue
        entry["players"].sort(
            key=lambda x: (not x["eligible"], -x["years_remaining"], x["player"])
        )
        out.append(entry)
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
    # Manager-scoped on purpose, not a league-wide lock: only the person who owes a
    # decision is stopped, and everyone else submits normally.
    open_absences = unresolved_absences(db, league, manager.id)
    if open_absences:
        names = ", ".join(
            (db.get(Player, e.player_id).name if db.get(Player, e.player_id) else "—")
            for e in open_absences
        )
        raise RuleViolation(
            f"the season is over and {names} is still on an absence list — return or "
            f"release them on My Team first, so your squad is back to "
            f"{ROSTER_SIZE}"
        )
    status = _derive_keeper_status(db, league).get(manager.id, {})
    by_fpl = {p.fpl_id: p for p in db.query(Player)}
    # A commissioner seed is the override of record and outranks the discovery clock
    # synthesized below, exactly as it outranks every other derived value.
    # manager_id is already league-scoped (one manager row per season), so it alone
    # is the right filter — the same one set_keeper_override writes through.
    seeded = {
        s.player_id for s in db.query(KeeperSeed).filter_by(manager_id=manager.id)
    }

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
            else:
                raise RuleViolation(
                    f"{player.name} is not one of {manager.display}'s keeper "
                    "candidates (traded away?)"
                )
        elif is_discovery and player.id not in seeded:
            # THE BUG THIS FIXES. The synthesis above only ever ran in the off-roster
            # branch, so it missed the ordinary success case exactly: a September
            # discovery pick who JOINS the Premier League and is on the roster — which
            # is the only way he becomes keepable at all. There `status.get` hits and
            # the derived ("waiver", 3) won, costing him a keeper year, because the
            # derivation reads rosters and trades and a discovery pick is neither.
            #
            # This can't be left to _derive_keeper_status's discovery_flagged set:
            # that reads KeeperSelection, and this call deliberately passes no viewer,
            # so it is empty here. A LINKED discovery pick does reach the derivation
            # (discovery_linked) and would already have produced this — but linking is
            # a manual admin step that may not have happened yet, and the manager
            # ticking the discovery box is itself the assertion.
            st = {**st, "acquisition": "discovery",
                  "years_remaining": KEEPER_FRESH_DRAFT,
                  # Recomputed, not inherited: a derived ("waiver", 0) carries
                  # eligible=False, which would survive the corrected clock and refuse
                  # a legitimate keeper. The goalie-team refusal isn't a clock
                  # question, so it's the one thing that still stands.
                  "eligible": keeper_eligible(KEEPER_FRESH_DRAFT)
                              and st.get("reason") is None}
        if (is_discovery and goalie_teams_on(league.goalie_team_mode)
                and (player.position or "").upper() == "GKP"):
            # The one door left open into individual goalkeeper ownership: the
            # discovery keeper may be ANY player, so it bypasses the roster candidate
            # list where the rule is otherwise enforced. Applies to an on-roster
            # discovery pick too — a keeper who joined the PL in January is still a
            # keeper, and his club, not he, is the keepable asset.
            st = {**st, "eligible": False,
                  "reason": "goalkeepers are kept as a club"}
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
def _prior_season_league(db: Session, league: League, season_year: int) -> League:
    """The row for the season that ENDED before `season_year` — i.e. season_year - 1.

    Two questions about a draft are really questions about the season that just
    finished, and both break the same way once the draft data has been migrated onto
    the season it describes:

      - the round-2+ ORDER is the reverse of the final standings;
      - whether a submitted keeper still COUNTS is "did that manager still hold him
        at the final gameweek".

    Pre-rollover the answer is the passed row itself (the 2026 draft ran on the 25/26
    row, whose own season_year is 2025), so this is a no-op on the historical path and
    on every archived season re-read on its own row.

    It matters post-migration. The 26/27 row's own standings are the 26/27 season's —
    at preseason, ten rows of zeroes — so ordering by them doesn't fail loudly, it
    yields a plausible-looking WRONG order. And the row has no gameweeks at all yet,
    so `effective_owner` returns an empty map and every keeper selection reads as
    "no longer owned", handing all ten managers a full un-reduced board.

    Falls back to `league` when no prior row exists (a first season).
    """
    prior = (
        db.query(League)
        .filter(League.season_year == (season_year or 0) - 1)
        .order_by(League.is_current.desc())
        .first()
    )
    return prior or league


def _manager_bridge(db: Session, src: League, dst: League) -> dict:
    """{src manager uuid: dst manager uuid} — the same PERSON on two league rows.

    `managers` has one row per manager per season, so any answer computed on one row
    has to be translated before it can be compared against ids on another. Empty
    (and the caller skips the translation) when both are the same row.

    **`display_name` first, `fpl_manager_id` only as a fallback.** The entry id is
    NOT stable across seasons — FPL reissued all ten at the 26/27 rollover, overlap
    zero. An entry-id-only bridge therefore returns an EMPTY map across exactly the
    boundary it exists to cross, and every caller then silently sees nothing: it made
    `effective_keeper_selections` drop all 50 migrated selections (ten full 15-slot
    boards with kept players draftable) and `_reverse_standings_managers` return an
    empty order (a 10-slot board instead of 150). The fallback still covers a row
    whose person names aren't filled in yet, where the entry id is all there is.
    """
    if src.id == dst.id:
        return {}
    dst_rows = db.query(Manager).filter_by(league_id=dst.id).all()
    by_person = {
        (m.display_name or "").strip().casefold(): m.id
        for m in dst_rows if (m.display_name or "").strip()
    }
    by_fpl = {m.fpl_manager_id: m.id for m in dst_rows}

    out = {}
    for m in db.query(Manager).filter_by(league_id=src.id):
        person = (m.display_name or "").strip().casefold()
        target = by_person.get(person) if person else None
        if target is None:
            target = by_fpl.get(m.fpl_manager_id)
        if target is not None:
            out[m.id] = target
    return out


def _reverse_standings_managers(
    db: Session, league: League, standings_league: League | None = None
) -> list[Manager]:
    """Draft order for rounds 2+: worst-placed first.

    Reads the ADJUSTED standings, not the raw `Standing.rank` column. A commissioner
    deduction changes where a team finished, and the draft order is a consequence of
    where they finished — sorting on the synced rank meant the standings page and the
    draft board could disagree, which is exactly what a post-season deduction caused.
    Reuses get_standings rather than re-merging the deltas here, so there is one
    definition of "the standings" and the tie-breaks can't drift apart.

    `standings_league` reads the ORDER off a different row than the one whose managers
    are returned (see _prior_season_league). The bridge is `fpl_manager_id`, the
    stable identity — `managers` has one row per manager per season, so the finishing
    order from last season's row has to be mapped back onto this row's manager uuids
    or every id would be a stranger to the board.
    """
    src = standings_league or league
    here = {m.id: m for m in db.query(Manager).filter_by(league_id=league.id)}
    # `get_standings` yields the SOURCE row's entry ids, so when the order comes off
    # another row it has to be translated through _manager_bridge (person name first
    # — entry ids are reissued between seasons, and keying on them here returned an
    # EMPTY order, which silently collapsed the migrated 2026 board from 150 slots to
    # the 10 the lottery alone produced).
    src_by_fpl = {
        m.fpl_manager_id: m.id for m in db.query(Manager).filter_by(league_id=src.id)
    }
    bridge = _manager_bridge(db, src, league)

    ordered = []
    for row in get_standings(db, src):                # best first, adjusted
        src_id = src_by_fpl.get(row.get("fpl"))
        if src_id is None:
            continue
        target_id = bridge.get(src_id, src_id)        # identity when same row
        m = here.get(target_id)
        if m is not None:
            ordered.append(m)
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


# Lowercase keys only — see _fold_name for why the application order matters.
_FOLD_TRANSLIT = str.maketrans({"ø": "o", "đ": "d", "ı": "i", "ł": "l",
                                "æ": "ae", "ß": "ss", "þ": "th"})


def _fold_name(s: str) -> str:
    """One name (or team) -> its ASCII-folded spelling, for <datalist> aliases.

    The order is load-bearing: lower() FIRST (the translation table has lowercase
    keys only), translate SECOND, NFKD THIRD. NFKD has no decomposition for ø or ı,
    which is why the table exists at all.

    Ported from scripts/import_projections.py:_norm, with one deliberate difference:
    that one ends in re.sub(r"[^a-z]", "", s) because it builds a match KEY. This
    builds text a human types into a picker, so spaces and punctuation stay.

    Fold each part SEPARATELY and rejoin — never fold a "Name . Team" label whole.
    The separator (U+00B7) is dropped by the ascii encode, so folding the whole
    label leaves a double space that merely LOOKS like a separator; restoring it by
    regex would also invent one inside any name containing two spaces.

    Deliberately NOT unified with history_import._norm (plain NFKD), which is
    load-bearing for keeper seeds that are already imported."""
    s = (s or "").lower().translate(_FOLD_TRANSLIT)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _picker_label(p: Player) -> tuple[str, str | None]:
    """(label, alias) for one player in a name picker. `label` disambiguates duplicate
    names by club; `alias` is its ASCII-folded spelling, or None when folding changes
    nothing — emitting an alias option for "Haaland" would just duplicate every row.

    Fold part-wise and rejoin with the real separator; see _fold_name for why folding
    the joined label instead is wrong."""
    parts = [p.name, p.current_team] if p.current_team else [p.name]
    label = " · ".join(parts)
    alias = " · ".join(_fold_name(x) for x in parts)
    # _fold_name lowercases, so compare lowered: casing alone is not a difference the
    # browser's <datalist> matching cares about.
    return label, (alias if alias != label.lower() else None)


def list_players(db: Session, league: League) -> list[dict]:
    """All players as [{fpl_id, label, alias}] for name-based pickers.

    NOTE `fpl_id` is THIS SEASON's element id and is None for anyone who has left the
    PL — so this list can name a player the form then can't submit. That's the open
    "IL backfill form must search by player name, not FPL id" backlog item; until it
    lands, callers that must handle departed players resolve by LABEL instead (see
    resolve_player_by_label), which needs no element id at all.
    """
    out = []
    for p in db.query(Player).order_by(Player.name).all():
        label, alias = _picker_label(p)
        out.append({"fpl_id": p.fpl_id, "label": label, "alias": alias})
    return out


def _condition_spec(
    db: Session, *, condition_logic, condition_effect, pick_round_if_met, condition_terms,
) -> tuple[dict, list[dict]]:
    """Validate a proposed pick CLAUSE plus its TERMS.

    Returns `(clause_columns, term_kwargs)` — the three columns to set on the Trade row
    and the rows to create in trade_condition_terms. Returns all-NULL and an empty list
    when no condition is being set, so an ordinary pick trade takes exactly the path it
    always did.

    Purity split, matching the rest of this module: the shape rules live in
    rules.validate_condition_term / rules.validate_pick_condition, and the only work
    done here is what genuinely needs the database (resolving a player id).

    `condition_terms` is a list of dicts, one per term. A single-term clause — still by
    far the common case — is a one-element list; nothing about the API special-cases it.
    """
    blank = {
        "condition_logic": None, "condition_effect": None, "pick_round_if_met": None,
    }
    terms = [t for t in (condition_terms or []) if t]
    if not terms and not condition_logic and not condition_effect:
        return blank, []

    # Default rather than require: `all` over one term is what every pre-terms
    # condition was, and escalate_round is what every one of them did.
    logic = condition_logic or CONDITION_LOGIC_ALL
    effect = condition_effect or CONDITION_EFFECT_ESCALATE
    validate_pick_condition(
        logic=logic, effect=effect,
        round_if_met=pick_round_if_met, term_count=len(terms),
    )

    out: list[dict] = []
    for raw in terms:
        metric = raw.get("metric")
        name = (raw.get("manager_name") or "").strip() or None
        note = (raw.get("note") or "").strip() or None
        player_id = raw.get("player_id")
        validate_condition_term(
            metric=metric,
            player_subject=player_id is not None,
            manager_subject=name is not None,
            season_year=raw.get("season_year"),
            comparison=raw.get("comparison"),
            threshold=raw.get("threshold"),
            note=note,
        )
        if player_id is not None and not db.get(Player, player_id):
            raise RuleViolation("the condition names a player who isn't in the pool")
        # Comparison/threshold are meaningless for the two boolean cup metrics, and a
        # manual term has no structured subject at all. Stored as NULL rather than as
        # whatever the form happened to submit, so a later reader can't mistake a
        # leftover ">= 1" for part of the rule.
        scoped = metric in CONDITION_THRESHOLD_METRICS
        manual = metric == CONDITION_MANUAL
        out.append({
            "metric": metric,
            "player_id": None if manual else player_id,
            "manager_name": None if manual else name,
            "season_year": None if manual else raw.get("season_year"),
            "comparison": raw.get("comparison") if scoped else None,
            "threshold": raw.get("threshold") if scoped else None,
            "note": note,
            "manual_state": raw.get("manual_state") if manual else None,
        })
    return (
        {
            "condition_logic": logic,
            "condition_effect": effect,
            # NULL for transfer_if_met, which validate_pick_condition has already
            # refused to accept a round for.
            "pick_round_if_met": pick_round_if_met,
        },
        out,
    )


def _write_condition_terms(db: Session, trade, term_kwargs: list[dict]) -> None:
    """Replace a trade's condition terms with `term_kwargs`.

    Delete-then-insert rather than a per-row diff: a clause is validated as a SET, and
    there is no stable identity for "the same term, edited" that a form could send
    back. The CASCADE on the FK is for trade deletion, not this.
    """
    from models import TradeConditionTerm

    db.query(TradeConditionTerm).filter(
        TradeConditionTerm.trade_id == trade.id
    ).delete(synchronize_session=False)
    for kw in term_kwargs:
        db.add(TradeConditionTerm(trade_id=trade.id, **kw))


def condition_term_from_flat(
    *, metric, player_id=None, manager_name=None, season_year=None,
    comparison=None, threshold=None, note=None,
) -> dict:
    """One term as a dict, for callers that collect a form's flat fields.

    Exists so the routes don't each hand-assemble a dict and drift on key names.
    """
    return {
        "metric": metric, "player_id": player_id, "manager_name": manager_name,
        "season_year": season_year, "comparison": comparison,
        "threshold": threshold, "note": note,
    }


def resolve_player_by_label(db: Session, league: League, label: str):
    """A picker label ("Name · Team") or its folded alias -> the Player row.

    The counterpart to list_players, for the forms that post a NAME rather than a
    hidden id. Accepts either spelling, so the accent alias works here too. Raises
    RuleViolation rather than returning None: every caller is a write path, and
    silently dropping an unmatched subject would save a condition with no subject.
    """
    want = (label or "").strip().lower()
    if not want:
        raise RuleViolation("no player named")
    for p in db.query(Player).order_by(Player.name).all():
        lab, alias = _picker_label(p)
        if want in (lab.strip().lower(), (alias or "").strip().lower()):
            return p
    raise RuleViolation(f"no player matching {label!r}")


def trade_pick(
    db: Session, league: League, *, from_fpl: str, to_fpl: str, original_fpl: str,
    round: int, season_year: int, draft_type: str = "main",
    condition_logic: str | None = None,
    condition_effect: str | None = None,
    pick_round_if_met: int | None = None,
    condition_terms: list[dict] | None = None,
    conditions: str | None = None,
) -> dict:
    """Record a draft-pick trade (commissioner-entered, live). Reassigns ownership
    of the (season, draft_type, round) slot originally belonging to original_fpl.

    Refuses to leave the seller with no way to get a goalie team. Trading away your
    last slot after thirteen outfielders is the same dead end `record_pick` guards
    against, reached by a different door.

    `condition_terms` (with `condition_logic` / `condition_effect`) makes the pick
    CONDITIONAL. Under `escalate_round`, `round` stays the base round and the pick
    becomes `pick_round_if_met` once the condition resolves met; under
    `transfer_if_met` the pick moves only once it does. All optional — omitting them
    is byte-identical to before.

    `conditions` is the deal's free text, kept verbatim so the words of an agreement
    survive even when a clause is only partly expressible in terms.
    """
    frm = _resolve_manager(db, league, from_fpl)
    to = _resolve_manager(db, league, to_fpl)
    orig = _resolve_manager(db, league, original_fpl)
    stranded = _goalie_team_required_reason(
        db, league, frm, season_year=season_year, draft_type=draft_type,
        pick_number=None,
    )
    if stranded:
        raise RuleViolation(f"{stranded} — trading it away would strand them")
    cond, terms = _condition_spec(
        db, condition_logic=condition_logic, condition_effect=condition_effect,
        pick_round_if_met=pick_round_if_met, condition_terms=condition_terms,
    )
    label = f"{season_year} {draft_type} R{round} (orig {orig.name})"
    if cond["condition_effect"] == CONDITION_EFFECT_ESCALATE:
        label += f" -> R{cond['pick_round_if_met']} if met"
    elif cond["condition_effect"] == CONDITION_EFFECT_TRANSFER:
        label += " if met"
    row = Trade(
        league_id=league.id, from_manager=frm.id, to_manager=to.id,
        pick_original_manager=orig.id, pick_round=round,
        pick_season_year=season_year, pick_draft_type=draft_type, draft_pick=label,
        conditions=(conditions or "").strip() or None,
        **cond,
    )
    db.add(row)
    # Flush before writing terms: `Trade.id` uses a PYTHON-side `default=uuid.uuid4`,
    # which SQLAlchemy applies at INSERT time, not at construction — so `row.id` is
    # still None here without this, and every term would fail the NOT NULL on trade_id.
    db.flush()
    _write_condition_terms(db, row, terms)
    record_audit(db, league, action="trade.pick",
                 summary=f"Pick trade: {frm.display} → {to.display} ({label})",
                 manager_ids=[frm.id, to.id, orig.id],
                 details={"round": round, "season_year": season_year,
                          "draft_type": draft_type,
                          "terms": len(terms) or None,
                          **{k: (str(v) if v is not None else None)
                             for k, v in cond.items() if v is not None}})
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


def _squad_quota_reason(
    db: Session, league: League, owner: Manager, player, *,
    season_year: int, draft_type: str, pick_number: int,
) -> str | None:
    """Why this manager can't take this player — a full position — or None.

    Counted from THIS season's main-draft picks plus this manager's keepers, since a
    kept player occupies a squad slot exactly as a drafted one does. The slot being
    filled right now is excluded, so re-recording a pick over itself is not refused as
    a duplicate.

    Discovery picks are excluded: a discovery player isn't in the PL yet and doesn't
    occupy a squad position until he arrives.

    Like `_goalie_team_required_reason`, deliberately NOT folded into
    `_unavailable_reason` — that doubles as search's taken-oracle, and a defender is
    not "taken" because YOUR back line is full.
    """
    if draft_type != "main":
        return None
    # The two agree on DEF/MID/FWD today — OUTFIELD_POSITION_LIMITS is derived from
    # SQUAD_POSITION_LIMITS — so this branch currently changes nothing except that the
    # outfield shape has no GKP entry, and keepers are off the board entirely under
    # goalie-team mode anyway. Kept because it states which rule applies, and because
    # the two would diverge the moment either shape is edited. A mutation swapping them
    # does NOT fail any test, which is expected rather than a coverage gap.
    limits = (
        OUTFIELD_POSITION_LIMITS
        if goalie_teams_on(league.goalie_team_mode)
        else SQUAD_POSITION_LIMITS
    )
    # Position by players.id (stable across seasons via `code`), preferring this
    # season's snapshot and FALLING BACK to the global row.
    #
    # The fallback is load-bearing, not defensive tidiness: a season with no
    # PlayerSeason rows yet — a fresh rollover, or any test that seeds only `Player` —
    # would otherwise count zero of everything and silently enforce nothing. That is
    # exactly how this check first shipped, and it passed the very test meant to prove
    # it worked. Unlike the SCORING path, where recycled element ids make PlayerSeason
    # mandatory, a draft is always about the current season, so the global row is the
    # same human at the same position.
    by_uuid = {
        ps.player_id: ps.position
        for ps in _season_by_fpl_id(db, league).values()
        if ps.position
    }

    counts: dict = {}
    def _bump(player_uuid):
        pos = by_uuid.get(player_uuid)
        if pos is None:
            row = db.get(Player, player_uuid)
            pos = row.position if row else None
        if pos:
            counts[pos.upper()] = counts.get(pos.upper(), 0) + 1

    for dp in (
        db.query(DraftPick)
        .filter(DraftPick.league_id == league.id,
                DraftPick.season_year == season_year,
                DraftPick.draft_type == "main",
                DraftPick.manager_id == owner.id,
                DraftPick.player_id.isnot(None))
    ):
        if dp.pick_number == pick_number:
            continue          # the slot being (re)filled right now
        _bump(dp.player_id)
    for sel in (
        db.query(KeeperSelection)
        .filter(KeeperSelection.league_id == league.id,
                KeeperSelection.manager_id == owner.id,
                KeeperSelection.season_year == season_year)
    ):
        _bump(sel.player_id)

    position = by_uuid.get(player.id) or player.position or ""
    return squad_quota_reason(position, counts, limits=limits)


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
        full = _squad_quota_reason(
            db, league, owner, player, season_year=season_year,
            draft_type=draft_type, pick_number=pick_number,
        )
        if full:
            raise RuleViolation(f"{player.name} can't be picked — {full}")
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


# ---- conditional pick trades ------------------------------------------------
# A traded pick can carry a condition with one of two effects: ESCALATE its round
# ("my 2nd, upgraded to my 1st if Kevin T finishes top 3 in 2027") or gate the
# TRANSFER itself ("my 2027 1st discovery pick, but only if Cunha stays under three
# red cards"). Resolved fresh on every read here and never written back onto
# pick_round (see the Trade model comment for why).
#
# A condition is a CLAUSE on the Trade row (condition_logic + condition_effect) over
# TERMS in trade_condition_terms. Both effects fold cleanly into pick_ownership
# because neither writes a value that would later have to be undone: escalate_round
# changes the KEY, transfer_if_met changes WHETHER the fold writes at all.
#
# Everything below reads. The write path is trade_pick/edit_trade + the pure
# rules.validate_condition_term / rules.validate_pick_condition.


def _condition_league(db: Session, season_year: int | None) -> League | None:
    """The league row a condition resolves against — the row FOR that season, which
    is not the row the trade was entered on. A condition entered in 2026 about the
    2027 season resolves against the 2027 row, which may not exist yet."""
    if season_year is None:
        return None
    return db.query(League).filter_by(season_year=season_year).first()


def _resolve_cup_winner_name(db: Session, league: League, cup_name: str) -> str | None:
    """Winner of "Cup" / "Pup Cup" for `league`'s season, as a Manager.display name.

    Mirrors get_payouts' cup resolution exactly — live bracket first, then the
    imported season_history fallback for a season whose cups were never run in-app.
    Returns None while a bracket exists but its decisive match is unscored, which is
    what keeps a condition `pending` rather than answering "didn't win it".

    Kept separate from get_payouts rather than shared with it: that function resolves
    FOUR recipients (cup 1st/2nd/3rd + pup) into manager IDS for money, this one
    resolves one champion into a NAME for a text comparison. A future fifth metric
    (the Pupmunity Shield) extends this helper, not get_payouts.
    """
    t = _get_tournament(db, league, cup_name)
    winner_id = None
    if t:
        if cup_name == "Cup":
            final, _third = _cup_final_and_third(db, t)
            winner_id = final.winner_id if final else None
        else:
            r3 = _round_matches(db, t, 3)
            winner_id = r3[0].winner_id if r3 else None
        if not winner_id:
            return None          # bracket exists, not decided yet -> pending
    else:
        # No live bracket at all: fall back to the imported history, exactly as
        # get_payouts does for a past season.
        hist_cup, hist_pup = _historical_cup_winners(db, league)
        winner_id = hist_cup if cup_name == "Cup" else hist_pup
    if not winner_id:
        return None
    m = db.get(Manager, winner_id)
    return m.display if m else None


def _same_person(a: str | None, b: str | None) -> bool:
    """Person-name equality for a stored condition subject. Case/space-insensitive,
    the same rule _historical_cup_winners already uses to match a sheet name to a
    Manager across seasons."""
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()


# ---- Discord ingest queue ----------------------------------------------------
# Proposals parsed from Discord. NOTHING here applies itself — see the DiscordIngest
# docstring for why that is permanent rather than cautious. These functions are the
# human's side of the loop.

def discord_ingest_queue(db: Session, league: League) -> list[dict]:
    """Pending Discord proposals, newest first, with everything the reviewer needs."""
    from models import DiscordIngest, DiscordMessage

    rows = (
        db.query(DiscordIngest, DiscordMessage)
        .join(DiscordMessage, DiscordMessage.id == DiscordIngest.discord_message_id)
        .filter(DiscordIngest.league_id == league.id,
                DiscordIngest.status == "pending")
        .order_by(DiscordMessage.posted_at.desc().nullslast(),
                  DiscordIngest.created_at.desc())
        .all()
    )
    return [
        {
            "id": str(ing.id), "kind": ing.kind, "confidence": ing.confidence,
            "payload": ing.payload or {}, "resolution": ing.resolution or {},
            "error": ing.error,
            "posted_at": msg.posted_at, "author": msg.author_name,
            "content": msg.content,
        }
        for ing, msg in rows
    ]


def unmapped_discord_authors(db: Session, league: League) -> list[dict]:
    """Posters we have seen but cannot identify, with a count of their messages.

    The mapping is what makes the whole inbound half safe (see Manager.discord_user_id),
    so an unmapped author is a real gap rather than a curiosity: every IL post they
    write will stage without a manager and need one typed in by hand.
    """
    from models import DiscordMessage

    mapped = {
        m.discord_user_id for m in db.query(Manager).filter_by(league_id=league.id)
        if m.discord_user_id
    }
    counts: dict = {}
    for did, name in (
        db.query(DiscordMessage.author_discord_id, DiscordMessage.author_name)
        .filter(DiscordMessage.league_id == league.id,
                DiscordMessage.author_discord_id.isnot(None))
    ):
        if did in mapped:
            continue
        entry = counts.setdefault(did, {"discord_user_id": did, "name": name, "messages": 0})
        entry["messages"] += 1
        entry["name"] = entry["name"] or name
    return sorted(counts.values(), key=lambda e: -e["messages"])


def map_discord_author(
    db: Session, league: League, *, fpl_manager_id: str, discord_user_id: str
) -> dict:
    """Bind a Discord account to a manager. Blank `discord_user_id` unmaps."""
    manager = _resolve_manager(db, league, fpl_manager_id)
    value = (discord_user_id or "").strip() or None
    if value:
        clash = (
            db.query(Manager)
            .filter(Manager.league_id == league.id,
                    Manager.discord_user_id == value,
                    Manager.id != manager.id)
            .first()
        )
        if clash:
            raise RuleViolation(
                f"that Discord account is already mapped to {clash.display}"
            )
    manager.discord_user_id = value
    record_audit(db, league, action="discord.map",
                 summary=f"Mapped a Discord account to {manager.display}",
                 manager_ids=[manager.id],
                 details={"discord_user_id": value})
    db.commit()
    return {"manager": manager.display, "discord_user_id": value}


def _ingest_or_404(db: Session, league: League, ingest_id: str):
    from models import DiscordIngest

    row = (
        db.query(DiscordIngest)
        .filter(DiscordIngest.id == ingest_id,
                DiscordIngest.league_id == league.id)
        .one_or_none()
    )
    if not row:
        raise RuleViolation("proposal not found")
    return row


def apply_discord_ingest(db: Session, league: League, ingest_id: str, **overrides) -> dict:
    """Confirm a proposal, applying the reviewer's corrections on top of the parse.

    The real service function runs FIRST and only then is the row marked applied, so a
    refused write leaves the proposal pending rather than recording a decision that
    never happened — the ordering `confirm_discovery_suggestion` documents.

    A RuleViolation is CAPTURED on the row rather than swallowed: it means Discord and
    the league's actual state disagree, which is worth seeing.
    """
    row = _ingest_or_404(db, league, ingest_id)
    if row.status == "applied":
        raise RuleViolation("that proposal has already been applied")
    payload = dict(row.payload or {})
    payload.update({k: v for k, v in overrides.items() if v is not None})

    try:
        if row.kind == "il_place":
            missing = [k for k in ("fpl_manager_id", "injured_fpl_id",
                                   "replacement_fpl_id", "start_gw")
                       if payload.get(k) in (None, "")]
            if missing:
                # The replacement is the expected one — no IL announcement contains it.
                raise RuleViolation(f"still needs: {', '.join(missing)}")
            result = place_on_il(
                db, league,
                fpl_manager_id=str(payload["fpl_manager_id"]),
                injured_fpl_id=int(payload["injured_fpl_id"]),
                replacement_fpl_id=int(payload["replacement_fpl_id"]),
                start_gw=int(payload["start_gw"]),
            )
            # _il_to_dict stringifies the PK; the column is a UUID.
            entity_id = uuid.UUID(result["id"]) if result.get("id") else None
        elif row.kind == "trade":
            missing = [k for k in ("a_fpl", "b_fpl") if not payload.get(k)]
            if missing:
                raise RuleViolation(f"still needs: {', '.join(missing)}")
            result = record_trade(
                db, league,
                a_fpl=str(payload["a_fpl"]), b_fpl=str(payload["b_fpl"]),
                a_players=list(payload.get("a_players") or []),
                b_players=list(payload.get("b_players") or []),
                a_picks=list(payload.get("a_picks") or []),
                b_picks=list(payload.get("b_picks") or []),
            )
            entity_id = None
        else:
            raise RuleViolation(f"unknown proposal kind {row.kind!r}")
    except RuleViolation as exc:
        row.status = "failed"
        row.error = str(exc)
        db.commit()
        raise

    row.status = "applied"
    row.error = None
    row.payload = payload
    if entity_id:
        row.applied_entity_id = entity_id
    record_audit(db, league, action="discord.apply",
                 summary=f"Applied a Discord proposal ({row.kind})",
                 details={"ingest_id": str(row.id), "kind": row.kind})
    db.commit()
    return {"applied": row.kind, "result": result}


def reject_discord_ingest(db: Session, league: League, ingest_id: str) -> dict:
    """Dismiss a proposal. The row is KEPT, never deleted, so re-parsing the same
    message never proposes it again — the discovery-suggestion rule."""
    row = _ingest_or_404(db, league, ingest_id)
    row.status = "rejected"
    record_audit(db, league, action="discord.reject",
                 summary=f"Dismissed a Discord proposal ({row.kind})",
                 details={"ingest_id": str(row.id), "kind": row.kind})
    db.commit()
    return {"rejected": str(row.id)}


def set_condition_term_state(db: Session, league: League, term_id: str, state) -> dict:
    """Rule on a `manual` condition term. `state` is 'met', 'not_met', or None (undecided).

    Only manual terms are settable: an evaluable metric is answered by the data, and
    letting a human override it would make the draft board disagree with the standings
    page for reasons nothing records.
    """
    from models import TradeConditionTerm

    if state not in (None, CONDITION_MET, CONDITION_NOT_MET):
        raise RuleViolation(f"unknown condition state {state!r}")
    term = db.get(TradeConditionTerm, term_id)
    if not term:
        raise RuleViolation("condition term not found")
    if term.metric != CONDITION_MANUAL:
        raise RuleViolation(
            f"a {term.metric} condition resolves from the data, not by hand"
        )
    previous = term.manual_state
    term.manual_state = state
    record_audit(
        db, league, action="trade.condition.term",
        summary=f"Ruled a manual trade condition {state or 'undecided'}",
        details={"term_id": str(term.id), "trade_id": str(term.trade_id),
                 "note": term.note, "previous": previous, "state": state},
    )
    db.commit()
    return {"term": str(term.id), "state": state}


def _condition_terms(db: Session, t: Trade) -> list:
    """This trade's condition terms, oldest first so a note reads in entry order."""
    from models import TradeConditionTerm

    return (
        db.query(TradeConditionTerm)
        .filter(TradeConditionTerm.trade_id == t.id)
        .order_by(TradeConditionTerm.created_at, TradeConditionTerm.id)
        .all()
    )


def _resolve_term(db: Session, term, cache: dict | None = None) -> dict:
    """{"state": pending|met|not_met, "current": <provisional reading or None>} for ONE
    condition term.

    An evaluable term is resolved ONLY once its season's league row is frozen
    (`sync_locked`), uniformly for all four metrics — a mid-season points total, an
    in-progress bracket and a live table are all equally provisional, and the
    `stats_season` split already draws this line elsewhere. Until then the state is
    `pending` and the BASE round stands.

    A `manual` term has no season to freeze and no metric to read: it is whatever the
    commissioner has ruled, and `pending` until they rule. That is the same "an
    unanswered condition leaves the base round in force" rule, reached a different way.

    `current` is a human string for the note ("5th", "142 pts") read even while
    pending, so the board can say what a condition is currently tracking without
    implying it has resolved. `cache` memoizes the standings/cup/player lookups across
    the terms of one clause and across the rows of one multi-row deal.
    """
    cache = cache if cache is not None else {}
    metric = term.metric

    if metric == CONDITION_MANUAL:
        # No auto-resolution by design — see the TradeConditionTerm docstring.
        return {"state": term.manual_state or CONDITION_PENDING, "current": None}

    lg = _condition_league(db, term.season_year)
    if lg is None:
        return {"state": CONDITION_PENDING, "current": None}

    def _cached(key, fn):
        if key not in cache:
            cache[key] = fn()
        return cache[key]

    resolved = None      # True/False once we can answer
    current = None
    if metric == "total_points":
        ps = _cached(
            ("ident", lg.id, term.player_id),
            lambda: season_identity(db, lg, [term.player_id]).get(term.player_id),
        )
        pts = ps.total_points if ps else None
        current = f"{pts} pts" if pts is not None else None
        resolved = compare_condition(term.comparison, pts, term.threshold)
    elif metric == "league_finish":
        rows = _cached(("standings", lg.id), lambda: get_standings(db, lg))
        row = next(
            (r for r in rows if _same_person(r.get("manager"), term.manager_name)),
            None,
        )
        rank = row.get("rank") if row else None
        current = _ordinal(rank) if rank else None
        resolved = compare_condition(term.comparison, rank, term.threshold)
    elif metric in ("cup_win", "pup_cup_win"):
        name = "Cup" if metric == "cup_win" else "Pup Cup"
        winner = _cached(("cup", lg.id, name), lambda: _resolve_cup_winner_name(db, lg, name))
        current = winner
        # An undecided bracket is NOT "didn't win it" — hold at pending. Distinct
        # from a decided cup somebody else won, which is a real not_met.
        if winner is None:
            return {"state": CONDITION_PENDING, "current": None}
        resolved = _same_person(winner, term.manager_name)
    else:
        # Unknown metric (hand-edited row / a metric added and later removed). Never
        # guess: leave the base round in force.
        return {"state": CONDITION_PENDING, "current": None}

    if not lg.sync_locked:
        return {"state": CONDITION_PENDING, "current": current}
    return {"state": CONDITION_MET if resolved else CONDITION_NOT_MET, "current": current}


def _resolve_condition(db: Session, t: Trade, cache: dict | None = None) -> dict:
    """{"state": pending|met|not_met, "current": <provisional reading or None>} for a
    conditional pick trade — its terms folded under the row's `condition_logic`.

    Signature and return shape are unchanged from the single-condition version, so
    every caller is untouched by the move to terms.

    The fold short-circuits on the decisive answer before considering pending terms
    (rules.combine_condition_states), which is what lets a four-way OR with one
    unevaluable branch still resolve.
    """
    cache = cache if cache is not None else {}
    if not t.condition_logic:
        return {"state": CONDITION_PENDING, "current": None}
    results = [_resolve_term(db, term, cache) for term in _condition_terms(db, t)]
    currents = [r["current"] for r in results if r["current"]]
    return {
        "state": combine_condition_states(t.condition_logic, [r["state"] for r in results]),
        # Joined rather than picked: with one term this is byte-identical to before,
        # and with several the reader wants every live number, not an arbitrary one.
        "current": ", ".join(currents) or None,
    }


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _term_subject_name(db: Session, term) -> str:
    """A term's subject as a display string — a player name for total_points, the
    stored person name otherwise."""
    if term.metric in CONDITION_PLAYER_METRICS:
        if not term.player_id:
            return "?"
        p = db.get(Player, term.player_id)
        return p.name if p else "?"
    return term.manager_name or "?"


_FINISH_PHRASE = {
    "<=": lambda n: f"finishes top {n}",
    "<": lambda n: f"finishes better than {_ordinal(n)}",
    ">=": lambda n: f"finishes {_ordinal(n)} or worse",
    ">": lambda n: f"finishes worse than {_ordinal(n)}",
}
_POINTS_PHRASE = {
    ">=": lambda n: f"scores {n}+ points",
    ">": lambda n: f"scores more than {n} points",
    "<=": lambda n: f"scores {n} or fewer points",
    "<": lambda n: f"scores fewer than {n} points",
}


def _term_phrase(db: Session, term) -> str:
    """One term as human text: "Kevin T finishes top 3 in 2026".

    A manual term is quoted as written rather than paraphrased — the commissioner's
    words are the rule, and rewording them would misstate an agreement between people.
    """
    if term.metric == CONDITION_MANUAL:
        return (term.note or "").strip() or "an unrecorded condition"
    who = _term_subject_name(db, term)
    metric = term.metric
    if metric == "total_points":
        what = _POINTS_PHRASE.get(term.comparison, lambda n: f"reaches {n} points")(
            term.threshold
        )
    elif metric == "league_finish":
        what = _FINISH_PHRASE.get(term.comparison, lambda n: f"finishes {n}")(term.threshold)
    elif metric == "cup_win":
        what = "wins the Cup"
    elif metric == "pup_cup_win":
        what = "wins the Pup Cup"
    else:
        what = "meets an unrecognized condition"
    return f"{who} {what} in {term.season_year}"


def _condition_note(db: Session, t: Trade, state: str, current: str | None) -> str:
    """One human sentence describing the condition and where it stands.

    The single-term `escalate_round` wording is preserved exactly as it was before
    terms existed — that is the overwhelmingly common shape, and its phrasing is
    pinned by test_condition_note_is_phrased_per_metric.
    """
    terms = _condition_terms(db, t)
    joiner = " or " if t.condition_logic == CONDITION_LOGIC_ANY else " and "
    what = joiner.join(_term_phrase(db, term) for term in terms) or "an empty condition"
    if t.condition_effect == CONDITION_EFFECT_TRANSFER:
        note = f"transfers only if {what}"
    else:
        note = f"upgrades to R{t.pick_round_if_met} if {what}"
    label = {CONDITION_MET: "met", CONDITION_NOT_MET: "not met"}.get(state, "pending")
    note += f" — {label}"
    if current and state == CONDITION_PENDING:
        note += f" (currently {current})"
    return note


def _effective_pick_round(db: Session, t: Trade, cache: dict | None = None) -> int | None:
    """The round this pick trade actually moves — `pick_round_if_met` once the
    condition is met, the base `pick_round` otherwise (including while pending).

    Only the `escalate_round` effect touches the round. `transfer_if_met` leaves it
    alone and is applied by pick_ownership skipping the row entirely.

    Note this changes the KEY the ownership fold writes to, not a value: the
    base-round slot simply never gets reassigned, so it stays with its original owner
    with nothing to undo.
    """
    if t.condition_effect != CONDITION_EFFECT_ESCALATE or t.pick_round_if_met is None:
        return t.pick_round
    state = _resolve_condition(db, t, cache)["state"]
    return t.pick_round_if_met if state == CONDITION_MET else t.pick_round


def _condition_applies(db: Session, t: Trade, cache: dict | None = None) -> bool:
    """Does this pick trade move the pick AT ALL?

    True for every ordinary row and for `escalate_round` (which moves the pick either
    way, just into a different round). False only for an unmet `transfer_if_met`,
    whose whole point is that the transfer is contingent.
    """
    if t.condition_effect != CONDITION_EFFECT_TRANSFER:
        return True
    return _resolve_condition(db, t, cache)["state"] == CONDITION_MET


def pick_conditions(
    db: Session, league: League, season_year: int, draft_type: str = "main"
) -> dict:
    """{(effective_round, original_owner_person): {...}} for CONDITIONAL pick trades
    in this draft — the display sibling of pick_ownership.

    Keyed the same way pick_ownership is, and on the EFFECTIVE round, so a slot's
    pill lands on the slot the pick actually occupies right now: the base round while
    pending or unmet, the upgraded round once met. Latest entry wins on a collision,
    the same created_at rule as the ownership fold.
    """
    person_by_id = {m.id: m.display for m in db.query(Manager)}
    cache: dict = {}
    out: dict = {}
    for t in (
        db.query(Trade)
        .filter(Trade.pick_round.isnot(None),
                Trade.condition_logic.isnot(None),
                Trade.pick_season_year == season_year,
                Trade.pick_draft_type == draft_type)
        .order_by(Trade.created_at, Trade.id)
        .all()
    ):
        orig = person_by_id.get(t.pick_original_manager)
        if not orig:
            continue
        res = _resolve_condition(db, t, cache)
        rnd = _effective_pick_round(db, t, cache)
        out[(rnd, orig)] = {
            "conditional": True,
            "condition_status": res["state"],
            "condition_note": _condition_note(db, t, res["state"], res["current"]),
        }
    return out


def pick_ownership(
    db: Session, league: League, season_year: int, draft_type: str = "main"
) -> dict:
    """SINGLE SOURCE OF TRUTH for who owns each pick in a draft year. Returns
    {(round, original_owner_person): current_owner_person} for picks that have
    changed hands. Built from the imported baseline (future_picks) + recorded
    pick trades (trades table), applied in order (latest reassignment wins).
    Shared by the draft board and the future-picks grid so they never disagree.

    Reads across EVERY league row, not just `league`. A pick trade is stored where
    it happened and a FuturePick is season-agnostic, so neither moves when a draft
    is migrated onto the season it describes — and while this was league-scoped, the
    migrated 2026 board found 0 reassignments instead of 28 and flagged all 28
    completed picks as `reassigned`, i.e. "the order moved under a pick already
    made". The owners displayed were still right (get_draft_board takes those from
    the stored DraftPick.manager_id), so this was noise rather than corruption — but
    noise on exactly the warning that exists to catch corruption. Had the migration
    happened mid-draft, the unmade traded slots would have shown the wrong owner for
    real.

    Keyed on PERSON NAME throughout, which is what makes crossing rows safe:
    `managers` has one row per manager per season, so the names map has to span every
    row too. The `league` parameter is kept for signature stability and is unused.
    """
    from models import FuturePick

    person_by_id = {m.id: m.display for m in db.query(Manager)}
    reassigned: dict = {}
    # Baseline (imported net ownership from the sheet), oldest league row first so a
    # newer row's entry for the same (round, original_owner) wins — the same rule
    # get_future_picks used to apply by merging per-row calls in that order.
    for fp, _sy in (
        db.query(FuturePick, League.season_year)
        .join(League, League.id == FuturePick.league_id)
        .filter(FuturePick.season_year == season_year,
                FuturePick.draft_type == draft_type)
        .order_by(League.season_year.asc())
        .all()
    ):
        reassigned[(fp.round, fp.original_owner)] = fp.owner
    # then live pick trades, in entry order (latest wins). Ordered on created_at:
    # this used to sort on Trade.id, which is a random uuid4 — so "latest wins" was
    # actually "whichever id sorted higher" the moment a pick changed hands twice.
    #
    # The round used is the EFFECTIVE one: a conditional trade whose condition is met
    # moves the upgraded round instead of the base round. That is a change of KEY, not
    # of value — the base-round slot is simply never written, so it stays with its
    # original owner. A row with pick_round_if_met NULL takes the same path as always.
    cond_cache: dict = {}
    for t in (
        db.query(Trade)
        .filter(Trade.pick_round.isnot(None),
                Trade.pick_season_year == season_year, Trade.pick_draft_type == draft_type)
        .order_by(Trade.created_at, Trade.id)
        .all()
    ):
        orig, to = person_by_id.get(t.pick_original_manager), person_by_id.get(t.to_manager)
        if orig and to:
            # An unmet `transfer_if_met` row moves nothing at all — skipping it is the
            # WHOLE effect, and it leaves the slot with its original owner without any
            # entry to undo. `escalate_round` and ordinary rows always fall through.
            if not _condition_applies(db, t, cond_cache):
                continue
            reassigned[(_effective_pick_round(db, t, cond_cache), orig)] = to
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
    moved = {pid: mid for pid, mid in owner.items() if base.get(pid) != mid}
    # A player the snapshot rosters but the overlays have RELEASED maps to None — the
    # map has to be able to say "nobody", or _effective_roster_pids would re-add him
    # from the snapshot and the season-end release would be invisible on every squad
    # page. Callers must therefore treat a None value as "not this manager's".
    moved.update({pid: None for pid in base if pid not in owner})
    return moved


def effective_owner(db: Session, league: League) -> dict:
    """{player_id: manager_id} for EVERY rostered player, trades applied. Use this
    when you need to ask "does this manager still hold him?"; player_ownership
    returns only the players who moved."""
    return _owner_maps(db, league)[1]


def _owner_maps(db: Session, league: League) -> tuple[dict, dict]:
    """(snapshot owners, owners after absences and site trades) — shared by the two
    readers above so the seeding and the folds can't drift apart.

    Two overlays, in this order: absences (additive — an IL'd or internationally-absent
    player is still held even though FPL shows his replacement), then commissioner
    trades. See docs/DESIGN_IL_OWNERSHIP.md for why the order matters and why absence
    ownership is derived on read rather than written to `rosters`."""
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

    # Absences fold BEFORE trades, and the order is load-bearing. An absentee is off the
    # FPL roster, so `owner.get(X)` is None; the trade guard below only fires when the
    # map already says the sender holds him. Fold absences second and a trade of an
    # absent player can NEVER apply — /admin/health's "site trades applied" check would
    # go red permanently and the buyer would never inherit the keeper clock. Fold them
    # first and the two compose with no special case.
    held, released = _absence_held(db, league, gw.number)
    for mid, pid in held:
        # Only if nobody rosters him. A mis-entered absence must not be able to STEAL a
        # rostered player from another manager — same fail-closed instinct as the trade
        # guard below.
        if pid not in owner:
            owner[pid] = mid
    # ...and the one subtraction: the player a manager gave up to bring an absentee back
    # after GW38, when the roster is frozen and the swap can't be made in FPL. Applied
    # only while he is still where the snapshot says, so a stale row can't strip a player
    # someone else has since acquired.
    for mid, pid in released:
        if owner.get(pid) == mid:
            del owner[pid]

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
    they end up one keeper short (they can re-submit while keepers are unlocked).

    "Still holds him" is judged on the season that ENDED (see _prior_season_league),
    which is normally this same row and, post-migration, is not. The 26/27 row has no
    gameweeks until FPL opens the season, so asking it would return an empty ownership
    map and silently drop EVERY selection — ten full un-reduced boards with kept
    players draftable, which is the exact failure the pre-rollover draft existed to
    avoid, arriving from the other direction.
    """
    src = _prior_season_league(db, league, season_year)
    owner = effective_owner(db, src)
    bridge = _manager_bridge(db, src, league)
    if bridge:
        # The map is keyed by the OTHER row's manager uuids; the selections carry
        # this row's. Compare like with like or nothing ever matches.
        owner = {pid: bridge.get(mid, mid) for pid, mid in owner.items()}
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
    # Rounds 2+ run in reverse order of the season that just FINISHED, which is not
    # necessarily this row's own season — see _prior_season_league. The lottery and
    # the order overrides stay on the passed row: those are properties of THIS draft,
    # and the 2026 migration moved them here along with the picks.
    standings_src = _prior_season_league(db, league, season_year)
    rev = _reverse_standings_managers(db, league, standings_src)
    r1 = _r1_order_managers(db, league) or rev

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
    # ...and the display-only condition metadata, keyed the same (round, original
    # owner) way, on the round the pick EFFECTIVELY occupies right now.
    conds = pick_conditions(db, league, season_year, draft_type)
    for b in board:
        orig_person = names.get(b["original_owner_id"])
        cur_person = own.get((b["round"], orig_person), orig_person)
        b["owner_id"] = id_by_person.get(cur_person, b["original_owner_id"])
        b["condition"] = conds.get((b["round"], orig_person))

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
        row = {
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
            # `player_label` is the last fallback, not an afterthought: three real
            # 2026 main-draft picks (Ruben Dias, Alex Scott, Braithwaite) carry a
            # free-text label and no player_id, and without this they render as EMPTY
            # SLOTS on a completed board. The discovery board has always preferred the
            # label for the same reason. It also matters live — see the note above:
            # `next_open_pick` reads a falsy player as "still on the clock", so a
            # recorded-but-blank pick would put the draft back on a slot already used.
            "player": (
                tnames.get(dp.team_id) if dp and dp.team_id
                else (pnames.get(dp.player_id) if dp and dp.player_id else None)
                or (dp.player_label if dp else None)
            ),
            "is_goalie_team": bool(dp and dp.team_id),
        }
        # Display only — the round this slot IS already reflects a met condition.
        # Attached ONLY for a conditional pick, so an ordinary row's dict is byte
        # -identical to what it was before this feature (an exact-equality test one
        # module over depends on that, and so does every consumer that iterates keys).
        if b.get("condition"):
            row.update(b["condition"])
        out.append(row)
    return out


def get_future_picks(db: Session, league: League) -> list[dict]:
    """Future pick ownership by year, across every league row — computed from the
    same pick_ownership source as the draft board, so a newly entered pick trade
    shows up here automatically. Only years still ahead of `league`'s own season
    are shown: this is a forward-looking outlook, not an archive.

    Future picks are deliberately season-AGNOSTIC (decided 2026-08-18) — a
    standing multi-year outlook on who owns what, never migrated between league
    rows even once the season-alignment migration lands. So this scans every
    league row for FuturePick/pick-Trade rows, not just the current one.

    `pick_ownership` is itself cross-row now, so one call answers each year — this
    used to merge a call per league row, ascending by season_year, to let a newer
    row's entry win for the same (round, original_owner). That precedence moved
    inside pick_ownership rather than being lost. The same future year can
    legitimately have rows on more than one league row (entered before a rollover,
    then again after), and person names are the stable key across them.
    """
    from models import FuturePick

    leagues = db.query(League).order_by(League.season_year.asc()).all()
    league_ids = [lg.id for lg in leagues]
    years = {
        y for (y,) in db.query(FuturePick.season_year)
        .filter(FuturePick.league_id.in_(league_ids)).distinct()
    }
    years |= {
        y for (y,) in db.query(Trade.pick_season_year)
        .filter(Trade.league_id.in_(league_ids), Trade.pick_season_year.isnot(None))
        .distinct()
    }
    cur = league.season_year or 0
    years = {y for y in years if y >= cur}

    out = []
    for y in sorted(years):
        entry = {"year": y}
        for dt in ("main", "discovery"):
            merged = pick_ownership(db, league, y, dt)
            conds = pick_conditions(db, league, y, dt)
            entry[dt] = [
                {"round": rnd, "original_owner": orig, "owner": owner,
                 # Same rule as the draft board: present only when conditional.
                 **(conds.get((rnd, orig)) or {})}
                for (rnd, orig), owner in sorted(merged.items(), key=lambda kv: (kv[0][0], kv[0][1]))
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
def _trade_season_year(trade: "Trade", leagues_by_id: dict) -> int:
    """Which season a trade belongs to for DISPLAY grouping — computed on read,
    never taken from the storing row alone, so a post-GW38 offseason trade shows
    under the season it's headed INTO rather than the one that just ended.

    `event_gw` set (FPL-synced, mid-season): the trade happened during that row's
    own season and can't have crossed a season boundary — the storing row's
    `season_year` is exactly right.

    `event_gw` NULL (commissioner-entered): bucket `created_at` against the trade
    deadline (Jan 31, per the spec) — a January trade belongs to the season that
    started the PREVIOUS calendar year; any other month belongs to the season
    starting THAT year. That second case is what puts a May-Dec (post-GW38,
    offseason) trade into the FOLLOWING season, the confirmed rule.

    KNOWN IMPRECISION, deliberately not solved here: `Trade.created_at` was
    backfilled by migration f5a6b7c8d9e0 (2026-08-11) with ONE SHARED timestamp
    on every pre-migration row, so a pre-migration commissioner trade (no
    event_gw) always buckets into 2026 regardless of when it actually happened.
    An admin can set `event_gw` via `edit_trade` to re-file a misfiled row onto
    the correct season — that flips it onto the (always-correct) first branch.
    """
    if trade.event_gw is not None:
        lg = leagues_by_id.get(trade.league_id)
        return lg.season_year if lg else 0
    d = trade.created_at
    return d.year - 1 if d.month == 1 else d.year


def get_trades(db: Session) -> list[dict]:
    """All trades across every season — synced player trades and commissioner-
    entered player/pick/club trades — grouped by the season each belongs to (see
    _trade_season_year), newest season first; within a season, newest first
    (event_gw desc, then created_at desc; rows with no event_gw sort last).

    Cross-season by design: trades are a permanent record of who dealt with whom,
    not scoped to whichever league row is current. Names/players must therefore
    be resolved across ALL managers/players, not one league's — a single-league
    map would silently render a blank from/to cell for a trade whose managers
    belong to a different row.
    """
    leagues_by_id = {lg.id: lg for lg in db.query(League)}
    names = {m.id: m.display for m in db.query(Manager)}
    # No single league's season_identity overlay is right for every row's trades
    # here — this is exactly the global fallback player_names() itself uses for
    # anyone without a snapshot on the league it's called with.
    pnames = {p.id: p.name for p in db.query(Player)}
    pl_teams = {t.id: t.name for t in db.query(PlTeam)}

    by_year: dict = {}
    cond_cache: dict = {}
    for t in db.query(Trade).all():
        if t.team_id is not None:
            # A goalie-team trade (team_id set, pick_round and player_id both
            # NULL) used to fall through to the player branch and render
            # kind="player", what="—" — fixed here.
            kind, what = "club", pl_teams.get(t.team_id, "—")
        elif t.pick_round is not None:
            kind, what = "pick", t.draft_pick or f"R{t.pick_round} pick"
        else:
            kind, what = "player", pnames.get(t.player_id, "—")
        row = {
            "id": str(t.id),
            "kind": kind,
            "what": what,
            "from": names.get(t.from_manager),
            "to": names.get(t.to_manager),
            "gw": t.event_gw,
            "source": "FPL" if t.fpl_trade_id else "site",
            "edited": bool(t.manually_edited),
        }
        if t.condition_logic is not None:
            # Resolved per row here rather than via pick_conditions: this listing is
            # cross-season and cross-league, so there is no one (season, draft_type)
            # to key a batch on.
            res = _resolve_condition(db, t, cond_cache)
            row["conditional"] = True
            row["condition_status"] = res["state"]
            row["condition_note"] = _condition_note(db, t, res["state"], res["current"])
            # Only the MANUAL terms, and only here: they're the ones the commissioner
            # has to rule on, and the corrections editor is the only surface that can.
            # Attached as a key rather than folded into the note so the template can
            # render one control per term.
            row["manual_terms"] = [
                {"id": str(term.id), "note": term.note, "manual_state": term.manual_state}
                for term in _condition_terms(db, t)
                if term.metric == CONDITION_MANUAL
            ]
        by_year.setdefault(_trade_season_year(t, leagues_by_id), []).append((t, row))

    out = []
    for year in sorted(by_year, reverse=True):
        ordered = sorted(
            by_year[year],
            key=lambda pair: (
                pair[0].event_gw is None, -(pair[0].event_gw or 0),
                -pair[0].created_at.timestamp(),
            ),
        )
        out.append({"year": year, "trades": [row for _t, row in ordered]})
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


_CONDITION_COLUMNS = ("condition_logic", "condition_effect", "pick_round_if_met")


def edit_trade(
    db: Session, league: League, trade_id: str, *,
    from_fpl: str | None = None, to_fpl: str | None = None,
    event_gw: int | None = None, conditions: str | None = None,
    set_condition: bool = False,
    condition_logic: str | None = None,
    condition_effect: str | None = None,
    pick_round_if_met: int | None = None,
    condition_terms: list[dict] | None = None,
) -> dict:
    """Correct a trade. Only the fields passed are changed.

    Sets `manually_edited`, which stops sync_trades rewriting it back or, worse,
    re-inserting the uncorrected version as a duplicate (its reconciliation matches
    an exact from/to pair, so a flipped direction sails straight past it).

    The pick CONDITION is edited as a unit behind `set_condition`, not field by
    field: a clause and its terms only make sense together (a metric without a
    threshold, or a threshold left over from a previous metric, is not a state worth
    being able to save), and validation is over the whole set. `set_condition=True`
    with no terms CLEARS the condition, which is how a conditional pick is made
    ordinary again — terms included, since a clause-less term row would be
    unreachable and would still show up in a note.
    """
    row = _trade_or_404(db, league, trade_id)
    prev = _previous(row, ["from_manager", "to_manager", "event_gw", "conditions",
                           *_CONDITION_COLUMNS])

    if from_fpl:
        row.from_manager = _resolve_manager(db, league, from_fpl).id
    if to_fpl:
        row.to_manager = _resolve_manager(db, league, to_fpl).id
    if event_gw is not None:
        row.event_gw = event_gw
    if conditions is not None:
        row.conditions = conditions or None
    if set_condition:
        if row.pick_round is None and condition_terms:
            raise RuleViolation("only a pick trade can carry a condition")
        cond, terms = _condition_spec(
            db, condition_logic=condition_logic, condition_effect=condition_effect,
            pick_round_if_met=pick_round_if_met, condition_terms=condition_terms,
        )
        for col, val in cond.items():
            setattr(row, col, val)
        _write_condition_terms(db, row, terms)
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


def _discovery_pick_or_404(
    db: Session, league: League, season_year: int, pick_number: int
) -> DraftPick:
    row = (
        db.query(DraftPick)
        .filter_by(league_id=league.id, season_year=season_year,
                   draft_type="discovery", pick_number=pick_number)
        .one_or_none()
    )
    if not row:
        raise RuleViolation(
            f"no {season_year} discovery pick #{pick_number}"
        )
    if row.team_id is not None:
        # A goalie-team pick carries nothing else (the DraftPick CHECK), so writing
        # player_id here would fail at the constraint with an opaque IntegrityError.
        raise RuleViolation("that pick is a goalie team, not a player")
    return row


def link_discovery_pick(
    db: Session, league: League, *, season_year: int, pick_number: int,
    player_fpl_id: int,
) -> dict:
    """Attach a real `players` row to a free-text discovery pick.

    A discovery pick is recorded in September as a NAME (`record_discovery_pick`),
    because the player is by definition not yet in the Premier League and so has no
    `players` row to point at. When he later joins, this is what connects the two —
    and that link is the only thing that lets `_derive_keeper_status` see the pick at
    all, since the derivation reads rosters and trades, never `draft_picks`.

    **Always an explicit admin action, never automatic name-matching.** `Player.name`
    is FPL's short `web_name` ("Woltemade") while a manager types whatever they had in
    mind ("Nick Woltemade"), so a fuzzy match is a coin flip — and a wrong link hands
    one manager another's keeper with a 4-year clock, which nothing downstream would
    flag as suspicious. A follow-up session adds sync-driven match SUGGESTIONS; they
    call this function on admin confirm, so the human is still the one deciding.

    `player_label` is kept exactly as entered. It's the historical record of what the
    manager actually called out on draft night, and `get_discovery_board` deliberately
    prefers it, so the board keeps reading the way the draft happened.
    """
    row = _discovery_pick_or_404(db, league, season_year, pick_number)
    player = _resolve_player(db, player_fpl_id)

    if row.player_id == player.id:
        # Idempotent: the follow-up suggestion flow can confirm the same match twice
        # (two admins, a double-submit) without an error or a second audit entry.
        return {"season_year": season_year, "pick_number": pick_number,
                "label": row.player_label, "player": player.name, "linked": True,
                "changed": False}
    if row.player_id is not None:
        prior = db.get(Player, row.player_id)
        raise RuleViolation(
            f"pick #{pick_number} is already linked to "
            f"{prior.name if prior else row.player_id} — unlink it first"
        )
    clash = (
        db.query(DraftPick)
        .filter(DraftPick.league_id == league.id,
                DraftPick.season_year == season_year,
                DraftPick.draft_type == "discovery",
                DraftPick.player_id == player.id,
                DraftPick.id != row.id)
        .first()
    )
    if clash:
        # Two picks pointing at one player would make him two managers' keeper at once,
        # and _derive_keeper_status would label whichever it saw last.
        raise RuleViolation(
            f"{player.name} is already linked to {season_year} discovery pick "
            f"#{clash.pick_number}"
        )

    mgr = db.get(Manager, row.manager_id) if row.manager_id else None
    record_audit(
        db, league, action="discovery.link",
        summary=(f"Linked {season_year} discovery pick #{pick_number} "
                 f"({mgr.display if mgr else '?'} — {row.player_label or '—'}) "
                 f"to {player.name}"),
        manager_ids=[row.manager_id] if row.manager_id else None,
        details={"season_year": season_year, "pick_number": pick_number,
                 "player_fpl_id": player_fpl_id, "player": player.name,
                 "previous": _previous(row, ["player_id", "player_label"])},
    )
    row.player_id = player.id
    db.commit()
    return {"season_year": season_year, "pick_number": pick_number,
            "label": row.player_label, "player": player.name, "linked": True,
            "changed": True}


def unlink_discovery_pick(
    db: Session, league: League, *, season_year: int, pick_number: int,
) -> dict:
    """Detach a player from a discovery pick — the undo for a mislink.

    The free-text `player_label` was never touched by the link, so clearing the FK
    restores the pick exactly as recorded rather than blanking it.
    """
    row = _discovery_pick_or_404(db, league, season_year, pick_number)
    if row.player_id is None:
        raise RuleViolation(f"pick #{pick_number} isn't linked to a player")

    prior = db.get(Player, row.player_id)
    mgr = db.get(Manager, row.manager_id) if row.manager_id else None
    record_audit(
        db, league, action="discovery.unlink",
        summary=(f"Unlinked {season_year} discovery pick #{pick_number} "
                 f"({mgr.display if mgr else '?'}) from "
                 f"{prior.name if prior else '?'}"),
        manager_ids=[row.manager_id] if row.manager_id else None,
        details={"season_year": season_year, "pick_number": pick_number,
                 "previous": _previous(row, ["player_id", "player_label"])},
    )
    row.player_id = None
    db.commit()
    return {"season_year": season_year, "pick_number": pick_number,
            "label": row.player_label, "player": None, "linked": False,
            "changed": True}


# ---- discovery pick match SUGGESTIONS (never links) ----
#
# Its own copy of the normalisation, not an import. scripts/import_projections.py's
# `_norm` is the model but strips every non-letter, which collapses "Nick Woltemade"
# to one token and makes subset matching impossible; and history_import._norm carries
# a comment expressly forbidding unification. Three small functions that agree by
# design beat one shared one that has to serve three different jobs.
_MATCH_TRANSLIT = str.maketrans({"ø": "o", "đ": "d", "ı": "i", "ł": "l",
                                 "æ": "ae", "ß": "ss", "þ": "th"})


def _match_norm(s: str) -> str:
    """Lowercase FIRST, transliterate SECOND, NFKD THIRD — the order is load-bearing.
    The translation table has lowercase keys only, so an uppercase Ø would slip past
    it; and ø/ı have NO NFKD decomposition, so a bare ascii-ignore pass DELETES them
    and 'Ødegaard' becomes 'degaard'. Same trap `_TRANSLIT` exists for in
    scripts/import_projections.py, pinned there by test_projections.py."""
    s = (s or "").lower().translate(_MATCH_TRANSLIT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s)


def _match_tokens(s: str) -> set:
    """The same normalisation applied per WORD, so name order and extra given names
    don't matter. Splitting before normalising is what `_match_norm` alone can't do:
    it strips the separators, so everything becomes one token."""
    return {t for t in (_match_norm(p) for p in re.split(r"[^\w]+", s or "")) if t}


def _score_match(label: str, full_name: str | None, web_name: str | None):
    """-> (score, method) for a free-text label against one player, or None.

    Three tiers, most confident first. All of them only ever produce a SUGGESTION —
    scoring 1.0 does not authorise a link, because two people share a name and a
    wrong link is invisible downstream.
    """
    lab_n, lab_t = _match_norm(label), _match_tokens(label)
    if not lab_n:
        return None
    names = [n for n in (full_name, web_name) if n]

    for n in names:
        if lab_n == _match_norm(n):
            return (1.0, "exact")

    # A subset either way covers the two common shapes at once: the manager typed
    # more than FPL stores ("Nick Woltemade" vs web_name "Woltemade"), or fewer
    # ("Woltemade" vs full_name "Nick Woltemade").
    for n in names:
        toks = _match_tokens(n)
        if toks and lab_t and (lab_t <= toks or toks <= lab_t):
            return (0.9, "strong")

    best = 0.0
    for n in names:
        best = max(best, difflib.SequenceMatcher(None, lab_n, _match_norm(n)).ratio())
    if best >= 0.85:
        return (round(best, 3), "close")
    return None


def match_discovery_picks(db: Session) -> dict:
    """Propose players for every unlinked discovery pick. Returns counts for logging.

    **Never links anything.** It only ever writes `discovery_match_suggestions` rows
    with status 'pending'; `DraftPick.player_id` is written by `link_discovery_pick`
    and nothing else, on an explicit admin action. That separation is the entire
    design — see `link_discovery_pick` for why a name match cannot be trusted to
    act on its own.

    Runs daily off the back of a full sync, because the pool it matches against grows
    all season as players join the Premier League: a September pick usually has no
    `players` row at all until January, so the same pick has to be re-examined against
    fresh data rather than judged once and forgotten.

    Idempotent. A pair already `confirmed` or `rejected` is left strictly alone — a
    rejection is the commissioner saying "not this one", and re-proposing it every
    night would make the dashboard useless.

    Reads and writes league-custom tables, so it lives here and is called from the
    post-sync hook in main.py — never from inside sync.py, which must stay on the
    FPL-canonical side of the two-truths boundary.
    """
    from models import DiscoveryMatchSuggestion

    picks = (
        db.query(DraftPick)
        .filter(DraftPick.draft_type == "discovery",
                DraftPick.player_id.is_(None),
                DraftPick.player_label.isnot(None),
                # Defensive: the DraftPick CHECK is deliberately narrow (see the
                # model), so live rows are not provably one-of.
                DraftPick.team_id.is_(None))
        .all()
    )
    if not picks:
        return {"picks": 0, "candidates": 0, "created": 0, "updated": 0, "skipped": 0}

    players = db.query(Player).all()
    # (pick_id, player_id) -> row, so an existing decision is visible before writing.
    existing = {
        (s.draft_pick_id, s.player_id): s
        for s in db.query(DiscoveryMatchSuggestion)
    }

    created = updated = skipped = 0
    for pick in picks:
        for p in players:
            hit = _score_match(pick.player_label, p.full_name, p.name)
            if not hit:
                continue
            score, method = hit
            prior = existing.get((pick.id, p.id))
            if prior is not None:
                if prior.status != "pending":
                    skipped += 1          # confirmed or rejected: the human decided
                    continue
                if (prior.score, prior.method) != (score, method):
                    prior.score, prior.method = score, method
                    updated += 1
                continue
            db.add(DiscoveryMatchSuggestion(
                draft_pick_id=pick.id, player_id=p.id,
                score=score, method=method, status="pending",
            ))
            created += 1
    db.commit()
    return {"picks": len(picks), "candidates": len(players),
            "created": created, "updated": updated, "skipped": skipped}


def _suggestion_or_404(db: Session, suggestion_id: str):
    from models import DiscoveryMatchSuggestion

    row = db.get(DiscoveryMatchSuggestion, suggestion_id)
    if row is None:
        raise RuleViolation("suggestion not found")
    return row


def confirm_discovery_suggestion(
    db: Session, league: League, suggestion_id: str
) -> dict:
    """Accept a proposed match: link the pick, then mark the suggestion confirmed.

    Linking first is deliberate — `link_discovery_pick` enforces every rule (already
    linked, player linked elsewhere, goalie-team pick) and raises, so a refused link
    leaves the suggestion `pending` rather than recording a decision that didn't
    happen.
    """
    row = _suggestion_or_404(db, suggestion_id)
    pick = db.get(DraftPick, row.draft_pick_id)
    if pick is None:
        raise RuleViolation("the pick this suggestion belongs to is gone")
    player = db.get(Player, row.player_id)
    if player is None or player.fpl_id is None:
        # link_discovery_pick resolves by fpl_id, and a departed player has none.
        raise RuleViolation("that player has no current FPL id to link by")

    out = link_discovery_pick(
        db, league, season_year=pick.season_year,
        pick_number=pick.pick_number, player_fpl_id=player.fpl_id,
    )
    previous = _previous(row, ["status", "score", "method"])
    row.status = "confirmed"
    record_audit(
        db, league, action="discovery.suggestion.confirm",
        summary=(f"Confirmed {pick.season_year} discovery pick #{pick.pick_number} "
                 f"({pick.player_label or '—'}) = {player.name} "
                 f"[{row.method} {row.score}]"),
        manager_ids=[pick.manager_id] if pick.manager_id else None,
        details={"suggestion_id": str(row.id), "player_fpl_id": player.fpl_id,
                 "previous": previous},
    )
    db.commit()
    return {**out, "status": "confirmed"}


def reject_discovery_suggestion(
    db: Session, league: League, suggestion_id: str
) -> dict:
    """Dismiss a proposed match. The row is KEPT, not deleted — that is what stops
    tonight's matcher proposing it all over again."""
    row = _suggestion_or_404(db, suggestion_id)
    pick = db.get(DraftPick, row.draft_pick_id)
    player = db.get(Player, row.player_id)
    previous = _previous(row, ["status", "score", "method"])
    row.status = "rejected"
    record_audit(
        db, league, action="discovery.suggestion.reject",
        summary=(f"Rejected {player.name if player else '?'} for "
                 f"{pick.season_year if pick else '?'} discovery pick "
                 f"#{pick.pick_number if pick else '?'} "
                 f"({pick.player_label if pick else '—'})"),
        manager_ids=[pick.manager_id] if pick and pick.manager_id else None,
        details={"suggestion_id": str(row.id), "previous": previous},
    )
    db.commit()
    return {"suggestion_id": str(row.id), "status": "rejected"}


def unlinked_discovery_picks(db: Session, league: League) -> list[dict]:
    """Every unlinked discovery pick with its pending suggestions, newest season
    first. Feeds the /admin/corrections dashboard and the data_health count."""
    from models import DiscoveryMatchSuggestion

    picks = (
        db.query(DraftPick)
        .filter(DraftPick.draft_type == "discovery",
                DraftPick.player_id.is_(None),
                DraftPick.player_label.isnot(None),
                DraftPick.team_id.is_(None))
        .order_by(DraftPick.season_year.desc(), DraftPick.pick_number)
        .all()
    )
    if not picks:
        return []

    # Managers span league rows (a pick predates a rollover), so no league filter.
    names = {m.id: m.display for m in db.query(Manager)}
    pool = {p.id: p for p in db.query(Player)}
    by_pick: dict = {}
    for s in (
        db.query(DiscoveryMatchSuggestion)
        .filter(DiscoveryMatchSuggestion.status == "pending")
        .order_by(DiscoveryMatchSuggestion.score.desc())
    ):
        by_pick.setdefault(s.draft_pick_id, []).append(s)

    out = []
    for pick in picks:
        suggestions = []
        for s in by_pick.get(pick.id, []):
            p = pool.get(s.player_id)
            if p is None:
                continue
            suggestions.append({
                "id": str(s.id), "player": p.name, "full_name": p.full_name,
                "team": p.current_team, "position": p.position,
                "fpl_id": p.fpl_id, "score": s.score, "method": s.method,
            })
        out.append({
            "pick_id": str(pick.id), "season_year": pick.season_year,
            "pick_number": pick.pick_number, "round": pick.round,
            "label": pick.player_label, "owner": names.get(pick.manager_id),
            "suggestions": suggestions,
        })
    return out


def next_open_pick(board: list[dict]) -> dict | None:
    """The on-the-clock slot: first board pick with no player recorded yet."""
    return next((b for b in board if not b.get("player")), None)


# ---- league history / honor roll ----
def get_history(db: Session, league: League) -> dict:
    """Season-by-season winners + career honor roll + per-season standings +
    discovery-draft results. Reads across all league rows, not just the current one.
    The `league` parameter is retained for signature stability but not used by the queries."""
    from models import DiscoveryResult, HistoricalStanding, ManagerHonors, SeasonHistory

    # Dedupe by year, NEWEST LEAGUE ROW WINS. The tiebreak is the whole point: two
    # rows for one year only happen around a rollover, when the same history has been
    # imported onto both the outgoing and incoming league row, and the incoming one is
    # the correction. Without the League join the winner was whatever order Postgres
    # happened to return — in practice heap order, so the STALE row won.
    seen_years = set()
    deduped_seasons = []
    for s in (
        db.query(SeasonHistory)
        .join(League, League.id == SeasonHistory.league_id)
        .order_by(League.season_year.desc(), SeasonHistory.year.desc())
        .all()
    ):
        if s.year not in seen_years:
            deduped_seasons.append(s)
            seen_years.add(s.year)
    # Display order is by year; the league-row ordering above only picked the winners.
    deduped_seasons.sort(key=lambda s: s.year or "", reverse=True)

    # Same rule, keyed on the manager instead of the year: honors are a career total,
    # so the newest row's figures supersede an older row's rather than merging.
    seen_managers = set()
    deduped_honors = []
    for h in (
        db.query(ManagerHonors)
        .join(League, League.id == ManagerHonors.league_id)
        .order_by(League.season_year.desc())
        .all()
    ):
        if h.manager_name not in seen_managers:
            deduped_honors.append(h)
            seen_managers.add(h.manager_name)
    deduped_honors.sort(
        key=lambda h: (-(h.titles or 0), -(h.cups or 0), h.manager_name or "")
    )

    standings_by_season: dict = {}
    for s in (
        db.query(HistoricalStanding)
        .order_by(HistoricalStanding.year.desc(), HistoricalStanding.rank)
        .all()
    ):
        if s.year not in standings_by_season:
            standings_by_season[s.year] = []
            standings_by_season[s.year].append(
                {"rank": s.rank, "team": s.team_name, "manager": s.manager_name,
                 "w": s.wins, "d": s.draws, "l": s.losses, "pf": s.points_for, "h2h": s.h2h_points}
            )
        else:
            # Year already seen; only add if this is the first occurrence (ordering guarantees this)
            standings_by_season[s.year].append(
                {"rank": s.rank, "team": s.team_name, "manager": s.manager_name,
                 "w": s.wins, "d": s.draws, "l": s.losses, "pf": s.points_for, "h2h": s.h2h_points}
            )
    return {
        "seasons": [
            {"year": s.year, "league": s.league_winner, "cup": s.cup_winner, "pup": s.pup_winner}
            for s in deduped_seasons
        ],
        "honors": [
            {"manager": h.manager_name, "titles": h.titles, "cups": h.cups} for h in deduped_honors
        ],
        "standings_by_season": [
            {"year": y, "rows": rows} for y, rows in standings_by_season.items()
        ],
        "discovery_by_season": _discovery_by_season(db, league),
        "cups_by_season": _cups_by_season(db, league),
    }


def _cups_by_season(db: Session, league: League) -> list[dict]:
    """Cup brackets by season, across all league rows. The `league` parameter is
    retained for signature stability but not used."""
    from models import CupMatch

    by_season: dict = {}
    for c in (
        db.query(CupMatch)
        .order_by(CupMatch.season.desc(), CupMatch.bracket, CupMatch.round, CupMatch.slot)
        .all()
    ):
        # Dedupe by season: only add the first occurrence (from ordering, that's the newest row)
        if c.season not in by_season:
            by_season[c.season] = []
        label = "Cup" if c.bracket == "cup" else "Pup Cup"
        rd = {1: "R1", 2: "Semi", 3: "Final"}.get(c.round, f"R{c.round}")
        by_season[c.season].append({
            "bracket": label, "round": rd, "seed": c.seed,
            "manager": c.manager_label, "total": c.total,
        })
    return [{"year": y, "rows": rows} for y, rows in by_season.items()]


def _discovery_by_season(db: Session, league: League) -> list[dict]:
    """Discovery results by season, across all league rows. The `league` parameter is
    retained for signature stability but not used."""
    from models import DiscoveryResult

    by_season: dict = {}
    for r in (
        db.query(DiscoveryResult)
        .order_by(DiscoveryResult.season.desc(), DiscoveryResult.pick_number)
        .all()
    ):
        # Dedupe by season: only add the first occurrence (from ordering, that's the newest row)
        if r.season not in by_season:
            by_season[r.season] = []
        by_season[r.season].append(
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
        linked_name = None
        if p.player_id:
            pl = db.get(Player, p.player_id)
            label = pl.name if pl else label
            linked_name = pl.name if pl else None
        picks.append({
            "id": str(p.id), "season_year": p.season_year, "draft_type": p.draft_type,
            "round": p.round, "pick_number": p.pick_number,
            "manager": names.get(p.manager_id), "player": label,
            # The discovery link tool below reads these. `label` above collapses the
            # two into one display string; the form needs to know which it is, and a
            # free-text pick needs its as-entered name kept visible next to the
            # linked player's so a mislink is obvious at a glance.
            "linked": p.player_id is not None,
            "linked_player": linked_name,
            "player_label": p.player_label,
            "linkable": p.draft_type == "discovery" and p.team_id is None,
        })
    return {
        # get_trades is cross-season now; flatten back to the flat list this
        # page (and its edit/delete forms) expects — newest season first, and
        # within a season already ordered newest-first.
        "trades": [row for season in get_trades(db) for row in season["trades"]],
        "discovery": _discovery_by_season(db, league),
        "unlinked_discovery": unlinked_discovery_picks(db, league),
        "picks": picks,
        "managers": [
            {"name": m.display, "fpl": m.fpl_manager_id}
            for m in db.query(Manager).filter_by(league_id=league.id)
            .order_by(Manager.display_name)
        ],
        # For the pick-condition editor's player subject. Same accent-aliased picker
        # the draft and IL forms use.
        "players": list_players(db, league),
        # Discord proposals awaiting review. Deliberately on THIS page rather than a
        # new one: it is already where the commissioner corrects trades and links
        # discovery picks, and a Discord proposal is the same kind of work.
        "discord_queue": discord_ingest_queue(db, league),
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
        # Membership through _effective_roster_pids, NOT a join on Roster. This is the
        # trade-entry form, and it already overlays PICKS via pick_ownership below — so
        # the raw join meant a commissioner-traded player still appeared on the seller's
        # side and was missing from the buyer's, on the very page used to enter trades.
        # It also hid an absent player, who is often exactly the asset being shopped.
        pids = _effective_roster_pids(db, league, m.id, gw.id)
        for ps, p in (
            db.query(PlayerSeason, Player)
            .join(Player, Player.id == PlayerSeason.player_id)
            .filter(
                PlayerSeason.player_id.in_(pids),
                PlayerSeason.league_id == league.id,
            )
            .order_by(PlayerSeason.position, PlayerSeason.name)
        ) if pids else []:
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
    from models import Gameweek, GameweekPoints, KeeperSeed, Roster

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

    # ---- rollover assertions ------------------------------------------------
    # Everything below would have caught the 26/27 rollover breakage on day one.
    # advance_season pairs managers on fpl_manager_id, FPL reissued every id, and
    # each carry `continue`s on a miss — so identity, logins and keeper clocks were
    # all silently dropped while the rollover reported success. See the P0 backlog
    # entry. These are the observable consequences, checked directly.

    # A NULL password_hash is the "first-time set" state, which is legitimate for a
    # genuinely new manager but not for ten of them at once.
    no_pw = [m.display for m in mgrs if not m.password_hash]
    add("managers can log in", not no_pw,
        f"{len(no_pw)} with no password set: " + ", ".join(sorted(no_pw))
        if no_pw else "ok")

    prior = _prior_season_league(db, league, league.season_year or 0)
    if prior.id != league.id:
        # Keeper clocks: a season whose predecessor had submitted keepers must have
        # carried seeds. 0-against-152 is what the failed carry actually looked like.
        prior_sel = db.query(KeeperSelection).filter_by(
            league_id=prior.id, season_year=league.season_year
        ).count()
        seeds = db.query(KeeperSeed).filter_by(league_id=league.id).count()
        if prior_sel:
            add("keeper clocks carried from last season", seeds > 0,
                f"{seeds} seed(s) for {prior_sel} selection(s) submitted on "
                f"{prior.season_year}"
                + ("" if seeds else " — the rollover carry did not run"))

        # Draft history: if last season's row holds keeper selections for THIS
        # season, a draft happened for it, and its picks belong on this row.
        if prior_sel:
            here = db.query(DraftPick).filter_by(
                league_id=league.id, season_year=_draft_year_for(league)
            ).count()
            there = db.query(DraftPick).filter_by(
                league_id=prior.id, season_year=_draft_year_for(league)
            ).count()
            add("this season's draft is on this season's row", here > 0 or not there,
                f"{here} here, {there} still on {prior.season_year}"
                + (" — run scripts/migrate_2026_draft.py" if there and not here else ""))

    # THE INPUT MUST EXIST, not merely "the guard behaved". `flag_ineligible` returns
    # 0 on an empty player_pool_snapshot BY DESIGN, and it runs on every full sync, so
    # a season with no snapshot has the ineligible-player rule quietly switched off and
    # nothing anywhere reports a problem. That is exactly how the original
    # snapshot_player_pool NameError survived unnoticed for every season (found
    # 2026-08-20), and the fix did NOT backfill: snapshot_player_pool's only caller is
    # the rollover route, so a season rolled over before the fix landed stays empty
    # until someone captures it by hand. Assert the pool, not the flagging — zero
    # ineligible players is a legitimate result, zero POOL never is.
    from models import PlayerPoolSnapshot

    pool = db.query(PlayerPoolSnapshot).filter_by(league_id=league.id).count()
    add(
        "draft-day player pool captured",
        pool > 0,
        f"{pool} rows"
        + (
            ""
            if pool
            else " — the ineligible-player rule cannot fire without it; run "
            "services.snapshot_player_pool for this league"
        ),
    )

    # The cron reporting green while syncing nothing: every sub-task skips a frozen
    # league and the skip sets ok=True. If THIS league isn't frozen but the last
    # league sync skipped one that was, the sync is pointed at the wrong season.
    if last is not None and last.ok and not league.sync_locked:
        if "frozen" in (last.notes or "").lower():
            add("sync is targeting this season", False,
                f"last league sync skipped a frozen season ({last.notes}) — "
                "sync_all is resolving a different league than the current one")

    sc = db.query(Standing).filter_by(league_id=league.id).count()
    add("standings row per manager", sc == len(mgrs), f"{sc}/{len(mgrs)}")

    gwp = (
        db.query(GameweekPoints)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .filter(Gameweek.league_id == league.id)
        .count()
    )
    add("gameweek points populated", gwp > 0, f"{gwp} rows")

    # "Some rows exist" is not the question. `sync_rosters`/`sync_gameweek_points`
    # resolved the gameweek through FPL's /pl/event-status, which has no `status` key,
    # so both silently pinned to gameweek 1 — and this check passed the whole time on
    # GW1's rows while /scoreboard, /transactions and the keeper derivation all asked
    # for GW2 and found nothing. Assert the CURRENT gameweek specifically, the same
    # number every reader uses, or the check confirms only that sync once worked.
    cur_gw = current_gameweek(db, league)
    if cur_gw:
        gw_row = (
            db.query(Gameweek).filter_by(league_id=league.id, number=cur_gw).one_or_none()
        )
        for label, model in (("rosters", Roster), ("gameweek points", GameweekPoints)):
            n = (
                db.query(model).filter_by(gameweek_id=gw_row.id).count()
                if gw_row else 0
            )
            add(
                f"{label} synced for the current gameweek",
                n > 0,
                f"GW{cur_gw}: {n} rows"
                + ("" if n else " — sync is writing a different gameweek than the "
                              "site reads"),
            )

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

    # Absence-list integrity. The overlay grants real ownership off these rows, so a
    # malformed one is no longer cosmetic — and none of these can be inferred away
    # safely, which is why they are reported rather than auto-corrected.
    pname = {p.id: p.name for p in db.query(Player)}
    mname = {m.id: m.display for m in mgrs}
    rows = _absence_rows(db, league)
    active = [e for e in rows if (e.status or "").strip().lower() == "active"]

    # NULL status folds nowhere (not held, not covered-as-closed) and silently drops the
    # player out of his manager's squad.
    nullst = [f"{mname.get(e.manager_id, '—')}/{pname.get(e.player_id, '—')}"
              for e in rows if not (e.status or "").strip()]
    add("absence rows have a status", not nullst,
        "; ".join(sorted(nullst)) if nullst else "all set")

    # Two managers naming the same absent player: the fold is first-wins, so one of them
    # silently loses him.
    seen: dict = {}
    dupes = set()
    for e in active:
        if e.player_id in seen and seen[e.player_id] != e.manager_id:
            dupes.add(pname.get(e.player_id, "—"))
        seen.setdefault(e.player_id, e.manager_id)
    add("no player on two absence lists", not dupes,
        "; ".join(sorted(dupes)) if dupes else "none")

    # An active entry whose player someone ELSE now rosters. The fold's "only if
    # unowned" guard stops the theft, so the entry just sits there doing nothing —
    # usually it means he was dropped and claimed and the entry was never closed.
    owner_now = effective_owner(db, league) if gw is not None else {}
    stale = [
        f"{pname.get(e.player_id, '—')} (rostered by {mname.get(owner_now[e.player_id], '—')})"
        for e in active
        if owner_now.get(e.player_id) not in (None, e.manager_id)
    ]
    add("absent players aren't rostered elsewhere", not stale,
        "; ".join(sorted(stale)) if stale else "none")

    # Past GW38 an open entry means the manager never returned or released, so their
    # squad is one over and their keeper choice was made from an extra candidate.
    owed = [f"{mname.get(e.manager_id, '—')}/{pname.get(e.player_id, '—')}"
            for e in unresolved_absences(db, league)]
    add("absences resolved for the season", not owed,
        "; ".join(sorted(owed)) if owed else "none open")

    # Playing again but still parked — the must-return alert's own check, so a manager
    # who never looks at the homepage nag still shows up here.
    overdue = _return_required_entries(db, league)
    add("no absentee playing while still parked", not overdue,
        "; ".join(sorted(f"{e['manager']}/{e['player']}" for e in overdue))
        if overdue else "none")

    # Self-reported historical placements — not a failure (that's the whole point of
    # the feature), just visibility: these bypassed the current-roster check on the
    # manager's own say-so, so a quick admin skim catches the rare bad-faith or
    # mistaken one. Mirrors "discovery picks linked to players" below: informational,
    # never gates anything.
    self_rep = [
        f"{mname.get(e.manager_id, '—')}/{pname.get(e.player_id, '—')} (GW{e.start_gw})"
        for e in active if e.self_reported
    ]
    add("self-reported IL/international placements", not self_rep,
        f"{len(self_rep)}: " + "; ".join(sorted(self_rep)) if self_rep else "none")

    # players on the latest roster with no keeper seed (they default to fresh)
    seeded = {pid for (pid,) in db.query(KeeperSeed.player_id).filter_by(league_id=league.id)}
    # Deliberately RAW Roster, not the overlay — the audit in
    # docs/DESIGN_IL_OWNERSHIP.md §9 flagged this one as raw with no recorded reason,
    # so here it is. A seed is a fact about a player's history under the manager who
    # actually rostered him, exactly like the keeper derivation's `presence`; asking the
    # overlay would demand a seed from whoever holds him TODAY and let the real holder's
    # missing seed go unreported.
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

    # An unlinked discovery pick isn't broken — it's the normal state until the player
    # joins the PL — so this is visibility, not a failure. It goes red only when there
    # is something to DO: candidates are waiting for a decision. Without it the
    # dashboard is a page nobody thinks to open.
    unlinked = unlinked_discovery_picks(db, league)
    pending = sum(len(u["suggestions"]) for u in unlinked)
    add("discovery picks linked to players", not pending,
        f"{len(unlinked)} unlinked, {pending} suggestion(s) awaiting review"
        if pending else
        (f"{len(unlinked)} unlinked, none matched yet" if unlinked else "ok"))

    if goalie_teams_on(league.goalie_team_mode):
        # The draft being checked, not blindly next year: once the draft data has been
        # migrated onto the season it belongs to, this row's own season_year IS the
        # draft year and +1 would check an empty future draft. See _draft_year_for.
        upcoming = _draft_year_for(league)
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

    # A webhook that has been rotated, revoked or pointed at a deleted channel fails
    # exactly the way a healthy one fails on a blip: post_message returns False, the
    # row stays unstamped, the sweep retries next sync. That is correct behaviour and
    # it is also indistinguishable from working — the only evidence is a log line
    # nobody reads, so trades quietly stop being announced forever.
    #
    # So assert the QUEUE, not the sender: a backlog older than a day means the sweep
    # has been failing, whatever the reason. Zero pending is the healthy state and a
    # trade recorded minutes ago is not yet a problem. Skipped when the feature is off,
    # since a permanent backlog is the correct state for an unconfigured webhook.
    import discord_bridge

    if discord_bridge.webhook_url():
        import datetime as _dt

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        stale = (
            db.query(Trade)
            .filter(Trade.announced_at.is_(None), Trade.created_at < cutoff)
            .count()
        )
        add("trades announced to Discord", stale == 0,
            f"{stale} trade(s) unannounced for over a day — check the webhook URL"
            if stale else "")

    # Discord bridge. Both of its silent failure modes look exactly like a quiet
    # channel — a missing Read Message History permission returns an empty array
    # rather than a 403, and a disabled MESSAGE CONTENT intent returns blank content
    # with a 200 — so this asserts the INPUT, the same way the draft-day pool check
    # does. Skipped entirely when the feature is off: "not configured" is a supported
    # state, not a failure.
    if discord_bridge.bot_token():
        for label, env in (("trades", discord_bridge.TRADE_CHANNEL_ENV),
                           ("IL", discord_bridge.IL_CHANNEL_ENV)):
            channel = (os.getenv(env) or "").strip()
            if not channel:
                continue
            probe = discord_bridge.probe_channel(channel)
            add(f"Discord {label} channel readable", probe["ok"], probe["detail"])
        unmapped = unmapped_discord_authors(db, league)
        add("Discord authors all mapped to managers", not unmapped,
            ("unmapped: " + ", ".join(
                f"{u['name'] or u['discord_user_id']} ({u['messages']})"
                for u in unmapped)) if unmapped else "")

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
