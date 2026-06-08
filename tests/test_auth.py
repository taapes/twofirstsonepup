"""Tests for per-manager auth helpers (pure; no DB). Run: pytest"""

from auth import (
    can_act_as,
    hash_password,
    is_logged_in,
    is_owner,
    owner_entry_id,
    verify_password,
)


class FakeRequest:
    """Minimal stand-in exposing the signed-session dict the helpers read."""

    def __init__(self, session: dict):
        self.session = session


# ---- password hashing ----
def test_hash_verify_round_trip():
    stored = hash_password("hunter2")
    assert verify_password("hunter2", stored)
    assert not verify_password("wrong", stored)


def test_hash_is_salted_unique():
    assert hash_password("same") != hash_password("same")  # random salt


def test_verify_none_and_garbage_are_safe():
    assert verify_password("anything", None) is False
    assert verify_password("anything", "") is False
    assert verify_password("anything", "not-a-valid-format") is False
    assert verify_password("anything", "pbkdf2_sha256$bad$bad") is False


# ---- can_act_as / is_logged_in ----
def test_admin_bypasses_everything():
    req = FakeRequest({"admin": True})
    assert can_act_as(req, "123")
    assert can_act_as(req, "999")  # admin can act for anyone
    assert is_logged_in(req)


def test_manager_can_only_act_as_self():
    req = FakeRequest({"manager_id": "123"})
    assert can_act_as(req, "123")
    assert can_act_as(req, "123", "456")  # is one of the parties
    assert not can_act_as(req, "456")
    assert is_logged_in(req)


def test_anonymous_cannot_act_and_is_not_logged_in():
    req = FakeRequest({})
    assert not can_act_as(req, "123")
    assert not is_logged_in(req)


def test_ids_compared_as_strings():
    req = FakeRequest({"manager_id": "123"})
    assert can_act_as(req, 123)  # int form coerced to str


# ---- is_owner (Tucker-only portal gate) ----
def test_owner_only_matches_owner_entry_id(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    owner = owner_entry_id()
    assert is_owner(FakeRequest({"manager_id": owner}))
    assert is_owner(FakeRequest({"manager_id": int(owner)}))  # int coerced to str


def test_owner_rejects_admin_password_and_other_managers(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    # a shared admin session alone is NOT the owner
    assert not is_owner(FakeRequest({"admin": True}))
    # a different logged-in manager is not the owner
    assert not is_owner(FakeRequest({"manager_id": "999999"}))
    assert not is_owner(FakeRequest({}))


def test_owner_env_override(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("OWNER_ENTRY_ID", "777")
    assert is_owner(FakeRequest({"manager_id": "777"}))
    assert not is_owner(FakeRequest({"manager_id": "43908"}))


def test_owner_open_in_demo(monkeypatch):
    monkeypatch.setenv("APP_ENV", "demo")
    assert is_owner(FakeRequest({"manager_id": "anyone"}))
