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


# ---- projected auto-subs, end to end ------------------------------------------
# The pure rule is covered in tests/test_auto_subs.py. These cover the DB layer:
# resolving who is ruled out, and that the score, the leader and the "left to play"
# count all move together.

def _squad_rows(session, lg, shape, *, gw_number=GW):
    """Create players for a squad shape and return {label: Player}.

    `shape` is a list of (label, position, club) in pick order, slots 1-15.
    """
    made = {}
    for i, (label, pos, club) in enumerate(shape, start=1):
        made[label] = _player(session, lg, 500 + i, label, pos, club)
    return made


def _entries(shape, made, *, minutes=None, points=None):
    minutes, points = minutes or {}, points or {}
    return [
        {"fpl_id": made[label].fpl_id, "position": slot, "is_starting": slot <= 11,
         "minutes": minutes.get(label, 90), "points": points.get(label, 0)}
        for slot, (label, _pos, _club) in enumerate(shape, start=1)
    ]


# 1 GKP, 4 DEF, 4 MID, 2 FWD starting; bench = GK, DEF, MID, FWD.
_SHAPE = [
    ("gk", "GKP", "AAA"),
    ("d1", "DEF", "BBB"), ("d2", "DEF", "BBB"), ("d3", "DEF", "CCC"), ("d4", "DEF", "CCC"),
    ("m1", "MID", "DDD"), ("m2", "MID", "DDD"), ("m3", "MID", "EEE"), ("m4", "MID", "EEE"),
    ("f1", "FWD", "FFF"), ("f2", "FWD", "FFF"),
    ("bgk", "GKP", "GGG"), ("bd", "DEF", "GGG"), ("bm", "MID", "HHH"), ("bf", "FWD", "HHH"),
]


def test_a_blanking_starter_is_replaced_and_the_score_rises(test_session):
    """The whole feature: FPL would show 0 for the blank until it finalises."""
    lg, gw = _league(test_session)
    mgr = _manager(test_session, lg, "A", "1")
    made = _squad_rows(test_session, lg, _SHAPE)
    for club in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"):
        _fixture(test_session, lg, hash(club) % 10000, club, "ZZZ", finished=True)
    entries = _entries(_SHAPE, made, minutes={"m1": 0}, points={"m2": 5, "bd": 8})
    _gwpoints(test_session, gw, mgr, entries)
    test_session.commit()

    proj = services.projected_points_by_manager(test_session, lg, GW)[mgr.id]
    # The DEFENDER at slot 13 covers the blanking midfielder: he is ahead of the bench
    # midfielder and four defenders becoming five is legal.
    assert [s["in"]["name"] for s in proj["subs"]] == ["bd"]
    assert proj["points"] == 13, "5 from m2 plus the sub's 8"


def test_a_zero_minute_starter_whose_match_is_unfinished_is_not_subbed(test_session):
    """He may yet play. Subbing him now is the thrash the bench rule exists to avoid."""
    lg, gw = _league(test_session)
    mgr = _manager(test_session, lg, "A", "1")
    made = _squad_rows(test_session, lg, _SHAPE)
    for club in ("AAA", "BBB", "CCC", "EEE", "FFF", "GGG", "HHH"):
        _fixture(test_session, lg, hash(club) % 10000, club, "ZZZ", finished=True)
    _fixture(test_session, lg, 7777, "DDD", "ZZZ", finished=False, started=True)
    entries = _entries(_SHAPE, made, minutes={"m1": 0}, points={"bm": 8})
    _gwpoints(test_session, gw, mgr, entries)
    test_session.commit()

    proj = services.projected_points_by_manager(test_session, lg, GW)[mgr.id]
    assert proj["subs"] == []


def test_a_gameweek_with_no_fixture_rows_rules_out_nobody(test_session):
    """Missing data, not twenty blank clubs. Without this guard an unsynced fixture
    table would blank every squad and the projection would report nonsense."""
    lg, gw = _league(test_session)
    mgr = _manager(test_session, lg, "A", "1")
    made = _squad_rows(test_session, lg, _SHAPE)
    entries = _entries(_SHAPE, made, minutes={"m1": 0, "m2": 0})
    _gwpoints(test_session, gw, mgr, entries)
    test_session.commit()

    proj = services.projected_points_by_manager(test_session, lg, GW)[mgr.id]
    assert proj["subs"] == []


def test_a_projected_sub_who_has_not_kicked_off_counts_as_left_to_play(test_session):
    """The commissioner's rule made visible. On the picked XI he appears nowhere,
    because the bench is excluded — so the count used to understate exactly the
    managers this feature exists to help."""
    lg, gw = _league(test_session)
    mgr = _manager(test_session, lg, "A", "1")
    made = _squad_rows(test_session, lg, _SHAPE)
    for club in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"):
        _fixture(test_session, lg, hash(club) % 10000, club, "ZZZ", finished=True)
    # The bench midfielder's club hasn't kicked off.
    _fixture(test_session, lg, 8888, "HHH", "ZZZ", finished=False, started=False,
             kickoff=datetime.datetime(2025, 5, 1, 19, 0, tzinfo=datetime.timezone.utc))
    # bd blanked for real (GGG has finished), so he is skipped and the midfielder
    # whose club hasn't kicked off takes the slot.
    entries = _entries(_SHAPE, made, minutes={"m1": 0, "bd": 0, "bm": 0, "bf": 0})
    _gwpoints(test_session, gw, mgr, entries)
    test_session.commit()

    rem = services.players_remaining_by_manager(test_session, lg, GW)[mgr.id]
    subs = [p for p in rem["remaining_players"] if p["sub"]]
    assert [p["name"] for p in subs] == ["bm"]
    assert rem["remaining"] == 1


def test_the_scoreboard_score_is_the_projection(test_session):
    """The leader arrow follows the projected number, not FPL's raw live one."""
    lg, gw = _league(test_session)
    home = _manager(test_session, lg, "H", "1")
    away = _manager(test_session, lg, "A", "2")
    made = _squad_rows(test_session, lg, _SHAPE)
    for club in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"):
        _fixture(test_session, lg, hash(club) % 10000, club, "ZZZ", finished=True)
    # Home blanks a midfielder but has an 8-point sub; stored total says otherwise.
    _gwpoints(test_session, gw, home,
              _entries(_SHAPE, made, minutes={"m1": 0}, points={"bd": 8}))
    _gwpoints(test_session, gw, away, _entries(_SHAPE, made, points={"m2": 3}))
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=home.id, away_manager_id=away.id))
    test_session.commit()

    board = services.get_scoreboard(test_session, lg, GW)
    m = board["matches"][0]
    assert m["home_score"] == 8 and m["away_score"] == 3
    assert m["leader"] == "H"
    assert [s["in"]["name"] for s in m["home_subs"]] == ["bd"]


def test_a_finalised_gameweek_reproduces_fpls_own_total(test_session):
    """THE INVARIANT, and the reason this feature is verifiable at all.

    Once FPL finalises a gameweek its own total already includes auto-subs, while our
    stored `is_starting` still reflects the ORIGINAL XI. So on a finalised gameweek the
    projection must land on exactly FPL's number. Confirmed against production GW1 while
    this was written — all ten managers, zero delta.
    """
    lg, gw = _league(test_session)
    mgr = _manager(test_session, lg, "A", "1")
    made = _squad_rows(test_session, lg, _SHAPE)
    for club in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"):
        _fixture(test_session, lg, hash(club) % 10000, club, "ZZZ", finished=True)
    entries = _entries(_SHAPE, made, minutes={"m1": 0}, points={"m2": 5, "bd": 8})
    # What FPL would store once finalised: the XI sum WITH the sub applied.
    test_session.add(GameweekPoints(manager_id=mgr.id, gameweek_id=gw.id,
                                    total_points=13, player_points=entries))
    test_session.commit()

    proj = services.projected_points_by_manager(test_session, lg, GW)[mgr.id]
    stored = test_session.query(GameweekPoints).filter_by(manager_id=mgr.id).one()
    assert proj["points"] == stored.total_points


# ---- matchup analysis ---------------------------------------------------------
# Deterministic, and the arithmetic is pinned here so that when the pundit layer
# arrives (backlog Item 19) a number in the sentence is never something it inferred.

def _match(hs, as_, *, home_left=(), away_left=(), home_cover=None, away_cover=None):
    """A get_scoreboard match row, reduced to what the analysis reads."""
    def side(names):
        players = [{"fpl_id": 1000 + i, "name": n} for i, n in enumerate(names)]
        return {"remaining": len(players), "remaining_players": players}

    return {
        "home": "Scott", "away": "John", "home_score": hs, "away_score": as_,
        "home_remaining": side(home_left), "away_remaining": side(away_left),
        "home_cover": home_cover or {}, "away_cover": away_cover or {},
    }


def test_analysis_settled_names_the_winner_and_the_score():
    assert services.matchup_analysis(_match(63, 43)) == "Scott wins 63–43."


def test_analysis_a_level_finish_is_a_draw():
    """Draws are real in this league, so this is a result, not a pending state."""
    assert services.matchup_analysis(_match(41, 41)) == "41–41 — a draw."


def test_analysis_a_trailing_manager_with_nobody_left_has_already_lost():
    """Points only go up, so the leader cannot be caught. Saying "needs 11 from
    nobody" would be technically true and useless."""
    out = services.matchup_analysis(_match(43, 63, home_left=()))
    assert out == "John wins 63–43."


def test_analysis_when_the_leader_is_done_it_is_a_plain_target():
    out = services.matchup_analysis(_match(38, 42, home_left=("Sesko", "White")))
    assert out == "Scott needs 5 from Sesko and White to win, 4 to draw."


def test_analysis_when_both_have_players_left_it_is_a_comparison():
    """The shape the commissioner asked for."""
    out = services.matchup_analysis(
        _match(38, 48, home_left=("Sesko", "White"), away_left=("Bruno",))
    )
    assert out == ("Scott needs Sesko and White to outscore Bruno by 11 to win, "
                   "10 to draw.")


def test_analysis_level_with_players_left_is_not_a_result_yet():
    out = services.matchup_analysis(
        _match(40, 40, home_left=("Sesko",), away_left=("Bruno",))
    )
    assert out == "Level at 40 — Scott has Sesko left, John has Bruno left."


def test_analysis_level_with_one_side_out_of_players():
    out = services.matchup_analysis(_match(40, 40, home_left=("Sesko",)))
    assert out == "Level at 40 — Scott has Sesko left, John has nobody left."


def test_analysis_draw_arithmetic_is_one_less_than_the_win():
    """A one-point trail needs 2 to win and 1 to draw — the case where getting this
    off by one actually changes what a manager does."""
    out = services.matchup_analysis(_match(40, 41, home_left=("Sesko",)))
    assert "needs 2 from Sesko to win, 1 to draw" in out


def test_analysis_caps_a_long_list_of_names():
    out = services.matchup_analysis(
        _match(10, 20, home_left=("A", "B", "C", "D", "E"))
    )
    assert "A, B, C +2 more" in out


def test_analysis_cover_clause_quotes_banked_points():
    cover = {1000: {"name": "Hall", "points": 11, "played": True}}
    out = services.matchup_analysis(
        _match(38, 48, home_left=("Sesko",), away_left=("Bruno",), home_cover=cover)
    )
    assert out.endswith("— Hall covers Sesko from the bench (11 pts)")


def test_analysis_cover_clause_quotes_the_fixture_when_he_has_not_played():
    """More useful than "0 pts": it says when the uncertainty resolves."""
    cover = {1000: {"name": "Hall", "points": 0, "played": False, "opponent": "SPU",
                    "kickoff_time": datetime.datetime(2025, 5, 5, 19, 0,
                                                      tzinfo=datetime.timezone.utc)}}
    out = services.matchup_analysis(
        _match(38, 48, home_left=("Sesko",), away_left=("Bruno",), home_cover=cover)
    )
    assert "Hall covers Sesko from the bench (vs SPU, Mon 19:00)" in out


def test_analysis_singular_point_is_not_pluralised():
    cover = {1000: {"name": "Hall", "points": 1, "played": True}}
    out = services.matchup_analysis(
        _match(38, 48, home_left=("Sesko",), away_left=("Bruno",), home_cover=cover)
    )
    assert "(1 pt)" in out and "1 pts" not in out


def test_analysis_says_nothing_without_scores():
    """A gameweek with no points synced yet must not produce a sentence about None."""
    assert services.matchup_analysis(_match(None, None)) == ""


def test_the_scoreboard_page_renders_the_analysis_and_marks_a_sub(test_session):
    """Both new pieces of UI. A template error here loses the whole feature silently,
    since the page would still render the scores."""
    from auth import hash_password
    from fastapi.testclient import TestClient
    from main import app

    lg, gw = _league(test_session)
    home = _manager(test_session, lg, "Scott", "1")
    away = _manager(test_session, lg, "John", "2")
    made = _squad_rows(test_session, lg, _SHAPE)
    for club in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"):
        _fixture(test_session, lg, hash(club) % 10000, club, "ZZZ", finished=True)
    _fixture(test_session, lg, 8888, "HHH", "ZZZ", finished=False, started=False,
             kickoff=datetime.datetime(2025, 5, 5, 19, 0, tzinfo=datetime.timezone.utc))
    # m1 blanks, bd blanked too, so the not-yet-kicked-off bench midfielder covers.
    _gwpoints(test_session, gw, home,
              _entries(_SHAPE, made, minutes={"m1": 0, "bd": 0, "bm": 0, "bf": 0}))
    _gwpoints(test_session, gw, away, _entries(_SHAPE, made, points={"m2": 9}))
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=home.id, away_manager_id=away.id))
    home.password_hash = hash_password("pw")
    test_session.commit()

    client = TestClient(app, follow_redirects=False)
    assert client.post("/login", data={"manager_id": "1", "password": "pw"}).status_code == 303
    r = client.get(f"/scoreboard?gw={GW}")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Scott needs" in body, "the analysis line rendered"
    assert ">sub<" in body, "the projected substitute is marked as one"


# ---- the homepage lead ---------------------------------------------------------
# The scoreboard leads the homepage only while a gameweek is actually being played.
# Both ends are excluded deliberately: before the first kickoff every score is 0–0, and
# after the last whistle the gameweek is a result rather than a race.

def _progress(session, lg, *, finished, in_progress, not_started):
    for i in range(finished):
        _fixture(session, lg, 100 + i, f"F{i}", "ZZZ", finished=True)
    for i in range(in_progress):
        _fixture(session, lg, 200 + i, f"P{i}", "ZZZ", finished=False, started=True)
    for i in range(not_started):
        _fixture(session, lg, 300 + i, f"N{i}", "ZZZ", finished=False, started=False)
    session.commit()


@pytest.mark.parametrize("finished,in_progress,not_started,live", [
    (0, 0, 10, False),   # before the first kickoff — every score is 0–0
    (0, 1, 9, True),     # first match under way
    (9, 0, 1, True),     # the real GW2 state: one fixture still to come
    (5, 2, 3, True),
    (10, 0, 0, False),   # all done — the standings are the story again
    (0, 0, 0, False),    # unsynced gameweek, NOT a finished one
])
def test_gameweek_is_live_only_while_it_is_being_played(
    test_session, finished, in_progress, not_started, live
):
    lg, _gw = _league(test_session)
    _progress(test_session, lg, finished=finished, in_progress=in_progress,
              not_started=not_started)
    assert services.gameweek_is_live(test_session, lg, GW) is live


def test_the_homepage_leads_with_the_scoreboard_while_live(test_session):
    from auth import hash_password
    from fastapi.testclient import TestClient
    from main import app

    lg, gw = _league(test_session)
    home = _manager(test_session, lg, "Scott", "1")
    away = _manager(test_session, lg, "John", "2")
    made = _squad_rows(test_session, lg, _SHAPE)
    _progress(test_session, lg, finished=1, in_progress=1, not_started=0)
    _gwpoints(test_session, gw, home, _entries(_SHAPE, made, points={"m2": 12}))
    _gwpoints(test_session, gw, away, _entries(_SHAPE, made, points={"m3": 5}))
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=home.id, away_manager_id=away.id))
    home.password_hash = hash_password("pw")
    test_session.commit()

    client = TestClient(app, follow_redirects=False)
    assert client.post("/login", data={"manager_id": "1", "password": "pw"}).status_code == 303
    body = client.get("/").text
    assert f"GW{GW} — in progress" in body
    # Above the standings, which is the whole point of the request.
    assert body.index("in progress") < body.index("Standings")


def test_the_homepage_omits_the_scoreboard_once_the_gameweek_is_done(test_session):
    from auth import hash_password
    from fastapi.testclient import TestClient
    from main import app

    lg, gw = _league(test_session)
    home = _manager(test_session, lg, "Scott", "1")
    _manager(test_session, lg, "John", "2")
    _progress(test_session, lg, finished=2, in_progress=0, not_started=0)
    home.password_hash = hash_password("pw")
    test_session.commit()

    client = TestClient(app, follow_redirects=False)
    assert client.post("/login", data={"manager_id": "1", "password": "pw"}).status_code == 303
    body = client.get("/").text
    assert "in progress" not in body
    assert "Standings" in body, "the rest of the page is unaffected"
