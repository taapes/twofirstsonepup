"""The Players tab shows the last COMPLETED season's statistics.

`players` is global and only ever holds the current season, so after a rollover its
stat columns are zero for months — precisely when the owner is drafting on last
season's production. `player_portal` therefore reads statistics from the most
recently completed season's `player_season` snapshot, while identity and league
context still come from the live pool.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import uuid

import pytest

import services
from models import League, Player, PlayerSeason


def _player(session, name, code, fpl_id, team="ARS", pos="DEF"):
    p = Player(name=name, code=code, fpl_id=fpl_id, current_team=team, position=pos,
               price=50, status="a")
    session.add(p)
    session.flush()
    return p


def _snapshot(session, league, player, **stats):
    ps = PlayerSeason(
        league_id=league.id, player_id=player.id, fpl_id=player.fpl_id,
        name=player.name, position=player.position, current_team=player.current_team,
        price=player.price, status=player.status, **stats,
    )
    session.add(ps)
    session.flush()
    return ps


def _league(session, fpl_id, year, *, current, locked):
    lg = League(fpl_league_id=fpl_id, name=f"S{year}", season_year=year,
                is_current=current, sync_locked=locked)
    session.add(lg)
    session.flush()
    return lg


# --------------------------------------------------------------------------
def test_stats_season_prefers_the_completed_season_over_the_live_one(test_session):
    """The core regression: mid-rollover the live season has no numbers yet, so the
    tab must fall back to the season that does."""
    old = _league(test_session, "1", 2025, current=False, locked=True)
    new = _league(test_session, "2", 2026, current=True, locked=False)
    p = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, old, p, total_points=209, minutes=2750)
    _snapshot(test_session, new, p, total_points=None, minutes=None)
    test_session.commit()

    assert services.stats_season(test_session, new).id == old.id
    assert services.season_label(old) == "25/26"


def test_stats_season_skips_a_completed_season_with_no_stats(test_session):
    """A frozen season whose snapshot was captured before stats existed must not
    win just for being the newest — that would blank the tab."""
    with_stats = _league(test_session, "1", 2025, current=False, locked=True)
    empty = _league(test_session, "2", 2026, current=False, locked=True)
    p = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, with_stats, p, total_points=209, minutes=2750)
    _snapshot(test_session, empty, p, total_points=None, minutes=None)
    test_session.commit()

    assert services.stats_season(test_session, empty).id == with_stats.id


def test_stats_season_falls_back_to_the_given_league(test_session):
    """Fresh deployment: nothing frozen yet, so there is nothing better to use."""
    only = _league(test_session, "1", 2026, current=True, locked=False)
    test_session.commit()
    assert services.stats_season(test_session, only).id == only.id


def test_portal_takes_stats_from_last_season_and_identity_from_the_live_pool(
    test_session,
):
    """The whole point: after a rollover you draft on last season's numbers, but you
    need this season's club to know who you're drafting."""
    old = _league(test_session, "1", 2025, current=False, locked=True)
    new = _league(test_session, "2", 2026, current=True, locked=False)
    # the player moved clubs between seasons
    p = _player(test_session, "Gabriel", 111, 5, team="LIV")
    _snapshot(test_session, old, p, total_points=209, minutes=2750, goals_scored=3,
              assists=5, clean_sheets=12, bonus=18, points_per_game="6.5")
    test_session.commit()

    (row,) = services.player_portal(test_session, new)
    assert row["total_points"] == 209 and row["minutes"] == 2750
    assert row["goals_scored"] == 3 and row["points_per_game"] == 6.5
    assert row["team"] == "LIV", "identity must come from the live pool, not the snapshot"


def test_portal_renders_a_player_with_no_snapshot_row_as_blank(test_session):
    """Players who left the PL keep no stats. That must be blank, not a crash."""
    old = _league(test_session, "1", 2025, current=False, locked=True)
    kept = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, old, kept, total_points=209, minutes=2750)
    _player(test_session, "Departed", None, 9)   # no code, no snapshot row
    test_session.commit()

    rows = {r["name"]: r for r in services.player_portal(test_session, old)}
    assert rows["Gabriel"]["total_points"] == 209
    assert rows["Departed"]["total_points"] is None
    assert rows["Departed"]["minutes"] is None
    assert rows["Departed"]["name"] == "Departed"   # still listed, just without stats


def test_portal_ignores_another_season_s_snapshot(test_session):
    """Stats must be scoped to the resolved season, not merged across all of them."""
    old = _league(test_session, "1", 2025, current=False, locked=True)
    older = _league(test_session, "9", 2024, current=False, locked=True)
    p = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, older, p, total_points=1, minutes=1)
    _snapshot(test_session, old, p, total_points=209, minutes=2750)
    test_session.commit()

    (row,) = services.player_portal(test_session, old)
    assert row["total_points"] == 209, "picked up the wrong season's snapshot"
