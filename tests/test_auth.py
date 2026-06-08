"""Tests for per-manager auth helpers (pure; no DB). Run: pytest"""

from auth import can_act_as, hash_password, is_logged_in, verify_password


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
