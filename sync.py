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
    Gameweek,
    GameweekPoints,
    League,
    Manager,
    Match,
    Player,
    Roster,
    Standing,
    SyncLog,
    Trade,
)
from settings import API_BASE, LEAGUE_ID

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
    async with httpx.AsyncClient() as client:
        st = await _get_json(client, f"{API_BASE}/pl/event-status")
    statuses = st.get("status", [])
    # Last gameweek that is live ("L") or finished ("F"); default to 1 preseason.
    return max(
        (s.get("event", 0) for s in statuses if s.get("status") in ("L", "F")),
        default=1,
    )


# ---------- players ----------
async def sync_players():
    with SessionLocal() as session:
        log = SyncLog(kind="players")
        session.add(log)
        session.commit()
        async with httpx.AsyncClient() as client:
            data = await _get_json(client, f"{API_BASE}/bootstrap-static")
            # Prices aren't in the DRAFT API (no budget). Pull now_cost from the
            # classic FPL bootstrap (same element ids), best-effort.
            prices: dict = {}
            try:
                classic = await _get_json(
                    client, "https://fantasy.premierleague.com/api/bootstrap-static/"
                )
                prices = {e["id"]: e.get("now_cost") for e in classic.get("elements", [])}
            except Exception:
                pass

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

        for e in data.get("elements", []):
            match = {"fpl_id": e["id"]}
            values = {
                "name": e.get("web_name") or e.get("second_name") or "",
                "position": positions.get(e.get("element_type"))
                or _POSITION_FALLBACK.get(e.get("element_type")),
                "current_team": teams.get(e.get("team")),
                "status": e.get("status") or None,
                "price": prices.get(e["id"]),  # now_cost from classic FPL (tenths)
                "last_season_points": e.get("total_points"),
            }
            _upsert(session, Player, match, values)

        log.ok = True
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


# ---------- league & managers ----------
async def sync_league_and_managers():
    if not LEAGUE_ID:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="league")
        session.add(log)
        session.commit()
        async with httpx.AsyncClient() as client:
            data = await _get_json(client, f"{API_BASE}/league/{LEAGUE_ID}/details")

        league_meta = data.get("league", {})
        draft_dt = _parse_iso(league_meta.get("draft_dt"))
        league = _upsert(
            session,
            League,
            {"fpl_league_id": str(LEAGUE_ID)},
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
        for entry in data.get("league_entries", data.get("entries", [])):
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
async def sync_gameweek_dates():
    """Populate gameweeks.start_date/end_date from the bootstrap event deadlines.
    A GW spans from its own deadline to the next GW's deadline."""
    if not LEAGUE_ID:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="gameweek_dates")
        session.add(log)
        session.commit()

        league = (
            session.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one_or_none()
        )
        if not league:
            log.notes = "league missing, run sync_league_and_managers first"
            log.finished_at = datetime.datetime.now(datetime.timezone.utc)
            session.commit()
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
async def sync_rosters(gw_number: int | None = None):
    """Snapshot each manager's roster for a gameweek. Defaults to the current GW;
    pass a number to (re)sync a specific GW (used by backfill)."""
    if not LEAGUE_ID:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="rosters")
        session.add(log)
        session.commit()

        league = (
            session.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one_or_none()
        )
        if not league:
            log.notes = "league missing, run sync_league_and_managers first"
            log.finished_at = datetime.datetime.now(datetime.timezone.utc)
            session.commit()
            return

        if gw_number is None:
            gw_number = await get_current_gw()
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
async def sync_gameweek_points(gw_number: int | None = None):
    """Store per-manager points for a gameweek, including each pick's minutes and
    lineup position in `player_points` JSONB. The anti-tanking rule reads minutes
    from here across gameweeks, so this is what makes infractions a precomputed
    query. Defaults to the current GW; pass a number to (re)sync a specific GW."""
    if not LEAGUE_ID:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="gameweek_points")
        session.add(log)
        session.commit()

        league = (
            session.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one_or_none()
        )
        if not league:
            log.notes = "league missing, run sync_league_and_managers first"
            log.finished_at = datetime.datetime.now(datetime.timezone.utc)
            session.commit()
            return

        if gw_number is None:
            gw_number = await get_current_gw()
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
                _upsert(
                    session,
                    GameweekPoints,
                    {"manager_id": m.id, "gameweek_id": gameweek.id},
                    {"total_points": total, "player_points": player_points},
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
async def sync_trades():
    """Pull accepted trades from the FPL Draft trades feed into `trades` — one row
    per moved player (from_manager -> to_manager, at the trade's GW). Keeper
    derivation uses these so a traded-away player isn't counted as a drop."""
    if not LEAGUE_ID:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="trades")
        session.add(log)
        session.commit()

        league = (
            session.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one_or_none()
        )
        if not league:
            log.notes = "league missing, run sync_league_and_managers first"
            log.finished_at = datetime.datetime.now(datetime.timezone.utc)
            session.commit()
            return

        async with httpx.AsyncClient() as client:
            data = await _get_json(
                client, f"{API_BASE}/draft/league/{LEAGUE_ID}/trades"
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
async def sync_all():
    await sync_players()
    await sync_league_and_managers()  # also standings + matches
    await sync_gameweek_dates()
    await sync_rosters()
    await sync_gameweek_points()
    await sync_trades()
