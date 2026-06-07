"""Tests for the league rules engine. Run: pytest"""

from rules import (
    ANTI_TANKING_MIN_RUN,
    is_anti_tanking_infraction,
    longest_zero_minute_run,
)


def _xi(minutes: list[int]) -> list[dict]:
    """Build a starting-XI player_points list from a list of minutes."""
    return [
        {"fpl_id": i, "position": i + 1, "is_starting": True, "minutes": m}
        for i, m in enumerate(minutes)
    ]


def test_no_zero_minute_players():
    assert longest_zero_minute_run(_xi([90] * 11)) == 0
    assert not is_anti_tanking_infraction(_xi([90] * 11))


def test_run_below_threshold_not_flagged():
    # two consecutive zeros only
    assert longest_zero_minute_run(_xi([0, 0, 90, 0, 45])) == 2
    assert not is_anti_tanking_infraction(_xi([0, 0, 90, 0, 45]))


def test_exactly_three_consecutive_is_flagged():
    assert longest_zero_minute_run(_xi([90, 0, 0, 0, 90])) == 3
    assert is_anti_tanking_infraction(_xi([90, 0, 0, 0, 90]))


def test_run_resets_on_nonzero():
    # 2 zeros, break, 2 zeros -> longest run is 2, not 4
    assert longest_zero_minute_run(_xi([0, 0, 90, 0, 0])) == 2
    assert not is_anti_tanking_infraction(_xi([0, 0, 90, 0, 0]))


def test_run_at_end_of_lineup():
    assert is_anti_tanking_infraction(_xi([90, 90, 0, 0, 0]))


def test_bench_zero_minutes_ignored():
    # 11 starters all played; 4 bench with 0 minutes must NOT trigger.
    picks = _xi([90] * 11) + [
        {"fpl_id": 100 + i, "position": 12 + i, "is_starting": False, "minutes": 0}
        for i in range(4)
    ]
    assert longest_zero_minute_run(picks) == 0
    assert not is_anti_tanking_infraction(picks)


def test_missing_minutes_treated_as_zero():
    picks = [
        {"fpl_id": 1, "position": 1, "is_starting": True},  # no minutes key
        {"fpl_id": 2, "position": 2, "is_starting": True, "minutes": 0},
        {"fpl_id": 3, "position": 3, "is_starting": True, "minutes": None},
    ]
    assert longest_zero_minute_run(picks) == 3
    assert is_anti_tanking_infraction(picks)


def test_threshold_is_three():
    assert ANTI_TANKING_MIN_RUN == 3
