import asyncio, datetime
import httpx
from sqlalchemy.orm import Session
from db import SessionLocal
from models import Player, League, Manager, RosterSlot, SyncLog
from settings import API_BASE, LEAGUE_ID

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

# ---------- current GW ----------
async def get_current_gw():
    async with httpx.AsyncClient() as client:
        # Either works; /game has season-wide info, /pl/event-status has live/next
        # Prefer event-status to get 'next' quickly if needed
        st = await _get_json(client, f"{API_BASE}/pl/event-status")
        # event-status looks like {"status":[{"event":<gw>,"status":"L"/"F"/"U"}], "leagues":...}
        # Find the max event with status != "U" (i.e., last finished/locked), then +1 if we want current/next.
        statuses = st.get("status", [])
        # Fallback if shape differs:
        current_gw = max([s.get("event", 0) for s in statuses if s.get("status") in ("L","F")], default=1)
        return current_gw

# ---------- players ----------
async def sync_players():
    with SessionLocal() as session:
        log = SyncLog(kind="players"); session.add(log); session.commit()
        async with httpx.AsyncClient() as client:
            data = await _get_json(client, f"{API_BASE}/bootstrap-static")
        elements = data.get("elements", [])
        for e in elements:
            match = {"fpl_id": e["id"]}
            values = {
                "name": e.get("web_name") or e.get("second_name") or "",
                "position": str(e.get("element_type")),
                "current_team": str(e.get("team")),
                "status": e.get("status") or ""
            }
            _upsert(session, Player, match, values)
        log.ok = True; log.finished_at = datetime.datetime.utcnow(); session.commit()

# ---------- league & managers ----------
async def sync_league_and_managers():
    if not LEAGUE_ID:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="league"); session.add(log); session.commit()
        async with httpx.AsyncClient() as client:
            data = await _get_json(client, f"{API_BASE}/league/{LEAGUE_ID}/details")
        league_name = data.get("league", {}).get("name", "")
        league = _upsert(session, League, {"fpl_league_id": str(LEAGUE_ID)}, {
            "season_year": datetime.datetime.utcnow().year,
            "name": league_name
        })
        session.flush()
        for entry in data.get("entries", []):
            _upsert(session, Manager,
                {"league_id": league.id, "fpl_manager_id": str(entry["entry_id"])},
                {"name": entry.get("entry_name") or entry.get("player_first_name","")})
        log.ok = True; log.finished_at = datetime.datetime.utcnow(); session.commit()

# ---------- rosters (snapshot current gw) ----------
async def sync_rosters_current_gw():
    if not LEAGUE_ID:
        return
    with SessionLocal() as session:
        log = SyncLog(kind="rosters"); session.add(log); session.commit()
        league = session.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).first()
        if not league:
            log.notes = "league missing, run sync_league_and_managers first"
            session.commit(); return
        gw = await get_current_gw()
        managers = session.query(Manager).filter_by(league_id=league.id).all()
        async with httpx.AsyncClient() as client:
            for m in managers:
                data = await _get_json(client, f"{API_BASE}/entry/{m.fpl_manager_id}/my-team")
                picks = data.get("picks", [])
                for p in picks:
                    fpl_player_id = p["element"]
                    player = session.query(Player).filter_by(fpl_id=fpl_player_id).one_or_none()
                    if not player:
                        continue
                    slot = session.query(RosterSlot).filter_by(
                        league_id=league.id, manager_id=m.id, player_id=player.id, gw=gw
                    ).one_or_none()
                    if not slot:
                        slot = RosterSlot(
                            league_id=league.id, manager_id=m.id, player_id=player.id,
                            gw=gw, origin="waiver"  # placeholder; refine later
                        )
                        session.add(slot)
        log.ok = True; log.finished_at = datetime.datetime.utcnow(); session.commit()

# ---------- orchestrator ----------
async def sync_all():
    await sync_players()
    await sync_league_and_managers()
    await sync_rosters_current_gw()
