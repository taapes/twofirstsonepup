"""The goalie-team rule is a per-season switch, and it starts OFF.

From 2026 a manager drafts one Premier League club instead of two goalkeepers, so a
draft is 13 outfielders + 1 goalie team = 14 picks. The switch has to be per-season
because `services.get_draft_board` regenerates its slot list on EVERY read with no
season parameter: a global 15 -> 14 would retroactively truncate every archived board.
These tests pin that, and the arithmetic behind 14.

Pure — no database.
"""

import pytest

from rules import (
    GOALIE_TEAM_MODES,
    GOALIE_TEAM_SLOTS,
    OUTFIELD_POSITION_LIMITS,
    OUTFIELD_SQUAD_SIZE,
    OUTFIELD_XI_MINIMUMS,
    ROSTER_SIZE,
    SQUAD_POSITION_LIMITS,
    XI_POSITION_MINIMUMS,
    draft_picks_per_manager,
    generate_draft_slots,
    goalie_team_keepable,
    goalie_teams_on,
)


# ---- the arithmetic ----
def test_the_outfield_shape_is_the_fpl_shape_minus_the_keepers():
    """Nothing about outfielders changes — the goalkeeper pair collapses into one
    club slot and the other thirteen are untouched. If this ever fails, someone has
    redefined a squad, not just its goalkeepers."""
    assert OUTFIELD_POSITION_LIMITS == {
        p: SQUAD_POSITION_LIMITS[p] for p in ("DEF", "MID", "FWD")
    }
    assert OUTFIELD_XI_MINIMUMS == {
        p: XI_POSITION_MINIMUMS[p] for p in ("DEF", "MID", "FWD")
    }
    assert OUTFIELD_SQUAD_SIZE == 13
    assert OUTFIELD_SQUAD_SIZE + SQUAD_POSITION_LIMITS["GKP"] == ROSTER_SIZE


def test_thirteen_outfielders_plus_one_club_is_fourteen_picks():
    assert OUTFIELD_SQUAD_SIZE + GOALIE_TEAM_SLOTS == 14
    assert draft_picks_per_manager("redraft") == 14
    assert draft_picks_per_manager("keeper") == 14


def test_the_rule_off_is_the_old_fifteen_pick_draft():
    """The archive's guarantee. `off` must be indistinguishable from before."""
    assert draft_picks_per_manager("off") == ROSTER_SIZE
    assert draft_picks_per_manager(None) == ROSTER_SIZE
    assert draft_picks_per_manager("") == ROSTER_SIZE


# ---- the modes ----
@pytest.mark.parametrize("mode,on", [("off", False), ("redraft", True), ("keeper", True)])
def test_which_modes_turn_the_rule_on(mode, on):
    assert goalie_teams_on(mode) is on


@pytest.mark.parametrize(
    "mode,keepable", [("off", False), ("redraft", False), ("keeper", True)]
)
def test_only_keeper_mode_lets_a_club_be_kept(mode, keepable):
    """`redraft` is the difference that matters: everyone drafts a club afresh every
    year, so no club ever needs a keeper clock."""
    assert goalie_team_keepable(mode) is keepable


def test_an_unknown_mode_is_treated_as_off():
    """Fail safe. A typo in the column must not silently start issuing 14-pick
    boards for a finished season."""
    assert goalie_teams_on("kepeer") is False
    assert draft_picks_per_manager("kepeer") == ROSTER_SIZE


def test_every_mode_is_declared():
    assert set(GOALIE_TEAM_MODES) == {"off", "redraft", "keeper"}


# ---- the board it produces ----
def test_a_goalie_team_league_drafts_fourteen_rounds_not_fifteen():
    r1 = rev = ["A", "B"]
    slots = generate_draft_slots(
        r1, rev, {}, picks_per_manager=draft_picks_per_manager("redraft")
    )
    assert max(s["round"] for s in slots) == 14
    assert sum(1 for s in slots if s["manager"] == "A") == 14


def test_the_same_league_with_the_rule_off_still_drafts_fifteen():
    r1 = rev = ["A", "B"]
    slots = generate_draft_slots(
        r1, rev, {}, picks_per_manager=draft_picks_per_manager("off")
    )
    assert max(s["round"] for s in slots) == 15


def test_keepers_still_come_off_the_top():
    """Keepers are free under either rule — five keepers means nine picks, not ten."""
    slots = generate_draft_slots(
        ["A"], ["A"], {"A": 5}, picks_per_manager=draft_picks_per_manager("redraft")
    )
    assert len(slots) == 9
