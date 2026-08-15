"""The draft-prep model under the goalie-team rule.

Two squad shapes now have to be simulable, and both are real: the FPL fifteen that
every archived season was drafted under (covered by tests/test_draftprep_model.py,
which must keep passing unchanged), and the thirteen-outfielders-plus-a-club shape
from 2026. That's why the shape is a PARAMETER — a module constant would have made
the archive unsimulable the moment the rule landed.

What's genuinely new and worth killing mutations over:

  - one of every manager's picks buys something that isn't in the pool, so it must be
    neither filled nor counted as a pick they can spend on a player;
  - a manager who has ALREADY taken their club (live mode) must stop having one held
    back, or the model hands them one outfielder too few;
  - clubs are ranked, never simulated — twenty of them for ten managers is not a
    scarce resource and a "gone by pick N" would be noise wearing a number.

All pure — no database.
"""

import pytest

import draftprep
from draftprep import FPL_SHAPE, GOALIE_TEAM_SHAPE, Rec
from rules import (
    OUTFIELD_POSITION_LIMITS,
    OUTFIELD_SQUAD_SIZE,
    OUTFIELD_XI_MINIMUMS,
    ROSTER_SIZE,
    SQUAD_POSITION_LIMITS,
)

SHAPE = GOALIE_TEAM_SHAPE
REP = {p: 0.0 for p in SHAPE.positions}


def _rec(name, pos, pts, *, acq="draft", eligible=True, pid=None):
    return Rec(pid or name, name, pos, pts, acq, eligible)


def _slots(order):
    return [{"pick": i, "round": (i - 1) // len(set(order)) + 1, "manager": m}
            for i, m in enumerate(order, start=1)]


# ---- the shape itself ------------------------------------------------------
def test_the_outfield_shape_adds_up_to_a_squad_minus_its_keepers():
    assert sum(SHAPE.limits.values()) == SHAPE.squad_size == OUTFIELD_SQUAD_SIZE
    assert SHAPE.squad_size + SQUAD_POSITION_LIMITS["GKP"] == ROSTER_SIZE


def test_goalkeepers_are_not_a_position_in_this_shape():
    assert "GKP" not in SHAPE.positions
    assert "GKP" not in SHAPE.limits
    assert "GKP" not in SHAPE.starter_demand
    assert "GKP" not in SHAPE.slot_weights


def test_starter_demand_is_the_outfield_xi():
    """Ten, not eleven — the goalkeeper slot is filled by a club, not from this pool."""
    assert sum(SHAPE.starter_demand.values()) == 10


def test_the_xi_minimums_fit_in_an_outfield_xi():
    assert sum(SHAPE.xi_minimums.values()) <= 10
    for pos, floor in SHAPE.xi_minimums.items():
        assert floor <= SHAPE.limits[pos]


def test_every_squad_slot_has_a_weight():
    """A missing weight would silently make the last slot free."""
    for pos, limit in SHAPE.limits.items():
        assert len(SHAPE.slot_weights[pos]) == limit


def test_one_slot_is_reserved_for_the_club():
    assert SHAPE.reserved_slots == 1
    assert FPL_SHAPE.reserved_slots == 0


def test_the_outfield_shape_is_the_fpl_shape_with_the_keepers_removed():
    """Nothing about outfielders changed. If this fails somebody redefined a squad."""
    assert SHAPE.limits == OUTFIELD_POSITION_LIMITS
    assert SHAPE.xi_minimums == OUTFIELD_XI_MINIMUMS
    for pos in SHAPE.positions:
        assert SHAPE.slot_weights[pos] == FPL_SHAPE.slot_weights[pos]
        assert SHAPE.starter_demand[pos] == FPL_SHAPE.starter_demand[pos]


@pytest.mark.parametrize("mode,expected", [
    ("off", FPL_SHAPE), (None, FPL_SHAPE), ("redraft", SHAPE), ("keeper", SHAPE),
])
def test_shape_for_maps_the_league_mode(mode, expected):
    assert draftprep.shape_for(mode) is expected


# ---- the reserved slot -----------------------------------------------------
def _outfield_pool(n=40):
    return [_rec(f"P{i:02d}", ["DEF", "MID", "FWD"][i % 3], 300 - i) for i in range(n)]


def test_one_pick_per_manager_goes_on_the_club():
    out = draftprep.simulate_draft(
        _slots(["A", "B"] * 14), _outfield_pool(), {"A": [], "B": []}, REP, shape=SHAPE
    )
    reserved = [r for r in out["picks"] if r["reason"] == "goalie team"]
    assert len(reserved) == 2
    assert {r["manager"] for r in reserved} == {"A", "B"}
    assert all(r["player"] is None for r in reserved)


def test_the_reserved_slot_is_not_a_lapsed_pick():
    """It reads as 'forfeited' if the squad-full check runs first, which would show up
    on the prep page as the manager wasting a pick."""
    out = draftprep.simulate_draft(
        _slots(["A"] * 14), _outfield_pool(), {"A": []}, REP, shape=SHAPE
    )
    assert [r["reason"] for r in out["picks"]].count("forfeited") == 0
    assert [r["reason"] for r in out["picks"]].count("goalie team") == 1


def test_the_squad_ends_at_thirteen_outfielders():
    out = draftprep.simulate_draft(
        _slots(["A"] * 14), _outfield_pool(), {"A": []}, REP, shape=SHAPE
    )
    assert len(out["squads"]["A"]) == OUTFIELD_SQUAD_SIZE
    assert sum(1 for r in out["picks"] if r["player"]) == OUTFIELD_SQUAD_SIZE


def test_the_reserved_slot_is_the_managers_last():
    out = draftprep.simulate_draft(
        _slots(["A", "B"] * 14), _outfield_pool(), {"A": [], "B": []}, REP, shape=SHAPE
    )
    a_picks = [r["pick"] for r in out["picks"] if r["manager"] == "A"]
    reserved = next(r["pick"] for r in out["picks"] if r["reason"] == "goalie team"
                    and r["manager"] == "A")
    assert reserved == max(a_picks)


def test_a_manager_who_already_has_a_club_keeps_all_their_picks():
    """Live mode. A's club pick is already recorded, so it isn't in the remaining
    slots and A owes no reservation — 13 slots, 13 outfielders. Without
    `reserved_spent` the model holds one back and shows A finishing a player short.
    """
    slots = _slots(["A", "B"] * 13 + ["B"])   # A: 13 left, B: 14
    assert sum(1 for s in slots if s["manager"] == "A") == 13
    out = draftprep.simulate_draft(
        slots, _outfield_pool(), {"A": [], "B": []}, REP,
        shape=SHAPE, reserved_spent={"A"},
    )
    reserved = [r for r in out["picks"] if r["reason"] == "goalie team"]
    assert [r["manager"] for r in reserved] == ["B"]
    assert sum(1 for r in out["picks"] if r["manager"] == "A" and r["player"]) == 13
    assert len(out["squads"]["A"]) == OUTFIELD_SQUAD_SIZE


def test_the_reserved_slot_does_not_count_as_a_spendable_pick():
    """The XI-minimums rule counts remaining picks. If the club slot is counted, a
    manager thinks he has one more outfield pick than he does and leaves a hole."""
    squad = ([_rec(f"D{i}", "DEF", 100) for i in range(3)]
             + [_rec(f"M{i}", "MID", 100) for i in range(2)])
    pool = [_rec("BigMid", "MID", 300), _rec("OnlyFwd", "FWD", 10)]
    # Two slots: one real pick, then the reserved club slot. The real pick MUST go on
    # the forward — it's the last chance to satisfy the FWD minimum.
    out = draftprep.simulate_draft(_slots(["A", "A"]), pool, {"A": list(squad)}, REP,
                                   shape=SHAPE)
    assert out["picks"][0]["player"].name == "OnlyFwd"
    assert out["picks"][1]["reason"] == "goalie team"


def test_a_manager_is_never_left_short_of_the_last_position_he_needs():
    """The outfield equivalent of the no-goalkeeper failure: best-available drafting
    spends everything on the top of the board and finishes unable to field a legal XI.
    Here the forwards are worthless and the midfielders are not, so only the reserve
    rule gets him his one required forward."""
    pool = ([_rec(f"M{i}", "MID", 300 - i) for i in range(5)]
            + [_rec(f"D{i}", "DEF", 250 - i) for i in range(5)]
            + [_rec(f"F{i}", "FWD", 5 - i) for i in range(3)])
    out = draftprep.simulate_draft(_slots(["A"] * 12), pool, {"A": []}, REP, shape=SHAPE)
    squad = out["squads"]["A"]
    for pos, floor in SHAPE.xi_minimums.items():
        assert sum(1 for p in squad if p.position == pos) >= floor, pos


def test_the_reserve_rule_counts_positions_not_bodies():
    """A real limit of the model, pinned so nobody reads it as a guarantee.

    The rule fires when the number of unmet POSITIONS reaches the number of picks
    left, so it can rescue a minimum that needs one more body but not one that needs
    three. Here the defenders are cheap and the manager has ten picks: he fills MID
    and FWD to their limits and finishes with two defenders, one short of a legal XI.

    Unchanged from the FPL shape — the same construction starves the goalkeeper slot
    there — so this is documented, not introduced. Harmless in practice: a real pool
    has hundreds of players at every position, so the greedy never runs out.
    """
    pool = ([_rec(f"M{i}", "MID", 300 - i) for i in range(5)]
            + [_rec(f"F{i}", "FWD", 250 - i) for i in range(3)]
            + [_rec(f"D{i}", "DEF", 10 - i) for i in range(5)])
    out = draftprep.simulate_draft(_slots(["A"] * 11), pool, {"A": []}, REP, shape=SHAPE)
    squad = out["squads"]["A"]
    assert sum(1 for p in squad if p.position == "DEF") == 2
    assert SHAPE.xi_minimums["DEF"] == 3   # ...which is one short, knowingly


def test_a_full_squad_still_forfeits_extra_slots():
    full = ([_rec(f"D{i}", "DEF", 50) for i in range(5)]
            + [_rec(f"M{i}", "MID", 50) for i in range(5)]
            + [_rec(f"F{i}", "FWD", 50) for i in range(3)])
    assert len(full) == OUTFIELD_SQUAD_SIZE
    out = draftprep.simulate_draft(_slots(["A", "A", "A"]), [_rec("Spare", "MID", 300)],
                                   {"A": full}, REP, shape=SHAPE)
    reasons = [r["reason"] for r in out["picks"]]
    assert reasons.count("forfeited") == 2 and reasons.count("goalie team") == 1
    assert out["undrafted"] and out["undrafted"][0].name == "Spare"


# ---- replacement level under the new shape --------------------------------
def test_the_baseline_ignores_goalkeepers_entirely():
    pool = _outfield_pool() + [_rec(f"G{i}", "GKP", 500) for i in range(5)]
    rep, diag = draftprep.replacement_levels(pool, teams=2, shape=SHAPE)
    assert set(rep) == set(SHAPE.positions)
    assert "GKP" not in diag


# ---- the club big board ----------------------------------------------------
def _clubs(points):
    return [_rec(f"C{i}", "TEAM", p) for i, p in enumerate(points)]


def test_a_clubs_value_is_over_the_best_one_still_available():
    """Ten managers, so the 11th-best club is what you can always still get."""
    clubs = _clubs([200 - 10 * i for i in range(20)])   # 200, 190, ... 10
    values, rep = draftprep.goalie_team_values(clubs, teams=10)
    assert rep == 100                      # the 11th, index 10
    assert values["C0"] == 100             # 200 - 100
    assert values["C10"] == 0              # the replacement club itself
    assert values["C19"] == -90            # worse than replacement, and says so


def test_the_club_baseline_lands_on_the_right_index():
    """Kills an off-by-one in either direction — asserting only that C0 ranks first
    would not."""
    clubs = _clubs([100, 90, 80, 70, 60])
    _v, rep_two = draftprep.goalie_team_values(clubs, teams=2)
    _v, rep_three = draftprep.goalie_team_values(clubs, teams=3)
    assert rep_two == 80 and rep_three == 70


def test_more_managers_than_clubs_does_not_crash():
    clubs = _clubs([100, 50])
    values, rep = draftprep.goalie_team_values(clubs, teams=10)
    assert rep == 50 and values["C0"] == 50


def test_an_empty_club_board_is_empty_not_an_error():
    assert draftprep.goalie_team_values([], teams=10) == ({}, 0.0)


# ---- what did NOT change ---------------------------------------------------
def test_the_fpl_shape_is_untouched_by_default():
    """Every call site that doesn't pass a shape must behave exactly as before, or
    the archive re-simulates differently than it was drafted."""
    pool = ([_rec(f"M{i}", "MID", 300 - i) for i in range(5)]
            + [_rec(f"D{i}", "DEF", 250 - i) for i in range(5)]
            + [_rec(f"F{i}", "FWD", 200 - i) for i in range(3)]
            + [_rec("GK1", "GKP", 10), _rec("GK2", "GKP", 5)])
    rep = {p: 0.0 for p in SQUAD_POSITION_LIMITS}
    out = draftprep.simulate_draft(_slots(["A"] * 15), pool, {"A": []}, rep)
    assert len(out["squads"]["A"]) == ROSTER_SIZE
    assert not [r for r in out["picks"] if r["reason"] == "goalie team"]
    assert sum(1 for p in out["squads"]["A"] if p.position == "GKP") == 2
