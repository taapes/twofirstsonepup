"""Setting the draft order is admin-only in the backend — the template must agree.

`POST /draft/{year}/order`, `/order-later` and `/order-revert` all already refuse a
non-admin with a clean 403 (`ui.py`, "Only the commissioner can set the draft order.").
But the "Set round-1 order" and "Set order for rounds 2+" forms in `draft.html` rendered
unconditionally — every logged-in manager saw them, right next to "Trade a pick" and
"Trade a player", which genuinely are open to any manager. Clicking Save as a non-admin
hit that 403, and htmx does not swap a non-2xx response, so nothing visibly happened —
confusing during a live draft, and easy to mistake for a bug.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from auth import hash_password
from models import Gameweek, League, Manager, Player, Standing

UPCOMING = 2026


def _seed(session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="draft")
    session.add(lg)
    session.flush()
    session.add(Gameweek(number=1, league_id=lg.id))
    session.flush()

    mgrs = {}
    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i, total=10 - i,
                             points_for=100))
        mgrs[name] = m

    session.add(Player(name="Haaland", code=7, fpl_id=1, position="MID",
                       current_team="ARS"))
    session.commit()
    return lg, mgrs


@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app, follow_redirects=False)


def _login_manager(client, session, manager, password="pw"):
    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text


def _login_admin(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "order-test-pw")
    r = client.post("/admin/login", data={"password": "order-test-pw"})
    assert r.status_code == 303, r.text


def test_a_manager_does_not_see_the_order_controls(client, test_session):
    lg, mgrs = _seed(test_session)
    _login_manager(client, test_session, mgrs["A"])

    body = client.get(f"/draft/{UPCOMING}").content.decode()
    assert "Set round-1 order" not in body
    assert "Set order for rounds 2+" not in body
    assert "Revert to standings order" not in body
    # the controls a manager genuinely may use must still be there
    assert "Trade a pick" in body
    assert "Trade a player" in body


def test_the_admin_still_sees_the_order_controls(client, test_session, monkeypatch):
    lg, mgrs = _seed(test_session)
    _login_admin(client, monkeypatch)

    body = client.get(f"/draft/{UPCOMING}").content.decode()
    assert "Set round-1 order" in body
    assert "Set order for rounds 2+" in body
    assert "Revert to standings order" in body


def test_a_manager_posting_the_order_route_directly_is_still_refused(
    client, test_session
):
    """The template gate is a UX fix, not the security boundary — the route's own
    is_admin check is, and it must still hold even if someone bypasses the UI."""
    lg, mgrs = _seed(test_session)
    _login_manager(client, test_session, mgrs["A"])

    r = client.post(f"/draft/{UPCOMING}/order", data={"order": "1,2"})
    assert r.status_code == 403
    assert b"Only the commissioner" in r.content
