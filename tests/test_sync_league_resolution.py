"""Which league does a cron sync actually target?

`services.current_league` has always preferred `leagues.is_current`, but `sync_all`
was the one path that didn't — it read `FPL_DRAFT_LEAGUE_ID` directly. After the
26/27 rollover that env still named the OUTGOING league, which is now `sync_locked`,
so every sub-task took the frozen-skip branch. That branch sets `log.ok = True`, so
the cron ran a full day of green runs syncing nothing (observed 2026-08-19 06:48:
six sub-tasks, all "season 2025 is frozen (sync_locked); skipped", all ok).

Resolving from the row instead means flipping `is_current` — which `advance_season`
already does — is the only thing a rollover needs. The env survives only for a
database with no league rows yet, which is the one case that can't answer the
question itself.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import sync
from models import League, SyncLog


def _league(session, *, fpl, season_year, is_current=False, locked=False):
    lg = League(fpl_league_id=str(fpl), name=f"S{season_year}",
                season_year=season_year, is_current=is_current,
                sync_locked=locked, phase="preseason")
    session.add(lg)
    session.commit()
    return lg


def test_the_current_row_wins_over_a_stale_env(test_session, monkeypatch):
    """The exact production shape: env still on the old frozen league, is_current on
    the new one."""
    _league(test_session, fpl=1754, season_year=2025, locked=True)
    _league(test_session, fpl=11818, season_year=2026, is_current=True)
    monkeypatch.setattr(sync, "LEAGUE_ID", "1754")

    league_id, how = sync._current_league_id()
    assert league_id == "11818", "a rollover must not need an env change"
    assert how == "is_current"


def test_the_env_is_still_the_bootstrap_for_an_empty_database(test_session, monkeypatch):
    """A database with no league rows can't answer the question itself — this is the
    only case the env still exists for."""
    monkeypatch.setattr(sync, "LEAGUE_ID", "1754")
    league_id, how = sync._current_league_id()
    assert league_id == "1754"
    assert "env" in how


def test_no_current_row_and_no_env_resolves_to_nothing(test_session, monkeypatch):
    """Rather than silently picking an arbitrary league row."""
    _league(test_session, fpl=1754, season_year=2025)
    monkeypatch.setattr(sync, "LEAGUE_ID", None)
    league_id, _how = sync._current_league_id()
    assert league_id is None


def test_an_explicit_caller_id_is_never_overridden(test_session, monkeypatch):
    """The rollover route passes the new id explicitly, before is_current has moved —
    resolution must not second-guess it."""
    _league(test_session, fpl=1754, season_year=2025, is_current=True)
    monkeypatch.setattr(sync, "LEAGUE_ID", "1754")

    seen = {}

    async def _capture(fpl_league_id=None, **kw):
        seen.setdefault("ids", []).append(fpl_league_id)

    for name in ("sync_players", "sync_league_and_managers", "sync_gameweek_dates",
                 "sync_fixtures", "sync_rosters", "sync_gameweek_points",
                 "sync_trades"):
        monkeypatch.setattr(sync, name, _capture)

    import asyncio
    asyncio.run(sync.sync_all(fpl_league_id="99999"))
    assert set(i for i in seen["ids"] if i is not None) == {"99999"}


def test_sync_all_targets_the_current_row_and_records_which(test_session, monkeypatch):
    """End to end, plus the log line: 'which league did the cron sync?' should be
    answerable from the log rather than inferred from what didn't change."""
    _league(test_session, fpl=1754, season_year=2025, locked=True)
    _league(test_session, fpl=11818, season_year=2026, is_current=True)
    monkeypatch.setattr(sync, "LEAGUE_ID", "1754")

    seen = []

    async def _capture(fpl_league_id=None, **kw):
        seen.append(fpl_league_id)

    for name in ("sync_players", "sync_league_and_managers", "sync_gameweek_dates",
                 "sync_fixtures", "sync_rosters", "sync_gameweek_points",
                 "sync_trades"):
        monkeypatch.setattr(sync, name, _capture)

    import asyncio
    asyncio.run(sync.sync_all())
    assert set(i for i in seen if i is not None) == {"11818"}

    note = (
        test_session.query(SyncLog)
        .filter_by(kind="resolve_league")
        .order_by(SyncLog.started_at.desc())
        .first()
    )
    assert note is not None
    assert "11818" in note.notes and "is_current" in note.notes
