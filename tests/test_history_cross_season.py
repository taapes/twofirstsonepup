"""History page must show data from all league rows, not just the current one.

After rollover, the current league row is the new season with no history. History
is by definition cross-season and should query across all league rows.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import services
from models import (
    CupMatch,
    DiscoveryResult,
    HistoricalStanding,
    League,
    ManagerHonors,
    SeasonHistory,
)


def _league(session, *, season_year, is_current=False):
    lg = League(
        fpl_league_id=str(season_year),
        name=f"S{season_year}",
        season_year=season_year,
        is_current=is_current,
        sync_locked=not is_current,
        phase="offseason",
    )
    session.add(lg)
    session.flush()
    return lg


def test_history_reads_season_history_from_old_rows(test_session):
    """After rollover, calling get_history with the current (new) league row should
    still return SeasonHistory from the old row."""
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)

    test_session.add(
        SeasonHistory(
            league_id=old.id,
            year="2025",
            league_winner="Old League Winner",
            cup_winner="Old Cup Winner",
            pup_winner="Old Pup Winner",
        )
    )
    test_session.commit()

    # Call with the new (current) league, expect to see the old data
    result = services.get_history(test_session, new)
    assert len(result["seasons"]) == 1
    assert result["seasons"][0]["year"] == "2025"
    assert result["seasons"][0]["league"] == "Old League Winner"


def test_history_reads_manager_honors_from_old_rows(test_session):
    """ManagerHonors from old rows should be visible."""
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)

    test_session.add(
        ManagerHonors(
            league_id=old.id, manager_name="Alice", titles=2, cups=1
        )
    )
    test_session.commit()

    result = services.get_history(test_session, new)
    assert len(result["honors"]) == 1
    assert result["honors"][0]["manager"] == "Alice"


def test_history_reads_historical_standings_from_old_rows(test_session):
    """HistoricalStanding from old rows should be visible."""
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)

    test_session.add(
        HistoricalStanding(
            league_id=old.id,
            year="2025",
            rank=1,
            manager_name="Alice",
            team_name="Alice's Team",
            wins=5,
            draws=2,
            losses=1,
            points_for=500,
            h2h_points=100,
        )
    )
    test_session.commit()

    result = services.get_history(test_session, new)
    assert len(result["standings_by_season"]) == 1
    assert result["standings_by_season"][0]["year"] == "2025"
    assert result["standings_by_season"][0]["rows"][0]["manager"] == "Alice"


def test_history_reads_cup_matches_from_old_rows(test_session):
    """CupMatch from old rows should be visible."""
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)

    test_session.add(
        CupMatch(
            league_id=old.id,
            season="2025",
            bracket="cup",
            round=1,
            slot=1,
            seed=1,
            manager_label="Alice vs Bob",
            total=150,
        )
    )
    test_session.commit()

    result = services.get_history(test_session, new)
    assert len(result["cups_by_season"]) == 1
    assert result["cups_by_season"][0]["year"] == "2025"
    assert len(result["cups_by_season"][0]["rows"]) == 1


def test_history_reads_discovery_results_from_old_rows(test_session):
    """DiscoveryResult from old rows should be visible."""
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)

    test_session.add(
        DiscoveryResult(
            league_id=old.id,
            season="2025",
            pick_number=1,
            round=1,
            manager_name="Alice",
            player_name="Haaland",
        )
    )
    test_session.commit()

    result = services.get_history(test_session, new)
    assert len(result["discovery_by_season"]) == 1
    assert result["discovery_by_season"][0]["year"] == "2025"
    assert len(result["discovery_by_season"][0]["picks"]) == 1


def test_history_dedupes_duplicate_years(test_session):
    """Two rows for one year only happen around a rollover, when the same history is
    imported onto both the outgoing and incoming league row — and the incoming one is
    the correction, so it must win. Deterministically: before the League join the
    winner was whatever order Postgres returned, which in practice was the STALE row.
    """
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)

    # Add the same year to both rows (shouldn't happen, but test dedup anyway)
    test_session.add(
        SeasonHistory(
            league_id=old.id,
            year="2025",
            league_winner="Old Winner",
            cup_winner="Old Cup",
            pup_winner="Old Pup",
        )
    )
    test_session.add(
        SeasonHistory(
            league_id=new.id,
            year="2025",
            league_winner="New Winner",
            cup_winner="New Cup",
            pup_winner="New Pup",
        )
    )
    test_session.commit()

    result = services.get_history(test_session, new)
    assert len(result["seasons"]) == 1
    # The first one should win (newer row's query runs first due to order)
    assert result["seasons"][0]["league"] == "New Winner"


def test_history_newest_season_first(test_session):
    """Seasons should be ordered newest first."""
    old = _league(test_session, season_year=2024)
    mid = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)

    for lg, year, name in [
        (old, "2024", "2024 Winner"),
        (mid, "2025", "2025 Winner"),
        (new, "2026", "2026 Winner"),
    ]:
        test_session.add(
            SeasonHistory(
                league_id=lg.id,
                year=year,
                league_winner=name,
                cup_winner="Cup",
                pup_winner="Pup",
            )
        )
    test_session.commit()

    result = services.get_history(test_session, new)
    years = [s["year"] for s in result["seasons"]]
    assert years == ["2026", "2025", "2024"]
