"""The draft-prep model: value over replacement, keeper choice, simulated draft.

All pure — no database. The model estimates what nine other humans will do, so the
tests are less about arithmetic than about the places a plausible-looking
implementation gives a confidently wrong answer:

  - a replacement baseline pegged to roster depth instead of starter depth, which
    values a backup goalkeeper like a starter and has the league keeping 13 of them;
  - greedy keeper selection, which is optimal for a separable objective and NOT for
    this one;
  - best-available drafting, which leaves a manager with no goalkeeper and no picks
    left to fix it.

Each test names the mutation it kills.
"""

import random

import pytest

import draftprep
from draftprep import SLOT_WEIGHTS, STARTER_DEMAND, Rec
from rules import (
    KEEPER_MAX_SELECTIONS,
    ROSTER_SIZE,
    SQUAD_POSITION_LIMITS,
    XI_POSITION_MINIMUMS,
)


def _rec(name, pos, pts, *, acq="draft", eligible=True, pid=None):
    return Rec(pid or name, name, pos, pts, acq, eligible)


# ---- constants ------------------------------------------------------------
def test_the_squad_shape_adds_up_to_a_squad():
    assert sum(SQUAD_POSITION_LIMITS.values()) == ROSTER_SIZE


def test_the_xi_minimums_fit_in_an_xi():
    assert sum(XI_POSITION_MINIMUMS.values()) <= 11
    for pos, floor in XI_POSITION_MINIMUMS.items():
        assert floor <= SQUAD_POSITION_LIMITS[pos]


def test_starter_demand_is_an_xi():
    assert sum(STARTER_DEMAND.values()) == 11


def test_every_squad_slot_has_a_weight():
    """A missing weight would silently make the last slot free."""
    for pos, limit in SQUAD_POSITION_LIMITS.items():
        assert len(SLOT_WEIGHTS[pos]) == limit


# ---- replacement level ----------------------------------------------------
def _gk_pool(points):
    return [_rec(f"GK{i}", "GKP", p) for i, p in enumerate(points)]


def test_replacement_is_the_player_after_the_last_starter():
    """teams=2, demand 1 -> two goalkeepers start, so the baseline is the THIRD."""
    pool = _gk_pool([200, 190, 180, 170, 160])
    rep, diag = draftprep.replacement_levels(pool, teams=2, demand={"GKP": 1})
    assert rep["GKP"] == 180.0
    assert diag["GKP"] == {"index": 3, "pool": 5}


def test_a_better_player_above_the_cut_pushes_the_baseline_down_one_rank():
    """Kills an off-by-one in one direction. Asserting rep[GKP] > rep[FWD] wouldn't."""
    pool = _gk_pool([200, 190, 180, 170, 160]) + _gk_pool([250])
    rep, _ = draftprep.replacement_levels(pool, teams=2, demand={"GKP": 1})
    assert rep["GKP"] == 190.0


def test_a_worse_player_below_the_cut_does_not_move_the_baseline():
    """Kills the off-by-one in the other direction."""
    pool = _gk_pool([200, 190, 180, 170, 160]) + _gk_pool([5])
    rep, _ = draftprep.replacement_levels(pool, teams=2, demand={"GKP": 1})
    assert rep["GKP"] == 180.0


def test_a_pool_shallower_than_the_demand_uses_the_last_player(caplog):
    pool = _gk_pool([100, 50])
    rep, diag = draftprep.replacement_levels(pool, teams=10, demand={"GKP": 1})
    assert rep["GKP"] == 50.0 and diag["GKP"]["index"] == 2


def test_an_empty_position_does_not_raise():
    rep, diag = draftprep.replacement_levels([], teams=10)
    assert rep["GKP"] == 0.0 and diag["GKP"]["pool"] == 0


# ---- squad value ----------------------------------------------------------
def test_a_second_goalkeeper_is_worth_almost_nothing():
    """The whole reason for the saturation weights: without them a backup keeper
    scores his full projected season and the model hoards goalkeepers."""
    rep = {"GKP": 100.0, "DEF": 100.0, "MID": 100.0, "FWD": 100.0}
    one = draftprep.squad_value([_rec("A", "GKP", 200)], rep)
    two = draftprep.squad_value([_rec("A", "GKP", 200), _rec("B", "GKP", 200)], rep)
    assert one == pytest.approx(100.0)
    assert two - one == pytest.approx(15.0), "the 2nd keeper counted as a starter"


def test_players_beyond_the_positional_limit_are_worthless():
    rep = {p: 0.0 for p in SQUAD_POSITION_LIMITS}
    three = [_rec(f"F{i}", "FWD", 100) for i in range(3)]
    assert draftprep.squad_value(three + [_rec("F4", "FWD", 100)], rep) == \
        draftprep.squad_value(three, rep)


def test_a_below_replacement_player_subtracts():
    rep = {p: 100.0 for p in SQUAD_POSITION_LIMITS}
    assert draftprep.squad_value([_rec("A", "MID", 40)], rep) < 0


# ---- keeper prediction ----------------------------------------------------
REP_FLAT = {"GKP": 100.0, "DEF": 100.0, "MID": 100.0, "FWD": 100.0}


def test_the_keeper_cap_binds():
    cands = [_rec(f"M{i}", "MID", 300 - i) for i in range(8)]
    out = draftprep.predict_keepers(cands, REP_FLAT)
    assert len(out["keepers"]) == KEEPER_MAX_SELECTIONS
    assert "count" in out["binding"]


def test_the_waiver_cap_binds_even_against_a_huge_score():
    """A cap that only bites on ties isn't a cap: the third waiver player here is by
    far the best asset available and must still be left out."""
    cands = [
        _rec("W1", "MID", 300, acq="waiver"),
        _rec("W2", "DEF", 290, acq="waiver"),
        _rec("W3", "FWD", 999, acq="waiver"),
        _rec("D1", "MID", 150), _rec("D2", "DEF", 150), _rec("D3", "FWD", 150),
    ]
    out = draftprep.predict_keepers(cands, REP_FLAT)
    names = {c.name for c in out["keepers"]}
    assert sum(1 for c in out["keepers"] if c.acquisition == "waiver") == 2
    assert "W3" not in names or "W1" not in names or "W2" not in names
    assert len(names) == 5


def test_ineligible_candidates_are_never_kept():
    cands = [_rec("Star", "MID", 999, eligible=False), _rec("Ok", "MID", 150)]
    assert [c.name for c in draftprep.predict_keepers(cands, REP_FLAT)["keepers"]] == ["Ok"]


def test_nobody_keeps_a_backup_goalkeeper():
    """The headline failure of the naive model, as a test."""
    cands = [_rec("GK1", "GKP", 180), _rec("GK2", "GKP", 175)] + \
            [_rec(f"M{i}", "MID", 170 - i) for i in range(5)]
    kept = draftprep.predict_keepers(cands, REP_FLAT)["keepers"]
    assert sum(1 for c in kept if c.position == "GKP") <= 1


def _greedy(cands, rep, max_keepers=5, max_waiver=2):
    chosen, waivers = [], 0
    for c in sorted((c for c in cands if c.eligible),
                    key=lambda c: -(c.points - rep[c.position])):
        if len(chosen) == max_keepers:
            break
        if c.acquisition == "waiver" and waivers == max_waiver:
            continue
        chosen.append(c)
        waivers += c.acquisition == "waiver"
    return chosen


def test_greedy_and_brute_force_agree_on_a_separable_ranking():
    """Where value is just points-over-replacement per player, greedy is optimal."""
    cands = [_rec(f"M{i}", "MID", 200 - 10 * i) for i in range(4)] + \
            [_rec(f"D{i}", "DEF", 190 - 10 * i) for i in range(4)]
    brute = draftprep.predict_keepers(cands, REP_FLAT)["keepers"]
    assert {c.name for c in brute} == {c.name for c in _greedy(cands, REP_FLAT)}


def test_brute_force_beats_greedy_when_saturation_bites():
    """Greedy ranks the elite 2nd keeper on raw value and takes him; brute force sees
    that his squad slot is worth 15% and spends it elsewhere. This is why the module
    must not be 'simplified' back to greedy."""
    cands = [
        _rec("GK1", "GKP", 300), _rec("GK2", "GKP", 295),
        _rec("M1", "MID", 290), _rec("M2", "MID", 285), _rec("M3", "MID", 280),
        _rec("D1", "DEF", 275),
    ]
    brute = draftprep.predict_keepers(cands, REP_FLAT)
    greedy = _greedy(cands, REP_FLAT)
    assert "GK2" not in {c.name for c in brute["keepers"]}
    assert "GK2" in {c.name for c in greedy}, "fixture no longer separates the two"
    assert brute["value"] > draftprep.squad_value(greedy, REP_FLAT)


def test_the_margin_shows_how_close_the_call_was():
    tight = [_rec("A", "MID", 200), _rec("B", "MID", 199.9)]
    clear = [_rec("A", "MID", 200), _rec("B", "MID", 10)]
    tight_margin = draftprep.predict_keepers(tight, REP_FLAT, max_keepers=1)["margin"]
    clear_margin = draftprep.predict_keepers(clear, REP_FLAT, max_keepers=1)["margin"]
    assert tight_margin == pytest.approx(0.1), "a coin flip should show a thin margin"
    assert clear_margin >= 50, "an obvious keep should show a wide one"


# ---- the simulation -------------------------------------------------------
def _slots(order):
    return [{"pick": i, "round": (i - 1) // len(set(order)) + 1, "manager": m}
            for i, m in enumerate(order, start=1)]


def test_a_manager_never_exceeds_a_positional_limit():
    """The squad is pre-seeded past the XI minimums and the pool offers a lower-value
    fallback on purpose. Start from an empty squad with only forwards available and
    the RESERVE rule ends up capping the count instead — the test then passes with the
    positional limit deleted entirely."""
    seeded = ([_rec("G", "GKP", 50)] + [_rec(f"D{i}", "DEF", 50) for i in range(3)]
              + [_rec(f"M{i}", "MID", 50) for i in range(2)])
    pool = ([_rec(f"F{i}", "FWD", 300 - i) for i in range(8)]
            + [_rec(f"N{i}", "MID", 200 - i) for i in range(5)])
    out = draftprep.simulate_draft(_slots(["A"] * 6), pool, {"A": seeded},
                                   {p: 0.0 for p in SQUAD_POSITION_LIMITS})
    squad = out["squads"]["A"]
    assert sum(1 for p in squad if p.position == "FWD") == SQUAD_POSITION_LIMITS["FWD"]
    assert all(r["player"] is not None for r in out["picks"]), "picks went unfilled"


def test_keepers_seed_the_positional_counts():
    """A manager who kept three midfielders has two midfield slots left, not five."""
    kept = [_rec(f"K{i}", "MID", 400) for i in range(3)]
    pool = [_rec(f"M{i}", "MID", 300 - i) for i in range(6)]
    out = draftprep.simulate_draft(_slots(["A"] * 6), pool, {"A": kept},
                                   {p: 0.0 for p in SQUAD_POSITION_LIMITS})
    drafted_mids = sum(1 for r in out["picks"] if r["player"] and r["player"].position == "MID")
    assert drafted_mids == 2


def test_a_manager_is_never_left_without_a_goalkeeper():
    """Best-available drafting spends every pick on higher-value outfielders and
    finishes with no keeper — a squad that can't field a legal XI."""
    order = ["A"] * 11
    pool = ([_rec(f"M{i}", "MID", 300 - i) for i in range(5)]
            + [_rec(f"D{i}", "DEF", 250 - i) for i in range(5)]
            + [_rec(f"F{i}", "FWD", 200 - i) for i in range(3)]
            + [_rec("GK1", "GKP", 10)])
    out = draftprep.simulate_draft(_slots(order), pool, {"A": []},
                                   {p: 0.0 for p in SQUAD_POSITION_LIMITS})
    squad = out["squads"]["A"]
    for pos, floor in XI_POSITION_MINIMUMS.items():
        assert sum(1 for p in squad if p.position == pos) >= floor, pos


def test_the_reserve_rule_fires_on_the_boundary_and_not_before():
    """One pick left and no keeper -> take the keeper. Two picks left -> the better
    player first. A rule that always prioritises need would fail the second half."""
    rep = {p: 0.0 for p in SQUAD_POSITION_LIMITS}
    squad = [_rec(f"X{i}", "DEF", 100) for i in range(3)] + \
            [_rec(f"Y{i}", "MID", 100) for i in range(2)] + [_rec("Z", "FWD", 100)]
    pool = [_rec("BigMid", "MID", 300), _rec("GK", "GKP", 10)]

    one = draftprep.simulate_draft(_slots(["A"]), list(pool), {"A": list(squad)}, rep)
    assert one["picks"][0]["player"].position == "GKP"

    two = draftprep.simulate_draft(_slots(["A", "A"]), list(pool), {"A": list(squad)}, rep)
    assert two["picks"][0]["player"].name == "BigMid"
    assert two["picks"][1]["player"].position == "GKP"


def test_a_full_squad_forfeits_its_remaining_slots():
    """Per the league: extra picks lapse, and their players stay in the pool."""
    rep = {p: 0.0 for p in SQUAD_POSITION_LIMITS}
    full = ([_rec(f"G{i}", "GKP", 50) for i in range(2)]
            + [_rec(f"D{i}", "DEF", 50) for i in range(5)]
            + [_rec(f"M{i}", "MID", 50) for i in range(5)]
            + [_rec(f"F{i}", "FWD", 50) for i in range(3)])
    assert len(full) == ROSTER_SIZE
    pool = [_rec("Spare", "MID", 300)]
    out = draftprep.simulate_draft(_slots(["A", "A"]), pool, {"A": full}, rep)
    assert [r["reason"] for r in out["picks"]] == ["forfeited", "forfeited"]
    assert [p.name for p in out["undrafted"]] == ["Spare"]


def test_every_slot_is_resolved():
    """A slot that silently vanishes would make the availability maths wrong."""
    rep = {p: 0.0 for p in SQUAD_POSITION_LIMITS}
    slots = _slots(["A", "B"] * 5)
    pool = [_rec(f"M{i}", "MID", 300 - i) for i in range(4)] + \
           [_rec(f"D{i}", "DEF", 200 - i) for i in range(6)]
    out = draftprep.simulate_draft(slots, pool, {"A": [], "B": []}, rep)
    assert len(out["picks"]) == len(slots)


def test_the_simulation_is_deterministic_under_input_order():
    rep = {p: 0.0 for p in SQUAD_POSITION_LIMITS}
    pool = [_rec(f"M{i}", "MID", 100) for i in range(5)] + \
           [_rec(f"D{i}", "DEF", 100) for i in range(5)]
    slots = _slots(["A", "B"] * 4)
    first = None
    for seed in range(10):
        shuffled = list(pool)
        random.Random(seed).shuffle(shuffled)
        out = draftprep.simulate_draft(slots, shuffled, {"A": [], "B": []}, rep)
        names = [r["player"].name if r["player"] else None for r in out["picks"]]
        first = first if first is not None else names
        assert names == first, "equal-valued players swapped between runs"
