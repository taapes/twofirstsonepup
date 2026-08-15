"""The draft pick route surfaces a refused pick instead of silently doing nothing.

`ui.draft_pick` used to `except RuleViolation: pass`, and htmx never swaps a non-2xx
response — so a kept player, a re-picked slot, or picking out of turn all looked
EXACTLY like a successful pick to the manager pressing the button: nothing on the
page changed. The fix renders the board partial (a 200, so htmx swaps it normally)
with the RuleViolation's message inside it.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from auth import hash_password
from models import DraftPick, Gameweek, KeeperSelection, League, Manager, Player, Standing

UPCOMING = 2026


def _seed(session):
    """Two managers, three players. Reverse standings (no lottery rows) puts B on
    the clock first — the same seed shape used in tests/test_keeper_privacy.py."""
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="draft")
    session.add(lg)
    session.flush()
    gw = Gameweek(number=1, league_id=lg.id)
    session.add(gw)
    session.flush()

    mgrs = {}
    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i, total=10 - i,
                             points_for=100))
        mgrs[name] = m

    players = {}
    for i, name in enumerate(["Haaland", "Palmer", "Saka"], start=1):
        p = Player(name=name, code=i * 7, fpl_id=i, position="MID", current_team="ARS")
        session.add(p)
        session.flush()
        players[name] = p
    session.commit()

    on_clock = services.next_open_pick(services.get_draft_board(session, lg, UPCOMING))
    assert on_clock["owner"] == "B", "seed assumption — B must be on the clock at pick 1"
    return lg, mgrs, players


@pytest.fixture
def client(test_session):
    """A TestClient sharing the test database (conftest patches db.SessionLocal,
    which get_db resolves at call time) — never the configured one."""
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _login(client, session, manager, password="pw"):
    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text
    return client


def test_a_refused_pick_shows_its_reason_and_writes_nothing(client, test_session):
    lg, mgrs, players = _seed(test_session)
    test_session.add(KeeperSelection(
        league_id=lg.id, manager_id=mgrs["A"].id, player_id=players["Palmer"].id,
        season_year=UPCOMING,
    ))
    test_session.commit()
    _login(client, test_session, mgrs["B"])   # B is on the clock

    r = client.post(f"/draft/{UPCOMING}/pick", data={"player_fpl_id": 2})  # Palmer
    assert r.status_code == 200
    assert "kept by A" in r.text
    assert test_session.query(DraftPick).count() == 0


def test_re_picking_a_filled_slot_shows_already_made_and_leaves_it_unchanged(
    client, test_session
):
    lg, mgrs, players = _seed(test_session)
    _login(client, test_session, mgrs["B"])

    first = client.post(f"/draft/{UPCOMING}/pick", data={"player_fpl_id": 1})  # Haaland
    assert first.status_code == 200
    assert test_session.query(DraftPick).count() == 1

    again = client.post(
        f"/draft/{UPCOMING}/pick", data={"player_fpl_id": 3, "pick_number": 1}
    )  # Saka, same slot, no overwrite (not admin)
    assert again.status_code == 200
    assert "already been made" in again.text
    row = test_session.query(DraftPick).one()
    assert row.player_id == players["Haaland"].id, "the original pick must survive"


def test_a_successful_pick_shows_no_error_banner(client, test_session):
    lg, mgrs, players = _seed(test_session)
    _login(client, test_session, mgrs["B"])

    r = client.post(f"/draft/{UPCOMING}/pick", data={"player_fpl_id": 1})
    assert r.status_code == 200
    assert "⚠" not in r.text
    assert test_session.query(DraftPick).one().player_id == players["Haaland"].id


def test_the_wrong_manager_still_gets_403(client, test_session):
    """Unchanged server-side: the visible-error fix only covers RuleViolation, not
    the authorization check, which must keep failing closed."""
    lg, mgrs, players = _seed(test_session)
    _login(client, test_session, mgrs["A"])   # A is NOT on the clock (B is)

    r = client.post(f"/draft/{UPCOMING}/pick", data={"player_fpl_id": 1})
    assert r.status_code == 403
    assert test_session.query(DraftPick).count() == 0
