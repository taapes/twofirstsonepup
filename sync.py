"""FPL Draft API sync: pull -> normalize -> store canonical data.

This module owns ONLY the FPL-canonical side of the two-truths boundary
(players, league, managers, gameweeks, rosters). It must never write to or
mutate league-custom tables (keepers, IL, drafts, etc.).

Each sub-task records a SyncLog row so /admin/sync runs are auditable.
"""

import asyncio
import datetime

import httpx
from sqlalchemy.orm import Session

from db import SessionLocal
from models import (
    Fixture,
    Gameweek,
    GameweekPoints,
    League,
    Manager,
    Match,
    Player,
    PlayerSeason,
    PlTeam,
    Roster,
    Standing,
    SyncLog,
    Trade,
)
import services
from rules import verify_league_feed
from settings import API_BASE, LEAGUE_ID


class LeagueIdentityError(RuntimeError):
    """The `/league/{id}/details` feed no longer describes the league we have
    stored — FPL recycles league ids between seasons, so this id now belongs to
    somebody else. Sync aborts loudly rather than merge a stranger's data into
    our history."""


# FPL element_type id -> position short name. Stable in bootstrap-static, but we
# read element_types from the payload when available and fall back to this.
_POSITION_FALLBACK = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


# ---------- helpers ----------
async def _get_json(client: httpx.AsyncClient, url: str):
    r = await client.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def _upsert(session: Session, model, match: dict, values: dict):
    row = session.query(model).filter_by(**match).one_or_none()
    if row:
        for k, v in values.items():
            setattr(row, k, v)
        return row
    row = model(**{**match, **values})
    session.add(row)
    return row


def _upsert_pl_teams(session: Session, teams_payload: list) -> dict:
    """Upsert the 20 Premier League clubs from a bootstrap `teams[]` array.

    Matched on the PERMANENT `code`, never on `teams[].id` — that one is the
    alphabetical index within a season and is reassigned every August (see PlTeam).
    A club whose payload entry carries no `code` falls back to matching on
    `short_name` and is never inserted, because a row without a stable identity is
    worse than no row: the next sync would insert it again under a different id.

    Membership is rewritten wholesale, but ONLY when the payload actually looks like
    a Premier League (>=20 clubs). A short or partial payload must not silently
    relegate the entire division. Rows are never deleted — a relegated club keeps its
    history and comes back on promotion.

    Returns {fpl teams[].id: PlTeam} so callers that already index by that id (every
    element's `team` field) can resolve straight to a row.
    """
    teams_payload = [t for t in (teams_payload or []) if t.get("id") is not None]
    if not teams_payload:
        return {}

    now = datetime.datetime.now(datetime.timezone.utc)
    by_code = {t.code: t for t in session.query(PlTeam)}
    by_short = {t.short_name: t for t in session.query(PlTeam)}

    seen, out = [], {}
    for t in teams_payload:
        short = t.get("short_name") or t.get("name")
        code = t.get("code")
        row = by_code.get(code) if code is not None else by_short.get(short)
        if row is None:
            if code is None:
                continue  # no stable identity; skip rather than create a duplicate
            row = PlTeam(code=code)
            session.add(row)
            by_code[code] = row
        row.fpl_id = t["id"]
        row.short_name = short
        row.name = t.get("name") or short
        row.last_seen_at = now
        seen.append(row)
        out[t["id"]] = row

    if len(seen) >= 20:
        current = {id(r) for r in seen}
        for row in session.query(PlTeam):
            row.is_current_pl = id(row) in current
        for row in seen:
            row.is_current_pl = True  # covers rows still pending in this flush
    session.flush()
    return out


def _resolve_league(session: Session, fpl_league_id, log: SyncLog) -> League | None:
    """The league row this sync task may write to, or None when it's missing or
    frozen (the reason recorded on `log`). A frozen row is a finished season: FPL
    may have handed its league id to a different league, so we never touch it."""
    league = (
        session.query(League).filter_by(fpl_league_id=str(fpl_league_id)).one_or_none()
    )
    if league and not league.sync_locked:
        return league
    if not league:
        log.notes = "league missing, run sync_league_and_managers first"
    else:
        log.ok = True  # a deliberate freeze is a skip, not a failure
        log.notes = f"season {league.season_year} is frozen (sync_locked); skipped"
    log.finished_at = datetime.datetime.now(datetime.timezone.utc)
    session.commit()
    return None


def _parse_iso(dt_str: str | None) -> datetime.datetime | None:
    """Parse an FPL ISO timestamp (e.g. '2025-08-11T22:15:00Z') -> aware datetime."""
    if not dt_str:
        return None
    try:
        return datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _season_start_year(dt: datetime.datetime | None) -> int:
    """FPL seasons span Aug->May; the season is named by its STARTING year
    (e.g. '25/26' -> 2025). Months Jan-Jun belong to the season that started the
    previous August. Falls back to 'now' if no anchor date is available."""
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    return dt.year if dt.month >= 7 else dt.year - 1


def _get_or_create_gameweek(session: Session, league_id, number: int) -> Gameweek:
    """Gameweeks are UUID-keyed rows scoped to (league, number). Sync only needs
    the row to exist so rosters/points can FK to it; dates/lock come later."""
    gw = (
        session.query(Gameweek)
        .filter_by(league_id=league_id, number=number)
        .one_or_none()
    )
    if not gw:
        gw = Gameweek(league_id=league_id, number=number)
        session.add(gw)
        session.flush()
    return gw


# ---------- current GW ----------
async def get_current_gw() -> int:
    """FPL's view of the current gameweek, from `/pl/event-status`.

    **`status` is not in that payload.** As of 2026-08-30 each entry carries
    `{bonus_added, date, event, leagues_updated, points}` and no `status` key at all,
    so the original `s.get("status") in ("L", "F")` filter matched nothing and this
    returned its `default=1` — forever. It looked correct for exactly as long as the
    real answer was 1, which is why it survived a whole preseason and GW1 unnoticed
    while pinning `sync_rosters` and `sync_gameweek_points` to gameweek 1.

    So the highest `event` named in the payload is the signal: FPL only lists days
    belonging to the gameweek it currently considers in play. The L/F branch is kept
    ahead of it in case those values return, since an explicit marker beats an
    inference — but it can no longer be the only source of an answer.

    Callers should prefer `services.current_gameweek`, which derives the same number
    from our own stored dates; this exists as the fallback for a league whose calendar
    hasn't synced yet.
    """
    async with httpx.AsyncClient() as client:
        st = await _get_json(client, f"{API_BASE}/pl/event-status")
    statuses = st.get("status", []) or []
    flagged = [s.get("event") for s in statuses if s.get("status") in ("L", "F")]
    if any(e for e in flagged):
        return max(e for e in flagged if e)
    events = [s.get("event") for s in statuses if s.get("event")]
    # Default 1 only when the payload names no gameweek at all (true preseason).
    return max(events) if events else 1


# ---------- players ----------
async def sync_players():
    with SessionLocal() as session:
        log = SyncLog(kind="players")
        session.add(log)
        session.commit()

        # The pool refreshes even when every season is frozen, because it has to:
        # between seasons the live pool is the ONLY way to see promoted clubs and new
        # signings, and drafting off a stale pool is worse than useless.
        #
        # This used to be gated, for a good reason that no longer applies. Back when
        # this upsert keyed on fpl_id, refreshing rewrote each row's identity to
        # whoever holds that element id now, which put the wrong names on every
        # historical team. Two things fixed that: matching on the permanent `code`
        # (phase 1a below) so a row always means one human, and player_season, which
        # freezes each finished season's identity and stats. Every historical read
        # resolves through that snapshot, so `players` is free to track the present.
        #
        # Phase 3 still only writes player_season for a league that is current AND
        # unfrozen, so a finished season's snapshot is never touched by this.

        async with httpx.AsyncClient() as client:
            data = await _get_json(client, f"{API_BASE}/bootstrap-static")
            # Prices + rich stats aren't in the DRAFT API. Pull them from the
            # classic FPL bootstrap (same element ids), best-effort.
            classic_by_id: dict = {}
            classic_teams: list = []
            try:
                classic = await _get_json(
                    client, "https://fantasy.premierleague.com/api/bootstrap-static/"
                )
                classic_by_id = {e["id"]: e for e in classic.get("elements", [])}
                classic_teams = classic.get("teams", [])
            except Exception:
                pass

        # The clubs, as rows. Prefer the classic payload: the DRAFT API's teams[] is
        # not guaranteed to carry the permanent `code`, and a club without one can't
        # be stored (see _upsert_pl_teams). Falls back to the draft payload so a
        # classic-feed outage still refreshes short names.
        _upsert_pl_teams(session, classic_teams or data.get("teams", []))

        # Build code -> name lookups from the same payload so stored rows are
        # human-readable rather than raw FPL integer codes.
        positions = {
            et["id"]: et.get("singular_name_short") or _POSITION_FALLBACK.get(et["id"])
            for et in data.get("element_types", [])
        }
        teams = {
            t["id"]: t.get("short_name") or t.get("name")
            for t in data.get("teams", [])
        }

        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        elements = data.get("elements", [])

        # -- phase 0: index what we already have -------------------------------
        by_code = {
            p.code: p for p in session.query(Player).filter(Player.code.isnot(None))
        }
        by_fpl = {
            p.fpl_id: p for p in session.query(Player).filter(Player.fpl_id.isnot(None))
        }

        def _pos(e):
            return positions.get(e.get("element_type")) or _POSITION_FALLBACK.get(
                e.get("element_type")
            )

        def _name(e):
            return e.get("web_name") or e.get("second_name") or ""

        def _full_name(e):
            """FPL's first + second name. `_name` above is web_name, the short form,
            which is all we used to keep — discovery-pick matching needs the long one
            (a manager writes "Nick Woltemade", the pool says "Woltemade"). None
            rather than "" when FPL sends neither, so "never synced" and "genuinely
            blank" stay distinguishable."""
            full = " ".join(
                part for part in (e.get("first_name"), e.get("second_name")) if part
            ).strip()
            return full or None

        # -- phase 1a: decide who will OWN each incoming element id ------------
        targets: dict[int, Player | None] = {}
        adopted = 0
        for e in elements:
            code = e.get("code")
            row = by_code.get(code) if code is not None else None
            if row is None:
                # Row not yet backfilled with a code. Adopt an existing row by
                # fpl_id ONLY when name AND position BOTH agree. fpl_id alone is
                # NOT evidence of identity: FPL reassigns element ids every season
                # and many reassignments keep the same position.
                # False negative = a duplicate row (fixable via a code backfill).
                # False positive = permanently rewriting a row that 12 FK columns
                # point at (unrecoverable). Fail safe.
                cand = by_fpl.get(e["id"])
                if (
                    cand is not None
                    and cand.code is None
                    and cand.name == _name(e)
                    and (cand.position or _pos(e)) == _pos(e)
                ):
                    row = cand
                    adopted += 1
            targets[e["id"]] = row

        # -- phase 1b: free EVERY fpl_id whose holder is not its new owner -----
        # Covers swaps (5<->12), chains (5->12->30), ids handed to a different
        # human, and players who left the PL. Without this the partial unique
        # index rejects the very first reassignment (measured against the live
        # 26/27 feed: 572 of 577 ids are held by a different row than their new
        # owner).
        freed = 0
        for fid, row in list(by_fpl.items()):
            if targets.get(fid) is not row:
                row.fpl_id = None  # ORM-level so the UPDATE is emitted
                freed += 1
        if freed:
            session.flush()

        # -- phase 2: assign (order no longer matters; conflicts are all NULL) --
        created = 0
        for e in elements:
            c = classic_by_id.get(e["id"], {})
            values = {
                "code": e.get("code"),
                "fpl_id": e["id"],
                "name": _name(e),
                "full_name": _full_name(e),
                "position": _pos(e),
                "current_team": teams.get(e.get("team")),
                "status": e.get("status") or c.get("status") or None,
                "price": c.get("now_cost"),  # now_cost from classic FPL (tenths)
                "last_season_points": e.get("total_points"),
                # rich season stats from classic bootstrap (best-effort)
                "form": c.get("form"),
                "points_per_game": c.get("points_per_game"),
                "total_points": _int(c.get("total_points")),
                "goals_scored": _int(c.get("goals_scored")),
                "assists": _int(c.get("assists")),
                "clean_sheets": _int(c.get("clean_sheets")),
                "bonus": _int(c.get("bonus")),
                "minutes": _int(c.get("minutes")),
                "ict_index": c.get("ict_index"),
                "selected_by_percent": c.get("selected_by_percent"),
                "news": c.get("news") or None,
            }
            row = targets[e["id"]]
            if row is None:
                row = Player()
                session.add(row)
                created += 1
            for k, v in values.items():
                setattr(row, k, v)
            session.flush()
            if e.get("code") is not None:
                by_code[e["code"]] = row
            by_fpl[e["id"]] = row  # phase 3 still reads this

        # -- phase 3: snapshot this season's identity --------------------------
        # Only for the live season. Frozen seasons keep the snapshot they already
        # have, which is the whole point: `players` moves on, player_season does not.
        cur = (
            session.query(League)
            .filter_by(is_current=True, sync_locked=False)
            .one_or_none()
        )
        snapped = 0
        if cur is not None:
            for e in elements:
                c = classic_by_id.get(e["id"], {})
                row = by_code.get(e.get("code")) or by_fpl.get(e["id"])
                if row is None:
                    continue
                _upsert(
                    session,
                    PlayerSeason,
                    {"league_id": cur.id, "fpl_id": e["id"]},
                    {
                        "player_id": row.id,
                        "name": row.name,
                        "position": row.position,
                        "current_team": row.current_team,
                        "price": c.get("now_cost"),
                        "status": row.status,
                        "news": row.news,
                        "total_points": _int(c.get("total_points")),
                        "goals_scored": _int(c.get("goals_scored")),
                        "assists": _int(c.get("assists")),
                        "clean_sheets": _int(c.get("clean_sheets")),
                        "bonus": _int(c.get("bonus")),
                        "minutes": _int(c.get("minutes")),
                        "form": c.get("form"),
                        "points_per_game": c.get("points_per_game"),
                        "ict_index": c.get("ict_index"),
                        "selected_by_percent": c.get("selected_by_percent"),
                    },
                )
                snapped += 1

        log.ok = True
        # Make a season boundary auditable rather than silent.
        log.notes = (
            f"{len(elements)} elements; {created} created, {adopted} adopted by "
            f"name+position, {freed} fpl_ids freed, {snapped} player_season rows"
        )
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


# ---------- real-life PL fixtures ----------
async def sync_fixtures(fpl_league_id: str | None = None):
    """Pull the PL fixture list + difficulty from the classic FPL feed so we can
    show each rostered player's upcoming opponent. Best-effort (same host already
    used for prices); skips quietly if unreachable. Teams stored as short names."""
    fpl_league_id = fpl_league_id or LEAGUE_ID
    if not fpl_league_id:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="fixtures")
        session.add(log)
        session.commit()

        league = _resolve_league(session, fpl_league_id, log)
        if not league:
            return

        async with httpx.AsyncClient() as client:
            try:
                classic = await _get_json(
                    client, "https://fantasy.premierleague.com/api/bootstrap-static/"
                )
                fixtures = await _get_json(
                    client, "https://fantasy.premierleague.com/api/fixtures/"
                )
            except Exception as exc:
                log.notes = f"classic fixtures unreachable: {exc}"
                log.finished_at = datetime.datetime.now(datetime.timezone.utc)
                session.commit()
                return

        # Same payload, second writer: keeps pl_teams fresh even in the stretch of
        # the offseason when only fixtures are worth syncing.
        _upsert_pl_teams(session, classic.get("teams", []))

        team_short = {
            t["id"]: t.get("short_name") or t.get("name")
            for t in classic.get("teams", [])
        }
        for f in fixtures:
            _upsert(
                session,
                Fixture,
                {"league_id": league.id, "fpl_fixture_id": f["id"]},
                {
                    "event": f.get("event"),
                    "kickoff_time": _parse_iso(f.get("kickoff_time")),
                    "home_team": team_short.get(f.get("team_h")),
                    "away_team": team_short.get(f.get("team_a")),
                    "home_difficulty": f.get("team_h_difficulty"),
                    "away_difficulty": f.get("team_a_difficulty"),
                    "finished": bool(f.get("finished")),
                    "started": f.get("started"),
                    "finished_provisional": f.get("finished_provisional"),
                    "home_score": f.get("team_h_score"),
                    "away_score": f.get("team_a_score"),
                    "minutes": f.get("minutes"),
                },
            )

        log.ok = True
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


# ---------- league & managers ----------
async def sync_league_and_managers(fpl_league_id: str | None = None):
    fpl_league_id = fpl_league_id or LEAGUE_ID
    if not fpl_league_id:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="league")
        session.add(log)
        session.commit()

        # A frozen row is a finished season — never re-read it from the feed.
        existing = (
            session.query(League)
            .filter_by(fpl_league_id=str(fpl_league_id))
            .one_or_none()
        )
        if existing and existing.sync_locked:
            log.ok = True
            log.notes = f"season {existing.season_year} is frozen (sync_locked); skipped"
            log.finished_at = datetime.datetime.now(datetime.timezone.utc)
            session.commit()
            return

        async with httpx.AsyncClient() as client:
            data = await _get_json(client, f"{API_BASE}/league/{fpl_league_id}/details")

        league_meta = data.get("league", {})
        draft_dt = _parse_iso(league_meta.get("draft_dt"))
        entries = data.get("league_entries", data.get("entries", []))

        # Identity gate: prove the feed is still OUR league before writing a byte.
        # Without this an id FPL has reused merges a stranger's managers,
        # standings and fixtures into our season (see LeagueIdentityError).
        if existing:
            stored_ids = [
                mid
                for (mid,) in session.query(Manager.fpl_manager_id).filter_by(
                    league_id=existing.id
                )
            ]
            ok, reason = verify_league_feed(
                stored_ids,
                [e.get("entry_id") or e.get("id") for e in entries],
                stored_season_year=existing.season_year,
                fetched_season_year=_season_start_year(draft_dt) if draft_dt else None,
            )
            if not ok:
                msg = (
                    f"Sync aborted for FPL league {fpl_league_id} "
                    f"('{league_meta.get('name', '')}'): {reason}. Nothing was "
                    f"written. If the season is over, freeze it (sync_locked) and "
                    f"roll over to the new season's league id."
                )
                log.ok = False
                log.notes = msg
                log.finished_at = datetime.datetime.now(datetime.timezone.utc)
                session.commit()
                raise LeagueIdentityError(msg)

        league = _upsert(
            session,
            League,
            {"fpl_league_id": str(fpl_league_id)},
            {
                "season_year": _season_start_year(draft_dt),
                "name": league_meta.get("name", ""),
                "draft_date": draft_dt.date() if draft_dt else None,
            },
        )
        session.flush()

        # league_entry id -> manager, so we can attach standings (which key off
        # the league_entry id, not the entry_id).
        entry_to_manager: dict[int, Manager] = {}
        for entry in entries:
            fpl_manager_id = str(entry.get("entry_id") or entry.get("id"))
            league_entry_id = entry.get("id")
            display = (
                entry.get("entry_name")
                or " ".join(
                    p
                    for p in (
                        entry.get("player_first_name"),
                        entry.get("player_last_name"),
                    )
                    if p
                )
                or ""
            )
            manager = _upsert(
                session,
                Manager,
                {"league_id": league.id, "fpl_manager_id": fpl_manager_id},
                {
                    "name": display,
                    "fpl_league_entry_id": str(league_entry_id)
                    if league_entry_id is not None
                    else None,
                },
            )
            session.flush()
            if league_entry_id is not None:
                entry_to_manager[league_entry_id] = manager

        # Standings snapshot (H2H). One upserted row per manager.
        for s in data.get("standings", []):
            manager = entry_to_manager.get(s.get("league_entry"))
            if not manager:
                continue
            _upsert(
                session,
                Standing,
                {"league_id": league.id, "manager_id": manager.id},
                {
                    "rank": s.get("rank"),
                    "last_rank": s.get("last_rank"),
                    "rank_sort": s.get("rank_sort"),
                    "total": s.get("total"),
                    "points_for": s.get("points_for"),
                    "points_against": s.get("points_against"),
                    "matches_played": s.get("matches_played"),
                    "matches_won": s.get("matches_won"),
                    "matches_drawn": s.get("matches_drawn"),
                    "matches_lost": s.get("matches_lost"),
                    "updated_at": datetime.datetime.now(datetime.timezone.utc),
                },
            )

        # Regular-season H2H matches (one per pairing per GW). winning_league_entry
        # is left null by the API, so derive the winner from points.
        for mt in data.get("matches", []):
            home = entry_to_manager.get(mt.get("league_entry_1"))
            away = entry_to_manager.get(mt.get("league_entry_2"))
            if not home or not away:
                continue
            gw = _get_or_create_gameweek(session, league.id, mt.get("event"))
            hp, ap = mt.get("league_entry_1_points"), mt.get("league_entry_2_points")
            winner_id = None
            if mt.get("finished") and hp is not None and ap is not None and hp != ap:
                winner_id = home.id if hp > ap else away.id
            _upsert(
                session,
                Match,
                {
                    "gameweek_id": gw.id,
                    "home_manager_id": home.id,
                    "away_manager_id": away.id,
                },
                {
                    "league_id": league.id,
                    "home_points": hp,
                    "away_points": ap,
                    "winner_id": winner_id,
                    "finished": bool(mt.get("finished")),
                },
            )

        log.ok = True
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


# ---------- gameweek dates (from bootstrap events) ----------
async def sync_gameweek_dates(fpl_league_id: str | None = None):
    """Populate gameweeks.start_date/end_date from the bootstrap event deadlines.
    A GW spans from its own deadline to the next GW's deadline."""
    fpl_league_id = fpl_league_id or LEAGUE_ID
    if not fpl_league_id:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="gameweek_dates")
        session.add(log)
        session.commit()

        league = _resolve_league(session, fpl_league_id, log)
        if not league:
            return

        async with httpx.AsyncClient() as client:
            data = await _get_json(client, f"{API_BASE}/bootstrap-static")
        events_payload = data.get("events", {})
        events = (
            events_payload.get("data", [])
            if isinstance(events_payload, dict)
            else events_payload
        )
        events = sorted(events, key=lambda e: e.get("id", 0))
        deadlines = {e["id"]: _parse_iso(e.get("deadline_time")) for e in events}

        for e in events:
            num = e["id"]
            start = deadlines.get(num)
            end = deadlines.get(num + 1)  # next GW's deadline; None for the last GW
            gw = _get_or_create_gameweek(session, league.id, num)
            gw.start_date = start.date() if start else None
            gw.end_date = end.date() if end else None

        log.ok = True
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


# ---------- rosters (snapshot a gw) ----------
async def sync_rosters(gw_number: int | None = None, fpl_league_id: str | None = None):
    """Snapshot each manager's roster for a gameweek. Defaults to the current GW;
    pass a number to (re)sync a specific GW (used by backfill)."""
    fpl_league_id = fpl_league_id or LEAGUE_ID
    if not fpl_league_id:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="rosters")
        session.add(log)
        session.commit()

        league = _resolve_league(session, fpl_league_id, log)
        if not league:
            return

        if gw_number is None:
            # The app's canonical answer, derived from stored gameweek dates — the
            # same one /scoreboard, /my-team and the transactions diff all read. Sync
            # must not resolve this differently from its readers: when it did, it
            # wrote gameweek 1 forever while every page asked for gameweek 2 and found
            # nothing. `get_current_gw` remains the fallback for a league whose
            # calendar hasn't been synced yet.
            gw_number = services.current_gameweek(session, league) or await get_current_gw()
        gameweek = _get_or_create_gameweek(session, league.id, gw_number)
        managers = session.query(Manager).filter_by(league_id=league.id).all()

        async with httpx.AsyncClient() as client:
            for m in managers:
                # Public per-entry endpoint (CLAUDE.md); /my-team requires auth.
                data = await _get_json(
                    client,
                    f"{API_BASE}/entry/{m.fpl_manager_id}/event/{gw_number}",
                )
                for p in data.get("picks", []):
                    player = (
                        session.query(Player)
                        .filter_by(fpl_id=p["element"])
                        .one_or_none()
                    )
                    if not player:
                        continue
                    # Upsert by (manager, player, gameweek): one roster slot per
                    # player per GW snapshot. source/keeper flags are league-custom
                    # and filled by the rules engine later, not here.
                    _upsert(
                        session,
                        Roster,
                        {
                            "manager_id": m.id,
                            "player_id": player.id,
                            "gameweek_id": gameweek.id,
                        },
                        {},
                    )

        log.ok = True
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


# ---------- gameweek points + minutes (feeds anti-tanking) ----------
async def sync_gameweek_points(gw_number: int | None = None, fpl_league_id: str | None = None):
    """Store per-manager points for a gameweek, including each pick's minutes and
    lineup position in `player_points` JSONB. The anti-tanking rule reads minutes
    from here across gameweeks, so this is what makes infractions a precomputed
    query. Defaults to the current GW; pass a number to (re)sync a specific GW."""
    fpl_league_id = fpl_league_id or LEAGUE_ID
    if not fpl_league_id:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="gameweek_points")
        session.add(log)
        session.commit()

        league = _resolve_league(session, fpl_league_id, log)
        if not league:
            return

        if gw_number is None:
            # The app's canonical answer, derived from stored gameweek dates — the
            # same one /scoreboard, /my-team and the transactions diff all read. Sync
            # must not resolve this differently from its readers: when it did, it
            # wrote gameweek 1 forever while every page asked for gameweek 2 and found
            # nothing. `get_current_gw` remains the fallback for a league whose
            # calendar hasn't been synced yet.
            gw_number = services.current_gameweek(session, league) or await get_current_gw()
        gameweek = _get_or_create_gameweek(session, league.id, gw_number)
        managers = session.query(Manager).filter_by(league_id=league.id).all()

        async with httpx.AsyncClient() as client:
            live = await _get_json(client, f"{API_BASE}/event/{gw_number}/live")
            # elements is keyed by player id as a string.
            live_stats = live.get("elements", {})

            def _minutes(fpl_id: int) -> int:
                return (live_stats.get(str(fpl_id), {}).get("stats", {}) or {}).get(
                    "minutes", 0
                )

            def _points(fpl_id: int) -> int:
                return (live_stats.get(str(fpl_id), {}).get("stats", {}) or {}).get(
                    "total_points", 0
                )

            def _stat(fpl_id: int, key: str) -> int:
                return (live_stats.get(str(fpl_id), {}).get("stats", {}) or {}).get(key, 0) or 0

            # Feed the must-return alert (docs/DESIGN_IL_OWNERSHIP.md §6): `live_stats`
            # already carries minutes for EVERY player in the game, not just the ones on
            # a manager's roster, so an absent player's minutes are sitting in the same
            # payload that just fetched everyone else's — they were simply never read.
            # Deliberately NOT folded into player_points below: that list means "FPL's
            # lineup" to rules.zero_minute_count, and widening it would silently change
            # the anti-tanking rule. Split out as a plain function of (session, league,
            # live_stats, gw_number) so it's testable without a live HTTP call.
            services.record_absentee_minutes(session, league, live_stats, gw_number)

            for m in managers:
                data = await _get_json(
                    client, f"{API_BASE}/entry/{m.fpl_manager_id}/event/{gw_number}"
                )
                picks = sorted(
                    data.get("picks", []), key=lambda p: p.get("position", 99)
                )
                player_points = [
                    {
                        "fpl_id": p["element"],
                        "position": p.get("position"),
                        "is_starting": (p.get("position") or 99) <= 11,
                        "minutes": _minutes(p["element"]),
                        "points": _points(p["element"]),
                    }
                    for p in picks
                ]
                total = (data.get("entry_history") or {}).get("points")
                if total is None:
                    total = sum(pp["points"] for pp in player_points if pp["is_starting"])
                # team tiebreak totals over the STARTING XI (cup tiebreakers)
                starters = [pp["fpl_id"] for pp in player_points if pp["is_starting"]]
                team_goals = sum(_stat(fid, "goals_scored") for fid in starters)
                team_assists = sum(_stat(fid, "assists") for fid in starters)
                team_cs = sum(_stat(fid, "clean_sheets") for fid in starters)
                _upsert(
                    session,
                    GameweekPoints,
                    {"manager_id": m.id, "gameweek_id": gameweek.id},
                    {"total_points": total, "player_points": player_points,
                     "team_goals": team_goals, "team_assists": team_assists,
                     "team_clean_sheets": team_cs},
                )

        log.ok = True
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


async def backfill_gameweek_points(start: int = 1, end: int = 38):
    """One-off: populate gameweek_points history so the across-gameweeks
    anti-tanking rule has data. During a live season the cron accumulates this
    one GW at a time; this backfills a completed season's range."""
    for gw in range(start, end + 1):
        await sync_gameweek_points(gw)


async def backfill_rosters(start: int = 1, end: int = 38):
    """One-off: populate per-GW roster snapshots for a completed season's range."""
    for gw in range(start, end + 1):
        await sync_rosters(gw)


async def backfill_history(start: int = 1, end: int = 38):
    """Full historical backfill: league/standings/matches, gameweek dates, and
    per-GW rosters + points. Run once for a completed season."""
    await sync_players()
    await sync_league_and_managers()  # standings + matches
    await sync_gameweek_dates()
    await backfill_rosters(start, end)
    await backfill_gameweek_points(start, end)
    await sync_trades()


# ---------- trades (canonical, from FPL draft trades feed) ----------
async def sync_trades(fpl_league_id: str | None = None):
    """Pull accepted trades from the FPL Draft trades feed into `trades` — one row
    per moved player (from_manager -> to_manager, at the trade's GW). Keeper
    derivation uses these so a traded-away player isn't counted as a drop."""
    fpl_league_id = fpl_league_id or LEAGUE_ID
    if not fpl_league_id:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="trades")
        session.add(log)
        session.commit()

        league = _resolve_league(session, fpl_league_id, log)
        if not league:
            return

        async with httpx.AsyncClient() as client:
            data = await _get_json(
                client, f"{API_BASE}/draft/league/{fpl_league_id}/trades"
            )

        mgr_by_entry = {
            m.fpl_manager_id: m
            for m in session.query(Manager).filter_by(league_id=league.id)
        }
        player_by_fpl = {p.fpl_id: p for p in session.query(Player)}

        def _record(tid, event, player, from_mgr, to_mgr):
            if not player or not from_mgr or not to_mgr:
                return
            # Reconcile: if the same player move was already entered on the SITE
            # (a manual player trade, no fpl_trade_id), link it to this FPL trade
            # instead of creating a duplicate.
            manual = (
                session.query(Trade)
                .filter_by(
                    league_id=league.id, player_id=player.id,
                    from_manager=from_mgr.id, to_manager=to_mgr.id,
                    fpl_trade_id=None, pick_round=None,
                )
                .first()
            )
            if manual:
                manual.fpl_trade_id = tid
                manual.event_gw = event
                return

            # A row the commissioner has corrected is left exactly as-is. The upsert
            # below would otherwise rewrite event_gw back to the feed's value, and the
            # reconciliation above only matches an exact (player, from, to) triple —
            # so a corrected DIRECTION would sail past it and land as a duplicate.
            edited = (
                session.query(Trade)
                .filter_by(league_id=league.id, fpl_trade_id=tid, manually_edited=True)
                .first()
            )
            if edited:
                return
            _upsert(
                session,
                Trade,
                {
                    "fpl_trade_id": tid,
                    "player_id": player.id,
                    "from_manager": from_mgr.id,
                    "to_manager": to_mgr.id,
                },
                {"league_id": league.id, "event_gw": event},
            )

        for t in data.get("trades", []):
            if t.get("state") != "p":  # only processed/accepted trades
                continue
            offered = mgr_by_entry.get(str(t.get("offered_entry")))
            received = mgr_by_entry.get(str(t.get("received_entry")))
            if not offered or not received:
                continue
            tid, event = str(t.get("id")), t.get("event")
            for item in t.get("tradeitem_set", []):
                # element_in moves INTO the offering team (from the receiver);
                # element_out moves the other way.
                _record(tid, event, player_by_fpl.get(item.get("element_in")), received, offered)
                _record(tid, event, player_by_fpl.get(item.get("element_out")), offered, received)

        log.ok = True
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


# ---------- orchestrator ----------
def _current_league_id() -> tuple[str | None, str]:
    """(fpl_league_id, how) for the season sync should target — the DATABASE first,
    the env only as a bootstrap.

    `leagues.is_current` is the app's source of truth for the active season
    (`services.current_league` has always preferred it); `sync_all` was the one path
    that didn't, and took `FPL_DRAFT_LEAGUE_ID` directly. After a rollover that env
    still names the OUTGOING league, which is now `sync_locked` — so every sub-task
    took the frozen-skip branch, and that branch sets `log.ok = True`. The 26/27
    rollover ran a full day of green cron runs syncing nothing before anyone noticed.

    Resolving from the row removes the manual step entirely: flipping `is_current`
    (which `advance_season` already does) is now the only thing a rollover needs.
    The env remains for a database with no league rows yet, which is the only case
    that can't answer the question itself.
    """
    with SessionLocal() as session:
        row = session.query(League).filter_by(is_current=True).one_or_none()
        if row is not None:
            return row.fpl_league_id, "is_current"
    return LEAGUE_ID, "env FPL_DRAFT_LEAGUE_ID (no current league row)"


async def sync_all(fpl_league_id: str | None = None):
    how = "caller"
    if fpl_league_id is None:
        fpl_league_id, how = _current_league_id()
    if how != "caller":
        # Recorded so "which league did the cron actually sync?" is answerable from
        # the log rather than by inferring it from what didn't change.
        with SessionLocal() as session:
            session.add(SyncLog(
                kind="resolve_league", ok=True,
                finished_at=datetime.datetime.now(datetime.timezone.utc),
                notes=f"targeting league {fpl_league_id} via {how}",
            ))
            session.commit()
    await sync_players()
    await sync_league_and_managers(fpl_league_id=fpl_league_id)  # also standings + matches
    await sync_gameweek_dates(fpl_league_id=fpl_league_id)
    await sync_fixtures(fpl_league_id=fpl_league_id)
    await sync_rosters(fpl_league_id=fpl_league_id)
    await sync_gameweek_points(fpl_league_id=fpl_league_id)
    await sync_trades(fpl_league_id=fpl_league_id)


def run_sync(force: bool = False) -> dict:
    """The shared body behind `POST /admin/sync` — advance the time/GW-driven phase,
    then sync per the fixture-aligned cadence plan (full | live | skip); `force=True`
    always does a full sync. Lives here, not in main.py/ui.py, so both the
    X-Auth-Token cron route and a session-authenticated admin-panel button can call
    the exact same orchestration without either importing the other (main.py already
    imports ui.py, so the reverse would be circular) and without duplicating the
    post-sync hooks below.

    Raises LeagueIdentityError on a feed that no longer looks like our league — the
    caller decides how to surface that (409 for the cron, a rendered error for the
    button)."""
    advanced = False
    plan = "full"
    with SessionLocal() as db:
        league = services.current_league(db)
        if league:
            advanced = services.advance_phase_if_due(db, league)
            plan = "full" if force else services.sync_plan(db, league)

    if plan == "full":
        asyncio.run(sync_all())
    elif plan == "live":
        async def _live():
            await sync_rosters()
            await sync_gameweek_points()
            await sync_fixtures()

        asyncio.run(_live())

    if plan in ("full", "live"):
        # rosters just refreshed: re-flag post-draft additions and auto-return any
        # IL / international player the manager has re-added in FPL. Skipped for a
        # frozen season — its roster data is final and the player pool has moved on.
        with SessionLocal() as db:
            league = services.current_league(db)
            if league and not league.sync_locked:
                if plan == "full":
                    services.flag_ineligible(db, league)
                services.reconcile_absences(db, league)
            if plan == "full":
                # Deliberately OUTSIDE the sync_locked guard above. That guard is
                # about the current league's ROSTER data being final; this reads the
                # global player pool (which sync_players just refreshed, frozen
                # seasons or not) against discovery picks that may live on an older
                # league row entirely. Its relevance doesn't depend on the current
                # season's freeze state — in fact the offseason, when everything is
                # frozen, is exactly when September's picks start arriving in the PL.
                # Only ever writes suggestions; never links a pick.
                services.match_discovery_picks(db)
    # plan == "skip": nothing to do (phase advance already ran). Note the daily
    # "full" run calls sync_players first, so the global player pool refreshes once a
    # day even while every season is frozen — that's what keeps promoted clubs and new
    # signings arriving between seasons.

    # Outbound Discord, OUTSIDE the plan branch: a trade entered on the site at noon
    # shouldn't wait for tomorrow's 06:00 full sync to be announced, and both sweeps
    # are cheap no-ops (one indexed query each) when there is nothing new. Deliberately
    # last, and it cannot raise — see discord_bridge's module docstring.
    #
    # Its own session: `league` above belongs to a session that has already closed, and
    # the announcer commits per row. run_outbound applies the frozen-season skip and
    # the feature-off check itself, so there is nothing to guard here.
    import discord_bridge

    with SessionLocal() as db:
        current = services.current_league(db)
        discord = {
            "in": discord_bridge.run_inbound(db, current),
            "out": discord_bridge.run_outbound(db, current),
        }
    return {"ok": True, "plan": plan, "phase_advanced": advanced, "discord": discord}
