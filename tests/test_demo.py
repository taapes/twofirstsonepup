"""Demo-mode passwordless login (APP_ENV=demo).

Verifies /demo-login works only in demo mode and 404s otherwise, so production can
never passwordless-login.

CONVERTED 2026-08-31 off the production database. This file was on conftest's
COMMITTING_DB_MODULES list and skipped by default, on the stated grounds that it
"commits internally" — it never wrote anything at all. What it actually did was
`from db import SessionLocal` at module scope (a binding `test_session`'s monkeypatch
cannot reach, since it rebinds the attribute on the `db` module) and then read whatever
rows happened to exist via `.first()`. Seeding its own two rows is the whole fix.
"""

import pytest

import auth
from models import Gameweek, League, Manager


@pytest.fixture
def client(test_session):
    """Built inside the fixture, after test_session has patched db.SessionLocal — the
    same recipe as every other route test here."""
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


@pytest.fixture
def manager_fpl(test_session):
    """One current league with one manager. `/` needs the league to render at all."""
    lg = League(fpl_league_id="1", name="Demo", season_year=2026, is_current=True,
                sync_locked=False, phase="in_season")
    test_session.add(lg)
    test_session.flush()
    test_session.add(Gameweek(number=1, league_id=lg.id))
    test_session.add(Manager(league_id=lg.id, fpl_manager_id="77", name="T",
                             display_name="Ann"))
    test_session.commit()
    return "77"


def test_is_demo_reads_app_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "demo")
    assert auth.is_demo()
    monkeypatch.setenv("APP_ENV", "prod")
    assert not auth.is_demo()


def test_demo_login_404_when_not_demo(client, manager_fpl, monkeypatch):
    """The guard that keeps passwordless login out of production."""
    monkeypatch.setenv("APP_ENV", "prod")
    r = client.post("/demo-login", data={"manager_id": manager_fpl})
    assert r.status_code == 404


def test_demo_login_logs_in_when_demo(client, manager_fpl, monkeypatch):
    monkeypatch.setenv("APP_ENV", "demo")
    r = client.post("/demo-login", data={"manager_id": manager_fpl})
    assert r.status_code == 303 and r.headers["location"] == "/"
    # The session now carries an identity, so the site-wide gate lets a page through
    # instead of bouncing it to /who.
    assert client.get("/").status_code == 200
    client.get("/logout")


def test_demo_login_unknown_manager_404(client, manager_fpl, monkeypatch):
    monkeypatch.setenv("APP_ENV", "demo")
    r = client.post("/demo-login", data={"manager_id": "does-not-exist"})
    assert r.status_code == 404
