"""Tests for the league rules engine. Run: pytest"""

from rules import (
    ANTI_TANKING_MIN_WEEKS,
    ANTI_TANKING_MIN_ZERO_PLAYERS,
    MIN_IL_STAY_GWS,
    PAYOUT_STRUCTURE,
    SEASON_LAST_GW,
    compute_payouts,
    h2h_standings,
    il_can_return,
    il_same_position,
    is_anti_tanking_infraction,
    match_winner,
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


# ---- injury list ----
def test_il_same_position():
    assert il_same_position("DEF", "DEF")
    assert not il_same_position("DEF", "MID")
    assert not il_same_position(None, "DEF")
    assert not il_same_position("DEF", None)


def test_il_min_stay_enforced():
    # placed GW10, min stay 4 -> earliest return GW14
    assert not il_can_return(10, 13)  # only 3 GWs
    assert il_can_return(10, 14)  # exactly 4 GWs
    assert il_can_return(10, 20)


def test_il_season_end_forces_return():
    # placed GW36: only 2 GWs by GW38, so the min-stay alone would block it...
    assert il_can_return(36, 38, min_stay=4, last_gw=99) is False
    # ...but the real season-end (GW38) override allows the automatic return.
    assert il_can_return(36, 38)
    assert il_can_return(36, 39)


def test_il_min_stay_constant():
    assert MIN_IL_STAY_GWS == 4
    assert SEASON_LAST_GW == 38


# ---- cups: seeding + knockout winner ----
def test_h2h_standings_orders_by_points_then_pf():
    # A beats B and C; B beats C; C loses both. A=6pts, B=3, C=0.
    results = [
        ("A", "B", 50, 40),  # A win
        ("A", "C", 60, 30),  # A win
        ("B", "C", 45, 20),  # B win
    ]
    assert h2h_standings(results) == ["A", "B", "C"]


def test_h2h_standings_draw_and_pf_tiebreak():
    # A vs B draw (1pt each, equal pf 50 -> alphabetical A first); C beats D (3pts).
    results = [("A", "B", 50, 50), ("C", "D", 10, 9)]
    assert h2h_standings(results) == ["C", "A", "B", "D"]


def test_match_winner_by_score():
    assert match_winner(80, 75, seed_a=3, seed_b=6) == "a"
    assert match_winner(70, 75, seed_a=3, seed_b=6) == "b"


def test_match_winner_tie_breaks_to_better_seed():
    assert match_winner(70, 70, seed_a=3, seed_b=6) == "a"  # seed 3 better
    assert match_winner(70, 70, seed_a=6, seed_b=3) == "b"


def test_match_winner_missing_scores():
    assert match_winner(None, None, seed_a=1, seed_b=2) == "a"


# ---- payouts ----
def test_payout_amounts_match_stated_structure():
    # 25/26: $125 entry x 10 = $1,250 base pot. Distinct managers per slot.
    r = {
        "league_1": "L1", "league_2": "L2", "league_3": "L3",
        "cup_1": "C1", "cup_2": "C2", "cup_3": "C3",
        "pup_cup": "PUP", "last_place": "LAST",
    }
    p = compute_payouts(r, num_managers=10)
    assert p["L1"]["total"] == 625.0  # 40% (500) + 125 last-place fine
    assert p["L2"]["total"] == 187.50
    assert p["L3"]["total"] == 62.50
    assert p["C1"]["total"] == 312.50
    assert p["C2"]["total"] == 125.0
    assert p["C3"]["total"] == 62.50
    assert p["PUP"]["total"] == 150.0
    assert p["LAST"]["total"] == -125.0  # owes the fine


def test_payout_stacks_when_one_manager_wins_multiple():
    # league winner also wins the Cup
    r = {"league_1": "A", "cup_1": "A"}
    p = compute_payouts(r, num_managers=10)
    # 500 (40%) + 125 fine + 312.50 (cup) = 937.50
    assert p["A"]["total"] == 937.50
    assert len(p["A"]["breakdown"]) == 3


def test_payout_other_fines_go_to_league_winner():
    r = {"league_1": "A", "last_place": "B"}
    p = compute_payouts(r, num_managers=10, other_fines=40.0)
    assert p["A"]["total"] == 500.0 + 125.0 + 40.0  # 40% + fine + other fines


def test_payout_skips_missing_slots():
    p = compute_payouts({"league_1": "A"}, num_managers=10)
    assert set(p.keys()) == {"A"}


def test_payout_structure_percentages_sum_to_one():
    assert round(sum(PAYOUT_STRUCTURE["pct"].values()), 4) == 1.0
