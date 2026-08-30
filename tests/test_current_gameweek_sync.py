"""Sync must resolve the current gameweek the same way its readers do.

THE INCIDENT (found 2026-08-30, live in production): `sync.get_current_gw` filtered
FPL's `/pl/event-status` entries on `s.get("status") in ("L", "F")`. That payload has
**no `status` key** — each entry is `{bonus_added, date, event, leagues_updated,
points}` — so the filter matched nothing and the function returned its `default=1`
every single time.

It looked correct for exactly as long as the real answer was 1. Through preseason and
GW1 nothing was wrong; the moment GW2 began, `sync_rosters` and `sync_gameweek_points`
kept writing gameweek 1 while `/scoreboard`, `/transactions`, the keeper derivation and
anti-tanking all asked `services.current_gameweek` for gameweek 2 and found nothing.
Both sync tasks logged ok=True throughout, because they had successfully synced — the
wrong gameweek. The visible symptom was the scoreboard's "left to play" silently
rendering nothing.

Two fixes, both pinned below: the payload is read for what it actually contains, and
sync now prefers the same derivation its readers use, so the two cannot disagree.
"""

import asyncio
import datetime as dt

import services
import sync
from models import Gameweek, GameweekPoints, League, Manager, Player, Roster

# The real payload, captured from the live API on 2026-08-30 during GW2.
REAL_PAYLOAD = {
    "status": [
        {"bonus_added": False, "date": "2026-08-28", "event": 2,
         "leagues_updated": True, "points": "p"},
        {"bonus_added": False, "date": "2026-08-29", "event": 2,
         "leagues_updated": True, "points": "p"},
        {"bonus_added": False, "date": "2026-08-30", "event": 2,
         "leagues_updated": True, "points": "p"},
        {"bonus_added": False, "date": "2026-08-31", "event": 2,
         "leagues_updated": False, "points": ""},
    ],
    "leagues": "",
}


def _patch_payload(monkeypatch, payload):
    async def fake(client, url):
        return payload

    monkeypatch.setattr(sync, "_get_json", fake)


def test_the_real_payload_resolves_to_the_gameweek_it_names(monkeypatch):
    """The regression itself. This returned 1 in production while the payload said 2."""
    _patch_payload(monkeypatch, REAL_PAYLOAD)
    assert asyncio.run(sync.get_current_gw()) == 2


def test_an_explicit_live_or_finished_marker_still_wins(monkeypatch):
    """Kept ahead of the inference in case FPL restores the field — an explicit marker
    beats reading intent off which days are listed."""
    _patch_payload(monkeypatch, {"status": [
        {"event": 5, "status": "F"},
        {"event": 6, "status": "L"},
        {"event": 7},
    ]})
    assert asyncio.run(sync.get_current_gw()) == 6


def test_an_empty_payload_is_preseason(monkeypatch):
    """Default 1 is right ONLY when the payload names no gameweek at all. Its old job —
    absorbing a field that never existed — is what made the bug invisible."""
    _patch_payload(monkeypatch, {"status": []})
    assert asyncio.run(sync.get_current_gw()) == 1
    _patch_payload(monkeypatch, {})
    assert asyncio.run(sync.get_current_gw()) == 1


def _seed(session, *, today_gw=2):
    lg = League(fpl_league_id="70", name="L", season_year=2026, is_current=True,
                sync_locked=False, phase="in_season")
    session.add(lg)
    session.flush()
    # Dates behind "now" so the derivation lands on `today_gw`.
    for n in (1, 2, 3):
        session.add(Gameweek(
            number=n, league_id=lg.id,
            start_date=dt.date.today() - dt.timedelta(days=(today_gw - n) * 7 + 1),
        ))
    session.add(Manager(league_id=lg.id, fpl_manager_id="1", name="A", display_name="Ann"))
    session.commit()
    return lg


def test_sync_and_the_site_agree_on_the_current_gameweek(test_session):
    """The property that makes the class of bug impossible, not just this instance:
    sync resolves the gameweek through the SAME function every reader uses."""
    lg = _seed(test_session, today_gw=2)
    assert services.current_gameweek(test_session, lg) == 2


def test_health_flags_a_gameweek_the_site_reads_but_sync_never_wrote(test_session):
    """The check that would have caught this on day one. The old one asserted
    `count > 0` across ALL gameweeks, so GW1's rows kept it green while GW2 was
    empty — 'sync worked once' rather than 'sync is working'."""
    lg = _seed(test_session, today_gw=2)
    gw1 = test_session.query(Gameweek).filter_by(number=1).one()
    mgr = test_session.query(Manager).one()
    p = Player(fpl_id=1, code=1000, name="X", position="MID")
    test_session.add(p)
    test_session.flush()
    # Exactly the production state: GW1 populated, GW2 untouched.
    test_session.add(Roster(manager_id=mgr.id, player_id=p.id, gameweek_id=gw1.id))
    test_session.add(GameweekPoints(manager_id=mgr.id, gameweek_id=gw1.id,
                                    total_points=50))
    test_session.commit()

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["gameweek points populated"]["ok"] is True, "the old check stays green"
    assert checks["rosters synced for the current gameweek"]["ok"] is False
    assert checks["gameweek points synced for the current gameweek"]["ok"] is False
    assert "GW2" in checks["rosters synced for the current gameweek"]["detail"]


def test_health_is_satisfied_once_the_current_gameweek_is_written(test_session):
    lg = _seed(test_session, today_gw=2)
    gw2 = test_session.query(Gameweek).filter_by(number=2).one()
    mgr = test_session.query(Manager).one()
    p = Player(fpl_id=1, code=1000, name="X", position="MID")
    test_session.add(p)
    test_session.flush()
    test_session.add(Roster(manager_id=mgr.id, player_id=p.id, gameweek_id=gw2.id))
    test_session.add(GameweekPoints(manager_id=mgr.id, gameweek_id=gw2.id,
                                    total_points=50))
    test_session.commit()

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["rosters synced for the current gameweek"]["ok"] is True
    assert checks["gameweek points synced for the current gameweek"]["ok"] is True


# ---- the fix that kills the CLASS, not just this instance ---------------------
# Without these two, both call sites could revert to `await get_current_gw()` and the
# entire suite would stay green — which is precisely how the original bug survived. The
# tests above pin the payload PARSING; these pin that sync asks the same question its
# readers do, which is the property that makes a future divergence impossible rather
# than merely unlikely.

def _run_sync_task(task, monkeypatch, *, fpl_gw, seen):
    """Drive a sync task with FPL disagreeing with our stored calendar.

    `get_current_gw` is forced to the WRONG answer, so a task that consults it lands on
    the wrong gameweek and a task that consults `services.current_gameweek` does not.
    """
    async def fake_current_gw():
        seen["asked_fpl"] = True
        return fpl_gw

    async def fake_get_json(client, url):
        seen.setdefault("urls", []).append(url)
        # Enough shape for both tasks; no player rows exist, so picks are skipped.
        return {"picks": [], "elements": {}}

    monkeypatch.setattr(sync, "get_current_gw", fake_current_gw)
    monkeypatch.setattr(sync, "_get_json", fake_get_json)
    asyncio.run(task(fpl_league_id="70"))


def test_sync_rosters_uses_the_gameweek_the_site_reads(test_session, monkeypatch):
    """The regression, at the call site. FPL says 1, our calendar says 2 — the snapshot
    must land on 2, because 2 is what /scoreboard and the transactions diff ask for."""
    lg = _seed(test_session, today_gw=2)
    seen: dict = {}
    _run_sync_task(sync.sync_rosters, monkeypatch, fpl_gw=1, seen=seen)

    # The per-entry URL carries the gameweek, so it shows which one was resolved.
    assert seen["urls"], "the task should have fetched something"
    assert all(url.endswith("/event/2") for url in seen["urls"]), seen["urls"]


def test_sync_gameweek_points_uses_the_gameweek_the_site_reads(test_session, monkeypatch):
    lg = _seed(test_session, today_gw=2)
    seen: dict = {}
    _run_sync_task(sync.sync_gameweek_points, monkeypatch, fpl_gw=1, seen=seen)

    assert any("/event/2/live" in url for url in seen["urls"]), seen["urls"]
    assert not any("/event/1/live" in url for url in seen["urls"]), seen["urls"]


def test_fpl_is_still_the_fallback_when_we_have_no_calendar(test_session, monkeypatch):
    """A league whose gameweek dates haven't synced yet has no derivation to use, and
    falling back is right there — the bug was never that FPL is consulted, only that it
    was consulted INSTEAD of our own answer."""
    lg = League(fpl_league_id="70", name="L", season_year=2026, is_current=True,
                sync_locked=False, phase="in_season")
    test_session.add(lg)
    test_session.flush()
    test_session.add(Manager(league_id=lg.id, fpl_manager_id="1", name="A",
                             display_name="Ann"))
    test_session.commit()
    assert services.current_gameweek(test_session, lg) is None, "no dated gameweeks"

    seen: dict = {}
    _run_sync_task(sync.sync_rosters, monkeypatch, fpl_gw=7, seen=seen)
    assert seen.get("asked_fpl") is True
    assert all(url.endswith("/event/7") for url in seen["urls"]), seen["urls"]
