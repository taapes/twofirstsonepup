"""Demo-mode passwordless login (APP_ENV=demo). Run: pytest

Verifies /demo-login works only in demo mode and 404s otherwise (so prod can never
passwordless-login). Uses a real manager from the DB.
"""

import os

from fastapi.testclient import TestClient

import auth
from db import SessionLocal
from main import app
from models import League, Manager

client = TestClient(app, follow_redirects=False)


def _a_manager_fpl():
    db = SessionLocal()
    try:
        lg = db.query(League).filter_by(is_current=True).first() or db.query(League).first()
        m = db.query(Manager).filter_by(league_id=lg.id).first()
        return m.fpl_manager_id
    finally:
        db.close()


def test_is_demo_reads_app_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "demo")
    assert auth.is_demo()
    monkeypatch.setenv("APP_ENV", "prod")
    assert not auth.is_demo()


def test_demo_login_404_when_not_demo(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    r = client.post("/demo-login", data={"manager_id": _a_manager_fpl()})
    assert r.status_code == 404


def test_demo_login_logs_in_when_demo(monkeypatch):
    monkeypatch.setenv("APP_ENV", "demo")
    fpl = _a_manager_fpl()
    r = client.post("/demo-login", data={"manager_id": fpl})
    assert r.status_code == 303 and r.headers["location"] == "/"
    # session now carries the identity → the gate lets a page through (not redirected to /who)
    home = client.get("/")
    assert home.status_code == 200
    client.get("/logout")


def test_demo_login_unknown_manager_404(monkeypatch):
    monkeypatch.setenv("APP_ENV", "demo")
    r = client.post("/demo-login", data={"manager_id": "does-not-exist"})
    assert r.status_code == 404
