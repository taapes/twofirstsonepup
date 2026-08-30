"""Projected auto-substitutions — the pure rule.

FPL applies bench subs only when a gameweek is FINALISED, so a live score shows a
manager carrying a hole FPL will later fill. This projects them.

THE RULE IS NOT FPL'S LITERAL ONE, deliberately. FPL says the incoming player must have
PLAYED; that is only equivalent at gameweek end. Applied live it skips a bench player
whose match is tomorrow, promotes the man behind him, then reverses itself — the
projection thrashes. The test here is "can this player still score?", which is stable
and CONVERGES ON FPL'S RULE EXACTLY once every match is over, because at that point
"not ruled out" and "played > 0" are the same predicate.

That convergence is the strongest check available and it is not a fixture: on a
finalised gameweek the projection must reproduce FPL's own total. Verified against
production GW1 (all ten managers, zero delta) while this was written.

Formation fixtures are REAL XI shapes from GW2 rather than invented ones — Scott's
1-5-2-3 (MID at the minimum) and John's 1-3-5-2 (DEF at the minimum) are precisely the
cases where a blank forces a specific position off the bench.
"""

import pytest

from rules import XI_POSITION_MAXIMUMS, XI_POSITION_MINIMUMS, project_auto_subs

# fpl_id -> position, for a squad shaped like FPL forces: 2 GKP, 5 DEF, 5 MID, 3 FWD.
POSITIONS = {
    1: "GKP", 2: "GKP",
    3: "DEF", 4: "DEF", 5: "DEF", 6: "DEF", 7: "DEF",
    8: "MID", 9: "MID", 10: "MID", 11: "MID", 12: "MID",
    13: "FWD", 14: "FWD", 15: "FWD",
}


def _squad(xi, bench, *, points=None, minutes=None):
    """A player_points list from an XI and a bench, both given as fpl_id lists.

    `xi` order is pick slots 1-11, `bench` is slots 12-15 in substitution priority.
    """
    points = points or {}
    minutes = minutes or {}
    rows = []
    for slot, fid in enumerate(list(xi) + list(bench), start=1):
        rows.append({
            "fpl_id": fid, "position": slot, "is_starting": slot <= 11,
            "minutes": minutes.get(fid, 90), "points": points.get(fid, 0),
        })
    return rows


def _run(entries, ruled_out=frozenset(), positions=None):
    return project_auto_subs(
        entries, positions=positions or POSITIONS, ruled_out=set(ruled_out)
    )


# ---- the baseline ------------------------------------------------------------
def test_nobody_ruled_out_means_no_subs():
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    out = _run(_squad(xi, [2, 6, 7, 12]))
    assert out["subs"] == []
    assert out["xi"] == xi
    assert out["short"] is False


def test_points_are_summed_over_the_effective_xi_not_the_picked_one():
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    # 9 (MID) blanks. Slot 12 is the bench keeper and is illegal, so the DEF at slot
    # 13 comes on with 7 points.
    entries = _squad(xi, [2, 6, 7, 12], points={9: 0, 6: 7, 1: 3}, minutes={9: 0})
    out = _run(entries, ruled_out={9})
    assert out["subs"] == [{"out": 9, "in": 6}]
    assert out["points"] == 10, "3 from the keeper + 7 from the sub"


# ---- the goalkeeper rule, both directions ------------------------------------
def test_a_blanking_keeper_draws_the_bench_keeper_not_an_outfielder():
    """The bench keeper sits at slot 15, LAST in priority — so if the formation check
    were absent, an outfielder at slot 12 would come on and leave the XI keeperless."""
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    out = _run(_squad(xi, [6, 7, 12, 2], minutes={1: 0}), ruled_out={1})
    assert out["subs"] == [{"out": 1, "in": 2}]
    assert POSITIONS[out["xi"][0]] == "GKP"


def test_the_bench_keeper_never_replaces_an_outfielder():
    """He is first in bench order here, so only the formation check keeps him off —
    two keepers is as illegal as none."""
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    out = _run(_squad(xi, [2, 6, 7, 12], minutes={13: 0}), ruled_out={13})
    assert out["subs"] == [{"out": 13, "in": 6}], "the bench keeper is skipped"
    assert sum(1 for f in out["xi"] if POSITIONS[f] == "GKP") == 1


# ---- formation bounds, using real GW2 shapes ---------------------------------
def test_a_blanking_defender_at_the_minimum_must_draw_a_defender():
    """John's real GW2 shape, 1-3-5-2. At three defenders a blank cannot be covered by
    a midfielder — that would leave two, and the XI would be illegal."""
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14]   # 1 GKP, 3 DEF, 5 MID, 2 FWD
    # Bench order puts a MID and a FWD ahead of the defender at slot 15.
    out = _run(_squad(xi, [2, 15, 6, 7], minutes={3: 0}), ruled_out={3})
    assert out["subs"] == [{"out": 3, "in": 6}]
    assert sum(1 for f in out["xi"] if POSITIONS[f] == "DEF") == 3


def test_a_blanking_midfielder_at_the_minimum_must_draw_a_midfielder():
    """Scott's real GW2 shape, 1-5-2-3. Two midfielders is the floor."""
    xi = [1, 3, 4, 5, 6, 7, 8, 9, 13, 14, 15]     # 1 GKP, 5 DEF, 2 MID, 3 FWD
    out = _run(_squad(xi, [2, 10, 11, 12], minutes={8: 0}), ruled_out={8})
    assert out["subs"] == [{"out": 8, "in": 10}]
    assert sum(1 for f in out["xi"] if POSITIONS[f] == "MID") == 2


def test_the_outfield_ceilings_only_bind_on_an_out_of_shape_squad():
    """Worth recording rather than asserting a case that cannot happen.

    For DEF, MID and FWD the SQUAD limit equals the XI limit (5/5/3), so a legal
    15-man squad can never push a position over its ceiling — there simply aren't
    enough of them. Only the keeper ceiling binds in practice, and that is the
    goalkeeper rule tested above.

    Give a squad a SIXTH defender (which the quota enforcement exists to prevent) and
    the ceiling does its job: at five defenders in the XI, a sixth is refused and the
    midfielder behind him comes on instead.
    """
    positions = dict(POSITIONS)
    positions[16] = "DEF"                          # the illegal sixth
    xi = [1, 3, 4, 5, 6, 7, 8, 9, 10, 13, 14]      # 1 GKP, 5 DEF, 3 MID, 2 FWD
    entries = _squad(xi, [16, 11, 2, 12], minutes={9: 0})
    out = _run(entries, ruled_out={9}, positions=positions)
    assert out["subs"] == [{"out": 9, "in": 11}], "the MID comes on, not a sixth defender"
    counts = {}
    for f in out["xi"]:
        counts[positions[f]] = counts.get(positions[f], 0) + 1
    assert counts["DEF"] <= XI_POSITION_MAXIMUMS["DEF"]


# ---- the bench rule ----------------------------------------------------------
def test_a_bench_player_who_has_not_kicked_off_is_still_the_sub():
    """THE commissioner's rule. He is next in order and can still score, so he takes
    the slot at 0 points rather than being skipped for someone behind him who happens
    to have played already. Skipping him would make the projection thrash as fixtures
    complete."""
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    # Slot 12 hasn't played (0 minutes, NOT ruled out). Slot 13 played and scored 9.
    entries = _squad(xi, [6, 7, 2, 12],
                     minutes={9: 0, 6: 0, 7: 90},
                     points={6: 0, 7: 9})
    out = _run(entries, ruled_out={9})
    assert out["subs"] == [{"out": 9, "in": 6}], "next in order wins, not the one who played"
    assert out["points"] == 0, "he contributes nothing YET"


def test_a_bench_player_who_definitively_blanked_is_skipped():
    """The other half of the same rule: ruled out means passed over. This is what makes
    the projection agree with FPL once every match is finished."""
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    entries = _squad(xi, [6, 7, 2, 12],
                     minutes={9: 0, 6: 0, 7: 90}, points={7: 9})
    out = _run(entries, ruled_out={9, 6})
    assert out["subs"] == [{"out": 9, "in": 7}]
    assert out["points"] == 9


def test_bench_priority_is_the_pick_slot_not_list_order():
    """sync stores the array pre-sorted, but nothing should depend on that."""
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    entries = _squad(xi, [6, 7, 2, 12], minutes={9: 0})
    shuffled = list(reversed(entries))
    assert _run(shuffled, ruled_out={9})["subs"] == [{"out": 9, "in": 6}]


def test_one_bench_player_cannot_cover_two_blanks():
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    entries = _squad(xi, [6, 7, 2, 12], minutes={9: 0, 10: 0})
    out = _run(entries, ruled_out={9, 10})
    assert [s["in"] for s in out["subs"]] == [6, 7], "two different players"


def test_more_blanks_than_usable_bench_covers_what_it_can():
    """Michael's real GW2 situation: four blanks, not enough usable bench. The ones
    that cannot be covered stay in the XI on zero — FPL does not field ten men."""
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15]
    # Bench is 6, 7, 2, 12 — the keeper (2) is ruled out AND illegal anyway, and 12 is
    # ruled out too, leaving exactly two usable.
    entries = _squad(xi, [6, 7, 2, 12],
                     minutes={9: 0, 10: 0, 11: 0, 14: 0, 2: 0, 12: 0},
                     points={6: 2, 7: 3})
    out = _run(entries, ruled_out={9, 10, 11, 14, 2, 12})
    assert len(out["subs"]) == 2, "only two usable bench players"
    assert out["short"] is True
    assert len(out["xi"]) == 11, "still eleven; the uncovered blanks simply score zero"


# ---- resilience --------------------------------------------------------------
def test_a_player_with_no_known_position_is_left_in_place():
    """An unresolvable element id must not be guessed at — the same guard
    players_remaining_by_manager already uses."""
    xi = [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 99]
    entries = _squad(xi, [2, 6, 7, 12], minutes={99: 0})
    out = _run(entries, ruled_out={99})
    assert 99 in out["xi"]


def test_an_empty_squad_is_not_an_error():
    out = _run([])
    assert out == {"xi": [], "subs": [], "points": 0, "short": False}


@pytest.mark.parametrize("pos", sorted(XI_POSITION_MINIMUMS))
def test_every_position_has_a_ceiling_at_or_above_its_floor(pos):
    """Guards a typo in the constants that would make every XI illegal."""
    assert XI_POSITION_MAXIMUMS[pos] >= XI_POSITION_MINIMUMS[pos]
