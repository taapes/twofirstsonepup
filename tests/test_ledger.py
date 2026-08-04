"""Unit tests for the v2 app-owned squad ledger (pure logic, no DB)."""

import pytest

from rules import RuleViolation, fold_moves, validate_roster_add, validate_roster_drop


def test_fold_moves_add_and_drop():
    moves = [
        ("a", "add"), ("b", "add"), ("c", "add"),
        ("b", "drop"),              # b left
        ("d", "add"),               # d joined
    ]
    assert fold_moves(moves) == {"a", "c", "d"}


def test_fold_moves_readd_after_drop():
    moves = [("a", "add"), ("a", "drop"), ("a", "add")]
    assert fold_moves(moves) == {"a"}


def test_fold_moves_empty():
    assert fold_moves([]) == set()


def test_validate_roster_add_ok():
    validate_roster_add(squad_size=14, already_owned=False, owned_by_other=False)


def test_validate_roster_add_rejections():
    with pytest.raises(RuleViolation):  # full squad
        validate_roster_add(squad_size=15, already_owned=False, owned_by_other=False)
    with pytest.raises(RuleViolation):  # already own
        validate_roster_add(squad_size=10, already_owned=True, owned_by_other=False)
    with pytest.raises(RuleViolation):  # owned by another manager
        validate_roster_add(squad_size=10, already_owned=False, owned_by_other=True)


def test_validate_roster_drop():
    validate_roster_drop(owned=True)  # no raise
    with pytest.raises(RuleViolation):
        validate_roster_drop(owned=False)
