"""Tests for the league rules engine. Run: pytest"""

from rules import (
    ANTI_TANKING_MIN_WEEKS,
    ANTI_TANKING_MIN_ZERO_PLAYERS,
    is_anti_tanking_infraction,
    tanking_windows,
    zero_minute_count,
)


# ---- zero_minute_count (whole 15-man squad) ----
def _squad(minutes: list[int]) -> list[dict]:
    return [{"fpl_id": i, "minutes": m} for i, m in enumerate(minutes)]


def test_zero_minute_count_counts_whole_squad():
    # 4 zeros among 15 (incl. bench) all count
    assert zero_minute_count(_squad([90, 0, 0, 45, 0, 90, 0] + [90] * 8)) == 4


def test_zero_minute_count_missing_minutes_is_zero():
    assert zero_minute_count([{"fpl_id": 1}, {"fpl_id": 2, "minutes": None}]) == 2


def test_zero_minute_count_empty():
    assert zero_minute_count([]) == 0
    assert zero_minute_count(None) == 0


# ---- tanking_windows (>=3 zero players for >=3 consecutive GWs) ----
def test_three_straight_weeks_flagged():
    counts = {10: 3, 11: 4, 12: 3}
    assert tanking_windows(counts) == [[10, 11, 12]]
    assert is_anti_tanking_infraction(counts)


def test_two_straight_weeks_not_flagged():
    counts = {10: 5, 11: 5}
    assert tanking_windows(counts) == []
    assert not is_anti_tanking_infraction(counts)


def test_below_player_threshold_breaks_run():
    # GW11 has only 2 zero-minute players -> breaks the streak
    counts = {10: 3, 11: 2, 12: 3, 13: 3}
    # only 12,13 qualify consecutively -> length 2 -> not flagged
    assert tanking_windows(counts) == []
    assert not is_anti_tanking_infraction(counts)


def test_missing_gameweek_breaks_consecutiveness():
    # 10 and 12 qualify but 11 absent -> not consecutive
    counts = {10: 4, 12: 4, 13: 4, 14: 4}
    assert tanking_windows(counts) == [[12, 13, 14]]


def test_longer_run_returns_full_window():
    counts = {5: 3, 6: 3, 7: 4, 8: 5, 9: 3}
    assert tanking_windows(counts) == [[5, 6, 7, 8, 9]]


def test_multiple_separate_windows():
    counts = {1: 3, 2: 3, 3: 3, 4: 0, 5: 3, 6: 4, 7: 3}
    assert tanking_windows(counts) == [[1, 2, 3], [5, 6, 7]]


def test_thresholds_are_three():
    assert ANTI_TANKING_MIN_ZERO_PLAYERS == 3
    assert ANTI_TANKING_MIN_WEEKS == 3
