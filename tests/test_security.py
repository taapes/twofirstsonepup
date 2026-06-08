"""Security-hardening tests (Phase A). Run: pytest"""

import os

import pytest
from fastapi.testclient import TestClient

import auth
import ui
from main import app
from rules import RuleViolation

client = TestClient(app, follow_redirects=False)


# ---- _safe_int bounds ----
def test_safe_int_accepts_in_range():
    assert ui._safe_int("38", 1, 38, field="gw") == 38
    assert ui._safe_int(" 5 ", 1, 38) == 5


def test_safe_int_rejects_out_of_range():
    with pytest.raises(RuleViolation):
        ui._safe_int("39", 1, 38)
    with pytest.raises(RuleViolation):
        ui._safe_int("0", 1, 38)


def test_safe_int_rejects_non_numeric():
    with pytest.raises(RuleViolation):
        ui._safe_int("abc", 1, 38)
    with pytest.raises(RuleViolation):
        ui._safe_int("", 1, 38)


# ---- error responses are plain text (no HTML injection) ----
def test_err_is_plain_text():
    resp = ui._err("oops")
    assert resp.media_type == "text/plain"
    assert resp.status_code == 400
    assert b"oops" in resp.body


# ---- timing-safe admin password ----
def test_check_admin_password(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    assert auth.check_admin_password("correct-horse")
    assert not auth.check_admin_password("wrong")
    assert not auth.check_admin_password("")


def test_admin_password_empty_expected(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SYNC_AUTH_TOKEN", raising=False)
    assert not auth.check_admin_password("anything")


# ---- security headers on every response ----
def test_security_headers_present():
    r = client.get("/health")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "same-origin"
    assert "Content-Security-Policy" in r.headers


def test_security_headers_on_gate_redirect():
    # the gate redirect (303 to /who) should still carry hardening headers
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["X-Content-Type-Options"] == "nosniff"
