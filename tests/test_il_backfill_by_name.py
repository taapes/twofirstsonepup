"""IL/international backfill by player NAME, not FPL id.

The backlog item this closes: the commissioner's historical backfill form
(`/admin/keepers`) and the self-service "he's already been dropped" pickers on
`/my-team` both used to require a bare FPL element id — unusable by hand, and
outright impossible for a player who has since left the Premier League entirely
(`players.fpl_id` is nullable and NULL for exactly that case, which
`_resolve_player`/`place_on_il`'s fpl_id-based path cannot look up at all).

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import Gameweek, InjuryList, InternationalList, League, Manager, Player, Roster
from rules import RuleViolation

LAST_GW = 38


def _seed(session, n_gws=LAST_GW):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                phase="offseason")
    session.add(lg)
    session.flush()
    gws = {}
    for n in range(1, n_gws + 1):
        g = Gameweek(number=n, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[n] = g
    a = Manager(league_id=lg.id, fpl_manager_id="1", name="A", display_name="Ann")
    session.add(a)
    session.commit()
    return lg, a, gws


def _player(session, name, fpl_id, pos="MID", team="MUN"):
    p = Player(name=name, code=(fpl_id or 999) * 7, fpl_id=fpl_id, position=pos,
               current_team=team, price=90, status="a")
    session.add(p)
    session.commit()
    return p


def _hold(session, mgr, player, gws, numbers):
    for n in numbers:
        session.add(Roster(manager_id=mgr.id, gameweek_id=gws[n].id, player_id=player.id))
    session.commit()


# ---- the core service functions -------------------------------------------

def test_place_on_il_by_player_succeeds_for_a_departed_player(test_session):
    """The actual regression pin: a player with fpl_id=None (left the PL entirely)
    can be placed on the IL when resolved by object rather than by fpl_id — the
    fpl_id-based place_on_il cannot represent this player at all."""
    lg, a, gws = _seed(test_session)
    departed = _player(test_session, "Departed", None)
    rep = _player(test_session, "Rep", 2)
    _hold(test_session, a, rep, gws, range(1, LAST_GW + 1))

    services.place_on_il_by_player(
        test_session, lg, fpl_manager_id="1", require_roster=False,
        injured=departed, replacement=rep, start_gw=30,
    )
    entry = test_session.query(InjuryList).one()
    assert entry.player_id == departed.id
    assert entry.replacement_id == rep.id


def test_place_on_intl_by_player_succeeds_for_a_departed_player(test_session):
    """place_on_intl has no require_roster bypass, so the departed player still
    needs a historical roster hold — _validate_absence_eligibility's HISTORICAL
    case (he's not on the CURRENT roster, but presence shows he was held earlier
    this season)."""
    lg, a, gws = _seed(test_session)
    departed = _player(test_session, "Departed", None)
    rep = _player(test_session, "Rep", 2)
    _hold(test_session, a, departed, gws, range(1, 20))
    _hold(test_session, a, rep, gws, range(1, LAST_GW + 1))

    services.place_on_intl_by_player(
        test_session, lg, fpl_manager_id="1",
        away=departed, replacement=rep, start_gw=25,
    )
    entry = test_session.query(InternationalList).one()
    assert entry.player_id == departed.id


def test_resolve_player_by_fpl_id_is_the_public_twin_of_the_private_helper(test_session):
    lg, _a, _gws = _seed(test_session)
    p = _player(test_session, "Star", 7)
    assert services.resolve_player_by_fpl_id(test_session, 7).id == p.id


# ---- dropped_players_for_manager now includes a departed player -----------

def test_dropped_players_for_manager_includes_a_departed_player(test_session):
    """Found via the backlog: this picker used to silently drop any candidate
    with fpl_id=None, hiding exactly the case a manager most needs it for."""
    lg, a, gws = _seed(test_session)
    departed = _player(test_session, "Departed", None)
    _hold(test_session, a, departed, gws, range(1, 20))

    out = services.dropped_players_for_manager(test_session, lg, a)
    assert any(r["name"] == "Departed" and r["fpl_id"] is None for r in out)


# ---- admin historical backfill route, end to end ---------------------------

@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app, follow_redirects=False)


def test_admin_backfill_route_succeeds_for_a_departed_player_by_name(
        test_session, client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "il-name-backfill-test-pw")
    lg, a, gws = _seed(test_session)
    departed = _player(test_session, "Departed", None)
    rep = _player(test_session, "Rep", 2)
    _hold(test_session, a, rep, gws, [38])

    assert client.post("/admin/login",
                       data={"password": "il-name-backfill-test-pw"}).status_code == 303

    r = client.post("/admin/keepers/il-backfill", data={
        "fpl_manager_id": "1", "injured_name": "Departed · MUN",
        "replacement_name": "Rep · MUN", "start_gw": "37",
    })
    assert r.status_code == 303, r.text
    entry = test_session.query(InjuryList).one()
    assert entry.player_id == departed.id


def test_admin_backfill_route_refuses_an_unresolvable_name(test_session, client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "il-name-backfill-test-pw")
    lg, a, gws = _seed(test_session)
    _player(test_session, "Rep", 2)

    assert client.post("/admin/login",
                       data={"password": "il-name-backfill-test-pw"}).status_code == 303

    r = client.post("/admin/keepers/il-backfill", data={
        "fpl_manager_id": "1", "injured_name": "Nobody By This Name",
        "replacement_name": "Rep · MUN", "start_gw": "37",
    })
    assert r.status_code == 400
    assert test_session.query(InjuryList).count() == 0


# ---- self-service "already dropped" picker, end to end ---------------------

def _login_manager(client, session, manager, password="pw"):
    from auth import hash_password

    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text


def test_self_service_dropped_form_succeeds_for_a_departed_player_by_name(
        test_session, client):
    lg, a, gws = _seed(test_session)
    departed = _player(test_session, "Departed", None)
    rep = _player(test_session, "Rep", 2)
    _hold(test_session, a, departed, gws, range(1, 20))
    _hold(test_session, a, rep, gws, range(1, LAST_GW + 1))
    lg.phase = "in_season"
    test_session.commit()
    _login_manager(client, test_session, a)

    r = client.post("/il/place", data={
        "fpl_manager_id": "1", "injured_name": "Departed · MUN",
        "replacement_fpl_id": "2", "start_gw": "1",
    })
    assert r.status_code == 303, r.text
    entry = test_session.query(InjuryList).one()
    assert entry.player_id == departed.id
