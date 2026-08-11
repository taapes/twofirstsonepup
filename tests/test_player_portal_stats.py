"""Which season's statistics the Players tab and the draft board show.

`players` is global and only ever holds the current season, so after a rollover its
stat columns are zero for months — precisely when the draft happens. So both surfaces
resolve statistics through `stats_season`: the live season once it has kicked off
(phase `in_season`), otherwise the most recent completed season's `player_season`
snapshot. Identity and league context still come from the live pool.

The draft always finishes before the season starts, so drafting always sees last
season's totals, and they switch to the live ones at kickoff on their own.

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


def _league(session, fpl_id, year, *, current, locked, phase="offseason"):
    lg = League(fpl_league_id=fpl_id, name=f"S{year}", season_year=year,
                is_current=current, sync_locked=locked, phase=phase)
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


# ---- the season the stats belong to flips at kickoff ----------------------
@pytest.mark.parametrize("phase", ["offseason", "draft", "preseason"])
def test_stats_season_uses_last_completed_before_kickoff(test_session, phase):
    """Through the draft there is no live data yet, so last season's totals are what
    you draft on."""
    old = _league(test_session, "1", 2025, current=False, locked=True)
    new = _league(test_session, "2", 2026, current=True, locked=False, phase=phase)
    p = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, old, p, total_points=209, minutes=2750)
    test_session.commit()

    assert services.stats_season(test_session, new).id == old.id


def test_stats_season_switches_to_the_live_season_once_it_starts(test_session):
    """The whole point of the change: once the season is under way its own running
    totals are the relevant ones, even though a completed season still exists."""
    old = _league(test_session, "1", 2025, current=False, locked=True)
    live = _league(test_session, "2", 2026, current=True, locked=False,
                   phase="in_season")
    p = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, old, p, total_points=209, minutes=2750)
    _snapshot(test_session, live, p, total_points=12, minutes=180)
    test_session.commit()

    assert services.stats_season(test_session, live).id == live.id
    (row,) = services.player_portal(test_session, live)
    assert row["total_points"] == 12, "still showing last season after kickoff"


def test_finished_season_still_shows_its_own_numbers(test_session):
    """After GW38 the phase drops to offseason and sync_locked goes on in the same
    pass — the season must resolve to itself, not to nothing."""
    done = _league(test_session, "1", 2025, current=True, locked=True,
                   phase="offseason")
    p = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, done, p, total_points=209, minutes=2750)
    test_session.commit()

    assert services.stats_season(test_session, done).id == done.id


# ---- draft board search ---------------------------------------------------
def test_search_points_come_from_the_resolved_season(test_session):
    old = _league(test_session, "1", 2025, current=False, locked=True)
    new = _league(test_session, "2", 2026, current=True, locked=False,
                  phase="preseason")
    p = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, old, p, total_points=209, minutes=2750)
    test_session.commit()

    (row,) = services.search_players(test_session, new, q="Gab")
    assert row["points"] == 209


def test_search_still_returns_a_player_with_no_snapshot(test_session):
    """A player absent from the stats season (new to the PL, or never code-matched)
    must still be searchable — otherwise they silently become undraftable. This is
    what an inner join to player_season would break."""
    old = _league(test_session, "1", 2025, current=False, locked=True)
    new = _league(test_session, "2", 2026, current=True, locked=False,
                  phase="preseason")
    known = _player(test_session, "Gabriel", 111, 5)
    _snapshot(test_session, old, known, total_points=209, minutes=2750)
    _player(test_session, "Newcomer", 222, 6)   # no snapshot in any season
    test_session.commit()

    names = {r["name"]: r for r in services.search_players(test_session, new)}
    assert "Newcomer" in names, "player without stats vanished from the draft pool"
    assert names["Newcomer"]["points"] is None
    assert names["Gabriel"]["points"] == 209


def test_search_points_sort_is_desc_with_blanks_last(test_session):
    old = _league(test_session, "1", 2025, current=False, locked=True)
    new = _league(test_session, "2", 2026, current=True, locked=False,
                  phase="preseason")
    for name, code, fid, pts in [
        ("Low", 1, 1, 50), ("High", 2, 2, 200), ("Mid", 3, 3, 120), ("Blank", 4, 4, None)
    ]:
        pl = _player(test_session, name, code, fid)
        if pts is not None:
            _snapshot(test_session, old, pl, total_points=pts, minutes=1000)
    test_session.commit()

    order = [r["name"] for r in services.search_players(test_session, new, sort="points")]
    assert order == ["High", "Mid", "Low", "Blank"], order


def test_search_points_sort_runs_before_the_limit(test_session):
    """Sorting after truncation would return an arbitrary subset — the top scorer
    must survive a limit smaller than the pool."""
    old = _league(test_session, "1", 2025, current=False, locked=True)
    new = _league(test_session, "2", 2026, current=True, locked=False,
                  phase="preseason")
    for i in range(10):
        pl = _player(test_session, f"P{i:02d}", 100 + i, 100 + i)
        _snapshot(test_session, old, pl, total_points=i * 10, minutes=100)
    test_session.commit()

    top = services.search_players(test_session, new, sort="points", limit=3)
    assert [r["points"] for r in top] == [90, 80, 70]
