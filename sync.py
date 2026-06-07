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
from models import Gameweek, League, Manager, Player, Roster, SyncLog
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

        league = _upsert(
            session,
            League,
            {"fpl_league_id": str(LEAGUE_ID)},
            {
                "season_year": datetime.datetime.now(datetime.timezone.utc).year,
                "name": data.get("league", {}).get("name", ""),
            },
        )
        session.flush()

        for entry in data.get("league_entries", data.get("entries", [])):
            fpl_manager_id = str(entry.get("entry_id") or entry.get("id"))
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
            _upsert(
                session,
                Manager,
                {"league_id": league.id, "fpl_manager_id": fpl_manager_id},
                {"name": display},
            )

        log.ok = True
        log.finished_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()


# ---------- rosters (snapshot current gw) ----------
async def sync_rosters_current_gw():
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


# ---------- orchestrator ----------
async def sync_all():
    await sync_players()
    await sync_league_and_managers()
    await sync_rosters_current_gw()
