"""Unit tests for the v2 pure scoring engine (no DB)."""

from scoring import (
    apply_auto_subs,
    h2h_result,
    legal_formation,
    score_lineup,
)

# A legal 1-3-4-3 starting XI plus a 4-man bench, one player per position group.
# Positions: GK1 | D1 D2 D3 | M1 M2 M3 M4 | F1 F2 F3   bench: GK2 D4 M5 F4
POS = {
    "GK1": "GKP", "GK2": "GKP",
    "D1": "DEF", "D2": "DEF", "D3": "DEF", "D4": "DEF",
    "M1": "MID", "M2": "MID", "M3": "MID", "M4": "MID", "M5": "MID",
    "F1": "FWD", "F2": "FWD", "F3": "FWD", "F4": "FWD",
}
STARTERS = ["GK1", "D1", "D2", "D3", "M1", "M2", "M3", "M4", "F1", "F2", "F3"]
BENCH = ["GK2", "D4", "M5", "F4"]


def _mins(**overrides):
    """All squad players played 90 unless overridden (e.g. F1=0)."""
    m = {pid: 90 for pid in POS}
    m.update(overrides)
    return m


def test_legal_formation():
    assert legal_formation([POS[p] for p in STARTERS])  # 1-3-4-3
    # too few defenders (2) is illegal
    assert not legal_formation(["GKP", "DEF", "DEF", "MID", "MID", "MID", "MID",
                                "MID", "FWD", "FWD", "FWD"])
    # two keepers is illegal
    assert not legal_formation(["GKP", "GKP", "DEF", "DEF", "DEF", "MID", "MID",
                                "MID", "FWD", "FWD", "FWD"])
    assert not legal_formation(["GKP"] * 11)  # wrong counts
    assert not legal_formation([POS[p] for p in STARTERS][:10])  # not 11


def test_no_subs_when_all_play():
    xi = apply_auto_subs(STARTERS, BENCH, POS, _mins())
    assert xi == STARTERS


def test_outfield_starter_dnp_promotes_first_legal_bench():
    # F1 (fwd) plays 0 min. First outfield bench is D4; 1-4-4-2 is legal, so D4 in.
    xi = apply_auto_subs(STARTERS, BENCH, POS, _mins(F1=0))
    assert "F1" not in xi and "D4" in xi
    assert legal_formation([POS[p] for p in xi])


def test_gk_dnp_promotes_bench_gk_only():
    xi = apply_auto_subs(STARTERS, BENCH, POS, _mins(GK1=0))
    assert "GK1" not in xi and "GK2" in xi
    # outfield bench untouched
    assert "D4" not in xi and "M5" not in xi and "F4" not in xi


def test_illegal_formation_subs_are_skipped_in_favor_of_legal_one():
    # D1 (def) DNP. Bench order M5, F4, D4, GK2: replacing a DEF with M5 or F4 would
    # drop DEF to 2 (illegal), so both are skipped and D4 (keeps 3 DEF) comes in.
    bench = ["M5", "F4", "D4", "GK2"]
    xi = apply_auto_subs(STARTERS, bench, POS, _mins(D1=0))
    assert "D1" not in xi and "D4" in xi
    assert "M5" not in xi and "F4" not in xi
    assert legal_formation([POS[p] for p in xi])


def test_non_playing_starter_with_no_legal_sub_stays_in():
    # Everyone on the bench also DNP: nobody can come on, XI is unchanged.
    dead_bench = {b: 0 for b in BENCH}
    xi = apply_auto_subs(STARTERS, BENCH, POS, _mins(F1=0, **dead_bench))
    assert xi == STARTERS  # F1 stays (and will score 0)


def test_score_lineup_sums_resolved_xi():
    players = {pid: {"pos": POS[pid], "minutes": 90, "points": 2} for pid in POS}
    # F1 blanks (0 min, 0 pts); bench D4 plays and scores 6 -> comes on for F1.
    players["F1"] = {"pos": "FWD", "minutes": 0, "points": 0}
    players["D4"] = {"pos": "DEF", "minutes": 90, "points": 6,
                     "goals": 1, "assists": 1, "clean_sheets": 1}
    res = score_lineup(STARTERS, BENCH, players)
    # 10 starters * 2 + D4's 6 = 26
    assert res["total"] == 26
    assert "D4" in res["resolved_xi"] and "F1" not in res["resolved_xi"]
    assert res["team_goals"] == 1 and res["team_assists"] == 1
    assert res["team_clean_sheets"] == 1


def test_h2h_result():
    assert h2h_result(50, 40) == "home"
    assert h2h_result(40, 50) == "away"
    assert h2h_result(45, 45) == "draw"
    assert h2h_result(None, 0) == "draw"


def test_v2_lineup_reconstruction_from_player_points():
    # services._v2_lineup_from_points is pure (no DB): reconstruct starters/bench
    # from a gameweek_points.player_points JSONB list (positions 1-15).
    from services import _v2_lineup_from_points

    pos_by_fpl = {i: POS_LIST[i - 1] for i in range(1, 16)}
    entries = [{"fpl_id": i, "position": i, "minutes": 90, "points": 1}
               for i in range(1, 16)]
    lu = _v2_lineup_from_points(entries, pos_by_fpl)
    assert lu["starters"] == list(range(1, 12))   # 1-11, in order
    assert lu["bench"] == [12, 13, 14, 15]         # 12-15, in order
    assert lu["players"][1]["pos"] == "GKP"
    # a player with unknown position is skipped (can't validate formation)
    lu2 = _v2_lineup_from_points(entries + [{"fpl_id": 99, "position": 3}], pos_by_fpl)
    assert 99 not in lu2["players"]


# Position layout for a legal 1-3-4-3 XI + bench, indexed 1..15 (matches fpl_id).
POS_LIST = ["GKP", "DEF", "DEF", "DEF", "MID", "MID", "MID", "MID",
            "FWD", "FWD", "FWD", "GKP", "DEF", "MID", "FWD"]


# ---- M2: lineup validation + app-lineup engine path ----

# A 15-man squad (2 GK, 5 DEF, 5 MID, 3 FWD), pids 1..15.
SQUAD_POS = {
    1: "GKP", 2: "GKP",
    3: "DEF", 4: "DEF", 5: "DEF", 6: "DEF", 7: "DEF",
    8: "MID", 9: "MID", 10: "MID", 11: "MID", 12: "MID",
    13: "FWD", 14: "FWD", 15: "FWD",
}
SQUAD = set(SQUAD_POS)


def test_validate_lineup_accepts_legal_submission():
    from rules import validate_lineup
    starters = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]  # 1-4-3-3
    bench = [2, 6, 7, 12]
    validate_lineup(starters, bench, SQUAD_POS, SQUAD)  # no raise


def test_validate_lineup_rejects_bad_counts_and_formation():
    from rules import RuleViolation, validate_lineup
    import pytest

    # only 10 starters
    with pytest.raises(RuleViolation):
        validate_lineup([1, 3, 4, 5, 8, 9, 10, 13, 14, 15], [2, 6, 7, 11, 12],
                        SQUAD_POS, SQUAD)
    # illegal formation: 2 GK in the XI (1 and 2), too few outfield of a position
    with pytest.raises(RuleViolation):
        validate_lineup([1, 2, 3, 4, 5, 8, 9, 10, 13, 14, 15], [6, 7, 11, 12],
                        SQUAD_POS, SQUAD)
    # a player not on the squad (99)
    with pytest.raises(RuleViolation):
        validate_lineup([99, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15], [2, 6, 7, 12],
                        SQUAD_POS, SQUAD)


def test_app_lineup_changes_engine_result():
    # Same squad points, two different lineups → different totals, proving the
    # engine scores the app lineup, not a fixed set.
    from services import _v2_score_gp

    pos_by_fpl = dict(SQUAD_POS)
    # player_points: everyone plays 90; pid 15 (FWD) scores 10, others score 1.
    entries = []
    for pid in range(1, 16):
        entries.append({"fpl_id": pid, "position": pid, "minutes": 90,
                        "points": 10 if pid == 15 else 1})

    class _GP:
        player_points = entries

    # Lineup A benches pid 15 → its 10 pts excluded (10 starters *1 + bench GK... )
    lineup_a = {"starters": [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 12], "bench": [2, 6, 7, 15]}
    # Lineup B starts pid 15 instead of pid 12 (both MID/FWD swap keeps legality)
    lineup_b = {"starters": [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15], "bench": [2, 6, 7, 12]}
    a = _v2_score_gp(_GP(), pos_by_fpl, lineup_a)["total"]
    b = _v2_score_gp(_GP(), pos_by_fpl, lineup_b)["total"]
    assert b == a + 9  # pid 15 (10) starts instead of pid 12 (1)
