"""A frozen season is never synced, and a recycled league id never writes.

Two guards, both from the August 2026 incident where FPL's reuse of league id 1754
merged twelve foreign managers and 228 foreign fixtures into a finished season:
`leagues.sync_locked` (checked before any HTTP call) and `rules.verify_league_feed`
(checked before any write).

CONVERTED 2026-08-31 off the production database. This was the genuinely dangerous one
of the three: its `frozen_league` fixture took the LIVE current league, flipped
`sync_locked=True` on it and committed — then relied on a `finally` to put it back. A
crash between those points left production frozen. It now seeds its own league.

The conversion is the pattern tests/test_sync_league_resolution.py already proves: sync
tasks open their own sessions via `sync.SessionLocal`, which conftest's `test_session`
patches, so their internal commits land in the test database and can be read back. The
old `clean_logs` fixture is gone entirely — the fixture truncates.
"""

import asyncio

import pytest

import sync
from models import League, Manager, SyncLog

# Every league-scoped sync task. Deliberately NOT sync_players — see the test below.
TASKS = (
    sync.sync_league_and_managers,
    sync.sync_gameweek_dates,
    sync.sync_rosters,
    sync.sync_gameweek_points,
    sync.sync_trades,
)

FPL_ID = "9999"


@pytest.fixture
def frozen_league(test_session):
    """A seeded, frozen league. Never the live one."""
    lg = League(fpl_league_id=FPL_ID, name="Frozen", season_year=2025,
                is_current=True, sync_locked=True, phase="offseason")
    test_session.add(lg)
    test_session.flush()
    for i in range(1, 11):
        test_session.add(Manager(league_id=lg.id, fpl_manager_id=str(1000 + i),
                                 name=f"Team {i}", display_name=f"M{i}"))
    test_session.commit()
    return FPL_ID


@pytest.fixture
def no_network(monkeypatch):
    """Any FPL call is a failure: the freeze must short-circuit before HTTP."""
    async def _boom(*a, **k):
        raise AssertionError("sync called the FPL API for a frozen season")

    monkeypatch.setattr(sync, "_get_json", _boom)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.__name__)
def test_a_frozen_season_is_never_synced(task, frozen_league, no_network, test_session):
    asyncio.run(task(fpl_league_id=frozen_league))  # `no_network` raises if it calls out

    log = test_session.query(SyncLog).order_by(SyncLog.started_at.desc()).first()
    assert log is not None, "the skip should still be recorded"
    assert log.ok is True, "a deliberate freeze is a skip, not a failure"
    assert "frozen" in (log.notes or "")


def test_the_freeze_covers_league_data_but_NOT_the_global_player_pool():
    """The freeze is deliberately scoped to league-owned data.

    `sync_players` used to be gated too, because it keyed on fpl_id and refreshing
    rewrote every historical roster's names. That is fixed at the root now (it matches
    on the permanent `code`, and player_season freezes each finished season), and the
    gate had to go: between seasons the live feed is the only source of promoted clubs
    and new signings. See tests/test_pool_refresh.py for the refresh behaviour.

    Pure assertion, no fixtures — it used to take `frozen_league` and so committed a
    lock to production for a check that touches no database at all.
    """
    assert sync.sync_players not in TASKS


def test_a_foreign_feed_aborts_before_writing(frozen_league, test_session, monkeypatch):
    """With the freeze lifted, the identity gate is the second line of defence."""
    lg = test_session.query(League).filter_by(fpl_league_id=frozen_league).one()
    lg.sync_locked = False
    test_session.commit()
    league_id, name, season = lg.id, lg.name, lg.season_year
    managers_before = _manager_names(test_session, league_id)

    # The actual 'Rottehulen' shape: a wholly different set of entries.
    async def _foreign(client, url):
        return {
            "league": {"name": "Rottehulen", "draft_dt": "2026-08-09T18:00:00Z"},
            "league_entries": [
                {"id": 4880, "entry_id": 4877, "entry_name": "Suppe Steg & Is"},
                {"id": 4972, "entry_id": 4969, "entry_name": "Armut's Army"},
            ],
            "standings": [{"league_entry": 4880, "rank": 1}],
            "matches": [],
        }

    monkeypatch.setattr(sync, "_get_json", _foreign)

    with pytest.raises(sync.LeagueIdentityError) as exc:
        asyncio.run(sync.sync_league_and_managers(fpl_league_id=frozen_league))

    # EITHER branch is a pass. The assertion used to demand "reused league id" (the
    # season-jump branch); post-rollover the real league's season_year matched what this
    # fabricated feed derives, so the jump stopped tripping and the entry-overlap branch
    # fired instead — a stale assertion about a working guard (docs/BACKLOG.md:941).
    # Seeding season_year=2025 against a 2026 feed makes the jump trip again, but
    # pinning the branch rather than the OUTCOME is what made it brittle, so don't.
    message = str(exc.value)
    assert "reused league id" in message or "known managers" in message, message

    test_session.expire_all()
    lg = test_session.query(League).filter_by(id=league_id).one()
    assert (lg.name, lg.season_year) == (name, season), "league row was mutated"
    assert _manager_names(test_session, league_id) == managers_before, \
        "foreign managers were written"

    log = test_session.query(SyncLog).order_by(SyncLog.started_at.desc()).first()
    assert log.ok is False and "aborted" in (log.notes or "")


def _manager_names(session, league_id):
    return {n for (n,) in session.query(Manager.name).filter_by(league_id=league_id)}
