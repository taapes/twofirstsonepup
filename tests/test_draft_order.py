"""Draft order: derived from the adjusted standings, overridable, and safe to change.

Three things are being guarded here.

1. Rounds 2+ follow the FINAL standings including commissioner adjustments. A
   post-season deduction changed where a team finished, and the order is a
   consequence of where they finished — but the derivation read the raw synced
   `Standing.rank`, so the standings page and the draft board disagreed.
2. The commissioner can override that order: a base for all of rounds 2+, or a
   single round, or one manager's position within a round.
3. Changing the order must never re-attribute a pick that has already been made.
   `pick_number` is positional, so a reorder shifts what a number means; the board
   used to recompute the owner for every slot, including completed ones.

The pure-ordering cases run without a database; the rest use TEST_DATABASE_URL.
"""

import pytest

import services
from models import (
    DraftPick,
    Gameweek,
    League,
    Manager,
    Player,
    Standing,
    StandingAdjustment,
)
from rules import generate_draft_slots


# ---- pure: override precedence -------------------------------------------
def _rounds(slots):
    out: dict = {}
    for s in slots:
        out.setdefault(s["round"], []).append(s["manager"])
    return out


def test_round_one_uses_the_lottery_and_later_rounds_the_standings():
    slots = generate_draft_slots(["a", "b", "c"], ["c", "b", "a"], {}, picks_per_manager=2)
    r = _rounds(slots)
    assert r[1] == ["a", "b", "c"]
    assert r[2] == ["c", "b", "a"]


def test_base_override_drives_every_round_after_the_first():
    slots = generate_draft_slots(
        ["a", "b", "c"], ["c", "b", "a"], {}, picks_per_manager=3,
        overrides={None: ["b", "a", "c"]},
    )
    r = _rounds(slots)
    assert r[1] == ["a", "b", "c"], "round 1 must keep its own order"
    assert r[2] == ["b", "a", "c"] and r[3] == ["b", "a", "c"]


def test_a_round_override_beats_the_base():
    slots = generate_draft_slots(
        ["a", "b", "c"], ["c", "b", "a"], {}, picks_per_manager=3,
        overrides={None: ["b", "a", "c"], 3: ["c", "a", "b"]},
    )
    r = _rounds(slots)
    assert r[2] == ["b", "a", "c"]
    assert r[3] == ["c", "a", "b"]


def test_keeper_filter_still_drops_managers_from_late_rounds():
    """Overrides must not defeat the rule that a full roster stops picking."""
    slots = generate_draft_slots(
        ["a", "b"], ["b", "a"], {"a": 1}, picks_per_manager=2,
        overrides={None: ["a", "b"]},
    )
    r = _rounds(slots)
    assert r[1] == ["a", "b"]
    assert r[2] == ["b"], "a has 1 keeper so only picks once"


def test_an_override_may_deliberately_give_someone_two_slots():
    """Reassigning a slot is allowed to produce a double pick — the commissioner
    asked for that. It must be applied, not silently normalised away."""
    slots = generate_draft_slots(
        ["a", "b", "c"], ["c", "b", "a"], {}, picks_per_manager=2,
        overrides={2: ["c", "c", "a"]},
    )
    assert _rounds(slots)[2] == ["c", "c", "a"]


# ---- DB: derivation, overrides, attribution -------------------------------
def _seed(session, totals):
    """A league with managers and standings. `totals` is [(name, total, rank)]."""
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="offseason")
    session.add(lg)
    session.flush()
    mgrs = {}
    for i, (name, total, rank) in enumerate(totals, start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name,
                    display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=rank,
                             total=total, points_for=total * 10))
        mgrs[name] = m
    session.commit()
    return lg, mgrs


def test_reverse_standings_reflects_a_commissioner_adjustment(test_session):
    """The reported bug. Steve finished on 56 and Tucker on 54, so Tucker picked
    first; a -3 deduction puts Steve on 53 and they should swap."""
    lg, m = _seed(test_session, [("Steve", 56, 1), ("Tucker", 54, 2)])
    before = [x.display for x in services._reverse_standings_managers(test_session, lg)]
    assert before == ["Tucker", "Steve"]

    test_session.add(StandingAdjustment(
        league_id=lg.id, manager_id=m["Steve"].id, total_delta=-3,
        points_for_delta=-6, gameweek=30, note="illegal player",
    ))
    test_session.commit()

    after = [x.display for x in services._reverse_standings_managers(test_session, lg)]
    assert after == ["Steve", "Tucker"], "the deduction did not reach the draft order"


def test_set_and_clear_an_override_round_trip(test_session):
    lg, m = _seed(test_session, [("A", 60, 1), ("B", 50, 2), ("C", 40, 3)])
    derived = [x["name"] for x in
               services.draft_order_context(test_session, lg, 2026)["base"]]
    assert derived == ["C", "B", "A"]

    services.set_draft_order_override(test_session, lg, 2026, ["1", "2", "3"])
    ctx = services.draft_order_context(test_session, lg, 2026)
    assert [x["name"] for x in ctx["base"]] == ["A", "B", "C"]
    assert ctx["base_overridden"] is True

    services.clear_draft_order_override(test_session, lg, 2026)
    ctx = services.draft_order_context(test_session, lg, 2026)
    assert [x["name"] for x in ctx["base"]] == ["C", "B", "A"]
    assert ctx["base_overridden"] is False


def test_override_changes_the_board_for_later_rounds_only(test_session):
    lg, m = _seed(test_session, [("A", 60, 1), ("B", 50, 2), ("C", 40, 3)])
    services.set_draft_order(test_session, lg, ["1", "2", "3"])   # R1: A, B, C
    services.set_draft_order_override(test_session, lg, 2026, ["2", "1", "3"])

    board = services.get_draft_board(test_session, lg, 2026)
    by_round: dict = {}
    for b in board:
        by_round.setdefault(b["round"], []).append(b["owner"])
    assert by_round[1] == ["A", "B", "C"], "round 1 must be untouched"
    assert by_round[2] == ["B", "A", "C"]


def test_round_one_order_cannot_be_set_through_the_override(test_session):
    lg, _ = _seed(test_session, [("A", 60, 1), ("B", 50, 2)])
    with pytest.raises(services.RuleViolation):
        services.set_draft_order_override(test_session, lg, 2026, ["1", "2"], round=1)


def test_a_completed_pick_keeps_the_manager_who_made_it(test_session):
    """The dangerous path. pick_number is positional, so reordering changes which
    manager a number maps to — a selection already made must not follow it."""
    lg, m = _seed(test_session, [("A", 60, 1), ("B", 50, 2)])
    services.set_draft_order(test_session, lg, ["1", "2"])       # R1: A, B
    p = Player(name="Gabriel", code=111, fpl_id=5, position="DEF")
    test_session.add(p)
    test_session.commit()

    services.record_pick(test_session, lg, season_year=2026, pick_number=1,
                         owner_fpl="1", player_fpl_id=5, round=1)
    assert services.get_draft_board(test_session, lg, 2026)[0]["owner"] == "A"

    # now flip round 1 so pick 1 would compute to B
    services.set_draft_order(test_session, lg, ["2", "1"])
    slot = services.get_draft_board(test_session, lg, 2026)[0]
    assert slot["owner"] == "A", "a completed pick was re-attributed by a reorder"
    assert slot["player"] == "Gabriel"
    assert slot["reassigned"] is True, "the disagreement should be surfaced"


def test_pick_counts_surface_a_double_slot(test_session):
    lg, m = _seed(test_session, [("A", 60, 1), ("B", 50, 2), ("C", 40, 3)])
    services.set_draft_order(test_session, lg, ["1", "2", "3"])
    services.set_draft_order_override(test_session, lg, 2026, ["2", "2", "3"], round=2)

    counts = dict(services.draft_order_context(test_session, lg, 2026)["counts"])
    assert counts["B"] > counts["C"], f"double slot not reflected: {counts}"
