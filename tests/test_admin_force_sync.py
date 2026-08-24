"""POST /admin/sync/force: the session-authenticated twin of the token-gated
POST /admin/sync?force=1 cron route. A logged-in commissioner's browser session
can never satisfy require_admin (X-Auth-Token only), so there was previously no
way to trigger a full sync from the admin panel itself. Calls the exact same
sync.run_sync orchestration so the two paths can't drift apart.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import os

import pytest

from models import League


def _league(session):
    lg = League(fpl_league_id="1", name="S", season_year=2026, is_current=True,
                sync_locked=False, phase="offseason")
    session.add(lg)
    session.commit()
    return lg


@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _admin(client):
    r = client.post("/admin/login", data={"password": os.getenv("ADMIN_PASSWORD", "sports")})
    assert r.status_code in (200, 303), r.text
    return client


def test_an_anonymous_request_is_gated_before_reaching_the_route(client, test_session):
    """The site-wide GateMiddleware is the OUTER of the two gates — an anonymous
    request never reaches is_admin at all."""
    _league(test_session)
    r = client.post("/admin/sync/force")
    assert r.status_code == 303 and r.headers["location"] == "/who"


def test_a_logged_in_non_admin_is_redirected_to_admin_login(client, test_session, monkeypatch):
    """A real manager session passes the site gate but isn't admin — a distinct
    code path from the anonymous case above."""
    import auth

    lg = _league(test_session)
    from models import Manager
    m = Manager(league_id=lg.id, fpl_manager_id="1", name="Ann", display_name="Ann",
                password_hash=auth.hash_password("pw"))
    test_session.add(m)
    test_session.commit()

    called = []
    import sync
    monkeypatch.setattr(sync, "run_sync", lambda **kw: called.append(kw) or {"ok": True})

    client.post("/login", data={"manager_id": "1", "password": "pw"})
    r = client.post("/admin/sync/force")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login?next=/admin/health"
    assert called == []  # never reached the sync call


def test_admin_triggers_run_sync_and_redirects_to_health(client, test_session, monkeypatch):
    _league(test_session)
    called = []
    import sync
    monkeypatch.setattr(sync, "run_sync", lambda **kw: called.append(kw) or {"ok": True, "plan": "full"})

    _admin(client)
    r = client.post("/admin/sync/force")
    assert r.status_code == 303 and r.headers["location"] == "/admin/health"
    assert called == [{"force": True}]


def test_a_league_identity_error_surfaces_as_409_not_a_crash(client, test_session, monkeypatch):
    """The feed no longer looking like our league must fail loudly, not silently
    500 or redirect as if nothing happened."""
    _league(test_session)
    import sync

    def _raise(**kw):
        raise sync.LeagueIdentityError("looks like a different league")

    monkeypatch.setattr(sync, "run_sync", _raise)

    _admin(client)
    r = client.post("/admin/sync/force")
    assert r.status_code == 409
    assert "different league" in r.text
