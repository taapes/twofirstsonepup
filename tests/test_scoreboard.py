"""Scores-page upgrade: real PL fixture progress, per-manager players-remaining
(starting XI only, blank-GW players excluded), and an honest last-synced
timestamp — replacing the old, misleading "live"/"final" label that was really
just the H2H scoring-lock flag (`Match.finished`), unrelated to any real match.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime

import pytest

import rules
import services
from models import (
    Fixture,
    Gameweek,
    GameweekPoints,
    League,
    Manager,
    Match,
    Player,
    PlayerSeason,
    SyncLog,
)

GW = 10


def _league(session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="in_season")
    session.add(lg)
    session.flush()
    gw = Gameweek(league_id=lg.id, number=GW)
    session.add(gw)
    session.flush()
    return lg, gw


def _manager(session, lg, name, fpl_id="1"):
    m = Manager(league_id=lg.id, fpl_manager_id=fpl_id, name=name, display_name=name)
    session.add(m)
    session.flush()
    return m


def _player(session, lg, fpl_id, name, position, team):
    p = Player(code=900000 + fpl_id, fpl_id=fpl_id, name=name,
               position=position, current_team=team)
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id,
                             name=name, position=position, current_team=team))
    return p


def _fixture(session, lg, fid, home, away, *, finished=False, started=None,
             kickoff=None, gw_number=GW):
    session.add(Fixture(league_id=lg.id, fpl_fixture_id=fid, event=gw_number,
                        home_team=home, away_team=away, finished=finished,
                        started=started, kickoff_time=kickoff))


# ---- rules.fixture_status ---------------------------------------------------

class _Fx:
    def __init__(self, finished=False, started=None, finished_provisional=None):
        self.finished = finished
        self.started = started
        self.finished_provisional = finished_provisional


def test_fixture_status_finished_wins_outright():
    assert rules.fixture_status(_Fx(finished=True, started=True)) == "finished"


def test_fixture_status_finished_provisional_also_counts_as_finished():
    """The exact bug hit in production: FPL's classic feed can sit at
    finished=False, finished_provisional=True, minutes=90 for HOURS after full
    time while bonus points are locked in. Waiting on `finished` alone left
    every just-played match reading as 'not started.' `finished_provisional`
    must be enough on its own, `finished` unset entirely."""
    assert rules.fixture_status(
        _Fx(finished=False, started=True, finished_provisional=True)
    ) == "finished"


def test_fixture_status_in_progress_when_started_and_not_finished():
    assert rules.fixture_status(_Fx(finished=False, started=True)) == "in_progress"


def test_fixture_status_not_started_when_neither():
    assert rules.fixture_status(_Fx(finished=False, started=False)) == "not_started"


def test_fixture_status_treats_null_started_as_not_started():
    """A row synced before the live-state migration has started=NULL — must not
    raise, and must read as not-yet-started rather than crashing every caller."""
    assert rules.fixture_status(_Fx(finished=False, started=None)) == "not_started"


# ---- services.gw_fixture_progress -------------------------------------------

def test_gw_fixture_progress_counts_and_sorts_by_kickoff(test_session):
    lg, gw = _league(test_session)
    _fixture(test_session, lg, 1, "ARS", "CHE", finished=True,
             kickoff=datetime.datetime(2025, 10, 5, 14, 0, tzinfo=datetime.timezone.utc))
    _fixture(test_session, lg, 2, "MCI", "LIV", started=True,
             kickoff=datetime.datetime(2025, 10, 5, 11, 30, tzinfo=datetime.timezone.utc))
    _fixture(test_session, lg, 3, "AVL", "BHA", started=False,
             kickoff=datetime.datetime(2025, 10, 5, 16, 0, tzinfo=datetime.timezone.utc))
    # A different GW's fixture must not leak in.
    _fixture(test_session, lg, 4, "EVE", "FUL", finished=True, gw_number=GW + 1)
    test_session.commit()

    out = services.gw_fixture_progress(test_session, lg, GW)
    assert out["counts"] == {"total": 3, "finished": 1, "in_progress": 1, "not_started": 1}
    assert [f["home"] for f in out["fixtures"]] == ["MCI", "ARS", "AVL"]  # kickoff order


# ---- services._club_status_by_gw --------------------------------------------

def test_double_gw_club_resolves_to_earliest_non_finished_status(test_session):
    lg, gw = _league(test_session)
    _fixture(test_session, lg, 1, "ARS", "CHE", finished=True)
    _fixture(test_session, lg, 2, "ARS", "MCI", started=True)  # ARS's 2nd leg, live
    test_session.commit()

    status = services._club_status_by_gw(test_session, lg, GW)
    assert status["ARS"] == "in_progress"  # never silently "finished"
    assert status["CHE"] == "finished"
    assert status["MCI"] == "in_progress"


# ---- services.players_remaining_by_manager ----------------------------------

def _gwpoints(session, gw, mgr, entries):
    session.add(GameweekPoints(manager_id=mgr.id, gameweek_id=gw.id,
                               total_points=50, player_points=entries))


def test_starting_xi_only_bench_excluded_even_at_zero_minutes(test_session):
    lg, gw = _league(test_session)
    m = _manager(test_session, lg, "Ann")
    _player(test_session, lg, 1, "Starter", "MID", "ARS")
    _player(test_session, lg, 2, "Bencher", "MID", "ARS")
    _fixture(test_session, lg, 1, "ARS", "CHE", started=True)
    _gwpoints(test_session, gw, m, [
        {"fpl_id": 1, "position": 5, "is_starting": True, "minutes": 0, "points": 0},
        {"fpl_id": 2, "position": 12, "is_starting": False, "minutes": 0, "points": 0},
    ])
    test_session.commit()

    out = services.players_remaining_by_manager(test_session, lg, GW)
    assert out[m.id]["total"] == 1
    assert out[m.id]["remaining"] == 1
    assert out[m.id]["in_progress"] == 1
    assert out[m.id]["playing_now"] == ["Starter"]


def test_blank_gw_player_excluded_from_total_and_remaining(test_session):
    lg, gw = _league(test_session)
    m = _manager(test_session, lg, "Ann")
    _player(test_session, lg, 1, "HasFixture", "MID", "ARS")
    _player(test_session, lg, 2, "BlankGW", "MID", "NFO")  # NFO has no fixture this GW
    _fixture(test_session, lg, 1, "ARS", "CHE", finished=True)
    _gwpoints(test_session, gw, m, [
        {"fpl_id": 1, "position": 1, "is_starting": True, "minutes": 90, "points": 6},
        {"fpl_id": 2, "position": 2, "is_starting": True, "minutes": 0, "points": 0},
    ])
    test_session.commit()

    out = services.players_remaining_by_manager(test_session, lg, GW)
    assert out[m.id]["total"] == 1  # BlankGW excluded entirely, not counted as "done"
    assert out[m.id]["remaining"] == 0


def test_double_gw_player_with_one_leg_finished_still_counts_as_remaining(test_session):
    lg, gw = _league(test_session)
    m = _manager(test_session, lg, "Ann")
    _player(test_session, lg, 1, "DoubleGW", "FWD", "ARS")
    _fixture(test_session, lg, 1, "ARS", "CHE", finished=True)
    _fixture(test_session, lg, 2, "ARS", "MCI", started=False)
    _gwpoints(test_session, gw, m, [
        {"fpl_id": 1, "position": 1, "is_starting": True, "minutes": 90, "points": 6},
    ])
    test_session.commit()

    out = services.players_remaining_by_manager(test_session, lg, GW)
    assert out[m.id]["remaining"] == 1  # still has the 2nd leg to play


def test_position_bucket_totals_sum_to_manager_total(test_session):
    lg, gw = _league(test_session)
    m = _manager(test_session, lg, "Ann")
    _player(test_session, lg, 1, "GK", "GKP", "ARS")
    _player(test_session, lg, 2, "D1", "DEF", "ARS")
    _player(test_session, lg, 3, "M1", "MID", "ARS")
    _fixture(test_session, lg, 1, "ARS", "CHE", finished=True)
    _gwpoints(test_session, gw, m, [
        {"fpl_id": i, "position": i, "is_starting": True, "minutes": 90, "points": 2}
        for i in (1, 2, 3)
    ])
    test_session.commit()

    out = services.players_remaining_by_manager(test_session, lg, GW)
    bucket_total = sum(b["total"] for b in out[m.id]["by_position"].values())
    assert bucket_total == out[m.id]["total"] == 3


def test_remaining_players_carry_next_kickoff_sorted_soonest_first(test_session):
    """A not-yet-started remaining player shows his club's next kickoff; an
    in-progress player is covered by `playing_now` instead and stays out of
    this list entirely."""
    lg, gw = _league(test_session)
    m = _manager(test_session, lg, "Ann")
    _player(test_session, lg, 1, "Later", "MID", "ARS")
    _player(test_session, lg, 2, "Sooner", "MID", "MCI")
    _player(test_session, lg, 3, "AlreadyOn", "MID", "LIV")
    _fixture(test_session, lg, 1, "ARS", "CHE", started=False,
             kickoff=datetime.datetime(2025, 10, 5, 16, 0, tzinfo=datetime.timezone.utc))
    _fixture(test_session, lg, 2, "MCI", "BHA", started=False,
             kickoff=datetime.datetime(2025, 10, 5, 11, 30, tzinfo=datetime.timezone.utc))
    _fixture(test_session, lg, 3, "LIV", "EVE", started=True)
    _gwpoints(test_session, gw, m, [
        {"fpl_id": i, "position": i, "is_starting": True, "minutes": 0, "points": 0}
        for i in (1, 2, 3)
    ])
    test_session.commit()

    out = services.players_remaining_by_manager(test_session, lg, GW)
    names = [p["name"] for p in out[m.id]["remaining_players"]]
    assert names == ["Sooner", "Later"]  # sorted by kickoff, AlreadyOn excluded
    assert out[m.id]["remaining_players"][0]["kickoff_time"] is not None


def test_remaining_players_with_unknown_kickoff_sort_last(test_session):
    lg, gw = _league(test_session)
    m = _manager(test_session, lg, "Ann")
    _player(test_session, lg, 1, "NoKickoff", "MID", "ARS")
    _player(test_session, lg, 2, "Known", "MID", "MCI")
    _fixture(test_session, lg, 1, "ARS", "CHE", started=False, kickoff=None)
    _fixture(test_session, lg, 2, "MCI", "BHA", started=False,
             kickoff=datetime.datetime(2025, 10, 5, 11, 30, tzinfo=datetime.timezone.utc))
    _gwpoints(test_session, gw, m, [
        {"fpl_id": i, "position": i, "is_starting": True, "minutes": 0, "points": 0}
        for i in (1, 2)
    ])
    test_session.commit()

    out = services.players_remaining_by_manager(test_session, lg, GW)
    names = [p["name"] for p in out[m.id]["remaining_players"]]
    assert names == ["Known", "NoKickoff"]


# ---- services.get_scoreboard: regression guard on existing keys ------------

def test_get_scoreboard_existing_keys_unchanged(test_session):
    lg, gw = _league(test_session)
    home = _manager(test_session, lg, "Ann", "1")
    away = _manager(test_session, lg, "Bob", "2")
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=home.id, away_manager_id=away.id,
                           home_points=40, away_points=30, finished=False))
    test_session.commit()

    board = services.get_scoreboard(test_session, lg, GW)
    m = board["matches"][0]
    assert m["home"] == "Ann" and m["away"] == "Bob"
    assert m["home_score"] == 40 and m["away_score"] == 30
    assert m["finished"] is False
    assert m["leader"] == "Ann"
    # new keys present and correctly shaped
    assert "home_remaining" in m and "away_remaining" in m
    assert m["home_playing_now"] == [] and m["away_playing_now"] == []
    assert "fixtures" in board and "synced_at" in board
    assert "closest" in m


def test_closest_match_flags_only_the_smallest_live_margin(test_session):
    lg, gw = _league(test_session)
    a1 = _manager(test_session, lg, "Ann", "1")
    a2 = _manager(test_session, lg, "Bob", "2")
    b1 = _manager(test_session, lg, "Cy", "3")
    b2 = _manager(test_session, lg, "Di", "4")
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=a1.id, away_manager_id=a2.id,
                           home_points=40, away_points=30, finished=False))  # margin 10
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=b1.id, away_manager_id=b2.id,
                           home_points=20, away_points=21, finished=False))  # margin 1
    test_session.commit()

    board = services.get_scoreboard(test_session, lg, GW)
    closest = {m["home"] for m in board["matches"] if m["closest"]}
    assert closest == {"Cy"}


def test_closest_match_ignores_finalized_matches(test_session):
    """A finalized H2H match isn't a 'live impact' fact — only in-progress ones
    are eligible for the closest-margin flag."""
    lg, gw = _league(test_session)
    a1 = _manager(test_session, lg, "Ann", "1")
    a2 = _manager(test_session, lg, "Bob", "2")
    b1 = _manager(test_session, lg, "Cy", "3")
    b2 = _manager(test_session, lg, "Di", "4")
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=a1.id, away_manager_id=a2.id,
                           home_points=20, away_points=20, finished=True))   # margin 0, but final
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=b1.id, away_manager_id=b2.id,
                           home_points=20, away_points=15, finished=False))  # margin 5, live
    test_session.commit()

    board = services.get_scoreboard(test_session, lg, GW)
    closest = {m["home"] for m in board["matches"] if m["closest"]}
    assert closest == {"Cy"}


# ---- services.scoreboard_freshness ------------------------------------------

def test_scoreboard_freshness_none_when_no_sync_log(test_session):
    out = services.scoreboard_freshness(test_session)
    assert out == {"points_synced_at": None, "fixtures_synced_at": None}


def test_scoreboard_freshness_returns_latest_ok_run_only(test_session):
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    older = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    test_session.add(SyncLog(kind="gameweek_points", ok=True, started_at=older))
    test_session.add(SyncLog(kind="gameweek_points", ok=True, started_at=now))
    test_session.add(SyncLog(kind="gameweek_points", ok=False,
                             started_at=datetime.datetime(2026, 1, 1, 13, 0,
                                                          tzinfo=datetime.timezone.utc)))
    test_session.add(SyncLog(kind="fixtures", ok=True, started_at=older))
    test_session.commit()

    out = services.scoreboard_freshness(test_session)
    assert out["points_synced_at"] == now  # the failed, later run is ignored
    assert out["fixtures_synced_at"] == older


# ---- route smoke test --------------------------------------------------------

def test_scoreboard_page_relabels_h2h_and_drops_the_bare_word_live(test_session):
    """The old page rendered the bare word 'live' for an unfinished H2H match --
    misleading, since that flag has nothing to do with any real PL match. The
    upgraded page must use 'H2H scoring' / 'in progress' / 'finalized' instead."""
    from fastapi.testclient import TestClient

    from auth import hash_password
    from main import app

    lg, gw = _league(test_session)
    home = _manager(test_session, lg, "Ann", "1")
    away = _manager(test_session, lg, "Bob", "2")
    home.password_hash = hash_password("pw")
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=home.id, away_manager_id=away.id,
                           home_points=40, away_points=30, finished=False))
    test_session.commit()

    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"manager_id": "1", "password": "pw"})
    r = client.get(f"/scoreboard?gw={GW}")
    assert r.status_code == 200
    assert b"H2H scoring" in r.content
    assert b">live<" not in r.content


def test_scoreboard_page_groups_finished_fixtures_and_shows_progress(test_session):
    """Finished PL fixtures collapse into their own section; the progress line
    and bar reflect the finished/total counts."""
    from fastapi.testclient import TestClient

    from auth import hash_password
    from main import app

    lg, gw = _league(test_session)
    home = _manager(test_session, lg, "Ann", "1")
    away = _manager(test_session, lg, "Bob", "2")
    home.password_hash = hash_password("pw")
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=home.id, away_manager_id=away.id,
                           home_points=40, away_points=30, finished=False))
    _fixture(test_session, lg, 1, "ARS", "CHE", finished=True)
    _fixture(test_session, lg, 2, "MCI", "LIV", started=False)
    test_session.commit()

    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"manager_id": "1", "password": "pw"})
    r = client.get(f"/scoreboard?gw={GW}")
    assert r.status_code == 200
    assert b"1 of 2 PL fixtures finished" in r.content
    assert b"finished fixture" in r.content  # the collapsed <details> summary
