"""A player still on IL at the final gameweek is invisible as a keeper candidate
unless BOTH pieces exist: an InjuryList row explaining the gap, AND
`_derive_keeper_status`'s `final_candidates` actually looking at IL coverage in the
first place — the IL/international `il` dict used to only ever explain a gap for a
player who was ALREADY a candidate; it never added one.

Regression for a real case: a player rostered continuously for most of a season,
swapped for a same-position replacement near the end, never returned. The FPL-synced
roster at the final GW shows the replacement, not him — so without this fix he simply
doesn't appear anywhere in the derivation, and the existing `set_keeper_override`
correction tool can't reach him either (it requires the player to already be a
candidate).

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import Gameweek, InjuryList, League, Manager, Player, PlayerSeason, Roster

LAST_GW = 38
ALL_GWS = range(1, LAST_GW + 1)


def _seed(session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=True, phase="offseason")
    session.add(lg)
    session.flush()
    gws = {}
    for n in ALL_GWS:
        g = Gameweek(number=n, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[n] = g
    m = Manager(league_id=lg.id, fpl_manager_id="1", name="Scott", display_name="Scott")
    session.add(m)
    session.commit()
    return lg, m, gws


def _player(session, lg, name, fpl_id, pos="FWD"):
    p = Player(name=name, code=fpl_id * 7, fpl_id=fpl_id, position=pos,
               current_team="MUN", price=90, status="a")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id, name=name,
                             position=pos, current_team="MUN"))
    session.commit()
    return p


def _hold(session, mgr, player, gws, numbers):
    for n in numbers:
        session.add(Roster(manager_id=mgr.id, gameweek_id=gws[n].id, player_id=player.id))
    session.commit()


def test_an_open_ended_il_entry_makes_the_player_a_keeper_candidate(test_session):
    """The actual case: rostered GW1-36, gone from GW37 on, an active IL entry
    (never returned) covering GW37 through the last GW. Must appear as a normal
    draft keeper — no override needed, since he genuinely started the season on
    this manager's roster and the gap is now explained."""
    lg, mgr, gws = _seed(test_session)
    p = _player(test_session, lg, "Šeško", 439)
    _hold(test_session, mgr, p, gws, range(1, 37))   # GW1..36
    test_session.add(InjuryList(
        player_id=p.id, manager_id=mgr.id, start_gw=37, end_gw=None, status="active",
    ))
    test_session.commit()

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    row = status.get(mgr.id, {}).get(p.id)
    assert row is not None, "an IL'd, never-returned player must still be a candidate"
    assert row["acquisition"] == "draft"
    assert row["eligible"] is True


def test_without_the_il_row_the_same_gap_is_invisible(test_session):
    """Pins the documented gap this fix closes: identical roster shape, but no
    InjuryList row at all — the player must not silently appear (that would mean
    the fix is inventing candidates, not explaining a real one)."""
    lg, mgr, gws = _seed(test_session)
    p = _player(test_session, lg, "Šeško", 439)
    _hold(test_session, mgr, p, gws, range(1, 37))

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    assert p.id not in status.get(mgr.id, {})


# ---- the admin backfill route ----------------------------------------------
@pytest.fixture
def client(test_session):
    """A TestClient sharing the test database (conftest patches db.SessionLocal,
    which get_db resolves at call time) — never the configured one."""
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def test_il_backfill_route_requires_admin(client, test_session):
    lg, mgr, gws = _seed(test_session)
    _player(test_session, lg, "Šeško", 439)

    r = client.post("/admin/keepers/il-backfill", data={
        "fpl_manager_id": "1", "injured_fpl_id": "439",
        "replacement_fpl_id": "27", "start_gw": "37",
    })
    # the login GATE (no session at all) catches this before the route's own
    # is_admin check even runs — /who is the login surface for a bare request
    assert r.status_code in (303, 307)
    assert r.headers["location"] in ("/who", "/admin/login")
    assert test_session.query(InjuryList).count() == 0


def test_il_backfill_route_creates_the_row_and_grants_candidacy(
    client, test_session, monkeypatch
):
    monkeypatch.setenv("ADMIN_PASSWORD", "il-backfill-test-pw")
    lg, mgr, gws = _seed(test_session)
    p = _player(test_session, lg, "Šeško", 439)
    replacement = _player(test_session, lg, "G.Jesus", 27)
    _hold(test_session, mgr, p, gws, range(1, 37))
    _hold(test_session, mgr, replacement, gws, [38])

    assert client.post("/admin/login", data={"password": "il-backfill-test-pw"}
                       ).status_code == 303

    r = client.post("/admin/keepers/il-backfill", data={
        "fpl_manager_id": "1", "injured_fpl_id": "439",
        "replacement_fpl_id": "27", "start_gw": "37",
    })
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/admin/keepers"

    entry = test_session.query(InjuryList).one()
    assert entry.start_gw == 37 and entry.end_gw is None and entry.status == "active"

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    assert status[mgr.id][p.id]["acquisition"] == "draft"
    # additive, not a replacement — the actual replacement is still his own candidate
    assert replacement.id in status[mgr.id]


def test_il_coverage_with_no_roster_presence_does_not_crash(test_session):
    """A candidate reached purely through IL coverage may have zero recorded roster
    presence for that (manager, player) pair. Must not KeyError/crash min() on an
    empty set — falls through to 'waiver' since they never started with this
    manager and were never traded in."""
    lg, mgr, gws = _seed(test_session)
    p = _player(test_session, lg, "Ghost", 900)
    test_session.add(InjuryList(
        player_id=p.id, manager_id=mgr.id, start_gw=10, end_gw=None, status="active",
    ))
    test_session.commit()

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    row = status[mgr.id][p.id]
    assert row["acquisition"] == "waiver"


def test_a_returned_il_entry_does_not_grant_candidacy_on_its_own(test_session):
    """An IL entry that already RETURNED before the final GW shouldn't make someone
    a candidate through IL alone — only an entry still covering the final GW
    should. (If they're back on the active roster, normal presence covers them.)"""
    lg, mgr, gws = _seed(test_session)
    p = _player(test_session, lg, "Returned", 901)
    test_session.add(InjuryList(
        player_id=p.id, manager_id=mgr.id, start_gw=10, end_gw=15, status="returned",
    ))
    test_session.commit()

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    assert p.id not in status.get(mgr.id, {})
