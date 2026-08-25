"""The My Team "Upcoming" page shows how each player is actually playing.

The page pairs your next H2H opponents with each player's real-life PL fixture. It used
to show WHO a player faces and nothing about HOW he is playing, so a fixture couldn't be
judged without opening another page. Form, total points and availability were already in
the data — `get_upcoming_matchups` builds every dict through `_player_stat_dict`, the
same helper `get_my_team` uses — the template just never read those keys.

So the service behaviour here is not new, and these tests exist to stop a REGRESSION
rather than to prove new logic: the template now depends on a dict contract that nothing
previously pinned. Swap `_player_stat_dict` for something leaner and the page would
quietly render dashes for every player, with no test failing.
"""

import datetime

import pytest

import services
from models import (
    Fixture,
    Gameweek,
    League,
    Manager,
    Match,
    Player,
    PlayerSeason,
    Roster,
)

CUR_GW = 5


@pytest.fixture
def client(test_session):
    """A TestClient sharing the test database (conftest patches db.SessionLocal, which
    get_db resolves at call time) — never the configured one."""
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _login(client, session, manager, password="pw"):
    """The token bypass is not enough here: it clears the login gate but leaves the
    session empty, so `_resolve_my_fpl` finds no manager (and honours `?fpl=` only for
    admin) and the page renders its empty state. These tests need a real identity."""
    from auth import hash_password

    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text
    return client


def _seed(session):
    """Two managers with a squad each, an H2H fixture between them next gameweek, and a
    real PL fixture for their clubs."""
    lg = League(fpl_league_id="1", name="S", season_year=2026, is_current=True,
                sync_locked=False, phase="in_season")
    session.add(lg)
    session.flush()

    gws = {}
    for n in range(1, 12):
        g = Gameweek(number=n, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[n] = g

    mgrs = {}
    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        mgrs[name] = m

    # One player each, with contrasting stats: a zero-points player is the case the
    # `is not none` idiom exists for.
    specs = [
        ("A", "Striker", "ARS", "3.5", 42, "a", None),
        ("B", "Blanker", "CHE", "0.0", 0, "i", "Knee injury"),
    ]
    # Deterministic ids: Python's hash() is salted per process, so deriving them from
    # the name would collide between these two players roughly one run in ninety.
    for i, (owner, name, team, form, pts, status, news) in enumerate(specs, start=1):
        p = Player(name=name, code=1000 + i, fpl_id=i,
                   position="MID", current_team=team)
        session.add(p)
        session.flush()
        session.add_all([
            PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=p.fpl_id, name=name,
                         position="MID", current_team=team, form=form,
                         total_points=pts, status=status, news=news),
            # _squad_players reads the LATEST gameweek's roster rows.
            Roster(manager_id=mgrs[owner].id, gameweek_id=gws[CUR_GW].id, player_id=p.id),
        ])

    session.add(Match(league_id=lg.id, gameweek_id=gws[CUR_GW + 1].id,
                      home_manager_id=mgrs["A"].id, away_manager_id=mgrs["B"].id))
    session.add(Fixture(league_id=lg.id, fpl_fixture_id=1, event=CUR_GW + 1,
                        kickoff_time=datetime.datetime(2026, 10, 1, 14, 0,
                                                       tzinfo=datetime.timezone.utc),
                        home_team="ARS", away_team="CHE",
                        home_difficulty=2, away_difficulty=4, finished=False))
    session.commit()
    return lg, mgrs


@pytest.fixture
def at_gw(monkeypatch):
    """Pin the current gameweek. It is otherwise derived from stored deadline dates,
    which is covered elsewhere and is not what these tests are about."""
    monkeypatch.setattr(services, "current_gameweek", lambda db, league: CUR_GW)
    monkeypatch.setattr(services, "latest_gameweek",
                        lambda db, league: db.query(Gameweek).filter_by(
                            league_id=league.id, number=CUR_GW).one())


# ---- the dict contract the template now depends on ------------------------
def test_every_player_in_both_squads_carries_form_and_total_points(test_session, at_gw):
    """The regression this file exists for. Both squads, every gameweek returned."""
    lg, mgrs = _seed(test_session)

    mus = services.get_upcoming_matchups(test_session, lg, "1")
    assert mus, "no matchups produced — the seed is wrong, not the assertion"

    seen = 0
    for mu in mus:
        for side in ("my_squad", "opp_squad"):
            for p in mu.get(side) or []:
                assert "form" in p, f"{side} lost `form`"
                assert "total_points" in p, f"{side} lost `total_points`"
                assert "status" in p, f"{side} lost `status` (the availability dot)"
                seen += 1
    assert seen, "matchups contained no players at all"


def test_the_opponents_squad_is_populated_too(test_session, at_gw):
    """my_squad and opp_squad go through the same closure, but only one of them is
    yours — a change that resolved stats off the logged-in manager would still pass a
    my_squad-only assertion."""
    lg, mgrs = _seed(test_session)

    mu = next(m for m in services.get_upcoming_matchups(test_session, lg, "1")
              if m.get("opponent"))
    assert [p["name"] for p in mu["my_squad"]] == ["Striker"]
    assert [p["name"] for p in mu["opp_squad"]] == ["Blanker"]
    assert mu["opp_squad"][0]["form"] == "0.0"
    assert mu["opp_squad"][0]["total_points"] == 0


# ---- rendering ------------------------------------------------------------
def test_the_page_renders_the_new_columns(client, test_session, at_gw):
    _lg, mgrs = _seed(test_session)
    _login(client, test_session, mgrs["A"])

    body = client.get("/my-team/upcoming").text
    assert ">Form<" in body
    assert ">Pts<" in body
    assert "3.5" in body, "form value missing"
    assert "42" in body, "total points missing"


def test_zero_points_renders_as_zero_not_a_dash(client, test_session, at_gw):
    """`total_points` is an int, so `or` would swallow a legitimate 0 — and at the start
    of a season most of the league sits on 0. Pins the `is not none` idiom against a
    future "simplification"."""
    _lg, mgrs = _seed(test_session)
    _login(client, test_session, mgrs["A"])

    body = client.get("/my-team/upcoming").text
    blanker = body.split("Blanker")[1][:400]
    assert ">0<" in blanker, f"zero points rendered as a dash: {blanker!r}"


def test_an_unavailable_player_gets_the_warning_dot_with_his_news(
    client, test_session, at_gw
):
    _lg, mgrs = _seed(test_session)
    _login(client, test_session, mgrs["A"])

    body = client.get("/my-team/upcoming").text
    assert 'title="Knee injury"' in body
    assert "dot warn" in body
    assert "dot ok" in body, "the available player should still get a dot"
