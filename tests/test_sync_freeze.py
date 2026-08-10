"""Sync must not touch a frozen (finished) season, and must refuse a feed that
isn't ours. Regression for Aug 2026, when FPL reused our 25/26 league id for a
stranger's league and three nightly syncs merged it into our season.

These hit the configured DB (sync commits internally) and clean up the SyncLog
rows they create.
"""

import asyncio

import pytest

import sync
from db import SessionLocal
from models import League, SyncLog

TASKS = (
    sync.sync_league_and_managers,
    sync.sync_gameweek_dates,
    sync.sync_rosters,
    sync.sync_gameweek_points,
    sync.sync_trades,
)


@pytest.fixture
def frozen_league():
    """The current league, forced frozen for the test and restored after."""
    db = SessionLocal()
    try:
        lg = db.query(League).filter_by(is_current=True).first() or db.query(League).first()
        if lg is None:
            pytest.skip("no league in the configured DB")
        was, lg.sync_locked = lg.sync_locked, True
        db.commit()
        yield lg.fpl_league_id
        db.query(League).filter_by(id=lg.id).update({"sync_locked": was})
        db.commit()
    finally:
        db.close()


@pytest.fixture
def no_network(monkeypatch):
    """Any FPL call is a failure: the freeze must short-circuit before HTTP."""
    async def _boom(*a, **k):
        raise AssertionError("sync called the FPL API for a frozen season")

    monkeypatch.setattr(sync, "_get_json", _boom)


@pytest.fixture
def clean_logs():
    before = SessionLocal()
    try:
        seen = {i for (i,) in before.query(SyncLog.id)}
    finally:
        before.close()
    yield
    db = SessionLocal()
    try:
        db.query(SyncLog).filter(SyncLog.id.notin_(seen or {-1})).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.__name__)
def test_frozen_season_is_never_synced(task, frozen_league, no_network, clean_logs):
    asyncio.run(task(fpl_league_id=frozen_league))  # raises if it hits the API

    db = SessionLocal()
    try:
        log = db.query(SyncLog).order_by(SyncLog.started_at.desc()).first()
        assert log.ok is True, "a deliberate freeze is a skip, not a failure"
        assert "frozen" in (log.notes or "")
    finally:
        db.close()


def test_player_pool_not_refreshed_when_every_season_is_frozen(
    frozen_league, no_network, clean_logs
):
    """FPL reassigns element ids each season, and `players` is global and keyed on
    fpl_id — so refreshing the pool with no live season rewrites every historical
    roster's names in place. This is what put the wrong players on every team.
    """
    asyncio.run(sync.sync_players())  # raises if it hits the API

    db = SessionLocal()
    try:
        log = db.query(SyncLog).order_by(SyncLog.started_at.desc()).first()
        assert log.kind == "players" and log.ok is True
        assert "frozen" in (log.notes or "")
    finally:
        db.close()


def test_foreign_feed_aborts_before_writing(frozen_league, clean_logs, monkeypatch):
    """With the freeze lifted, the identity gate is the second line of defence."""
    db = SessionLocal()
    try:
        lg = db.query(League).filter_by(fpl_league_id=frozen_league).one()
        lg.sync_locked = False
        db.commit()
        league_id, name, season = lg.id, lg.name, lg.season_year
        managers_before = _manager_names(db, league_id)

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
        # The season jump trips first; the entry overlap would catch it anyway.
        assert "reused league id" in str(exc.value)

        db.expire_all()
        lg = db.query(League).filter_by(id=league_id).one()
        assert (lg.name, lg.season_year) == (name, season), "league row was mutated"
        assert _manager_names(db, league_id) == managers_before, "managers were added"

        log = db.query(SyncLog).order_by(SyncLog.started_at.desc()).first()
        assert log.ok is False and "aborted" in (log.notes or "")
    finally:
        db.query(League).filter_by(fpl_league_id=frozen_league).update(
            {"sync_locked": True}
        )
        db.commit()
        db.close()


def _manager_names(db, league_id):
    from models import Manager

    return {n for (n,) in db.query(Manager.name).filter_by(league_id=league_id)}
