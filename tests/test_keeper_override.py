"""Commissioner override of derived keeper facts.

Keeper eligibility is derived, never entered, and two rules turn on it: the =<2
waiver-acquired keeper cap and the 4-year clock. `rules.keeper_status` treats ANY
unexplained gap in a manager's tenure as a drop, which relabels the player 'waiver'
AND caps their clock — and 25/26 has no injury-list records at all, so a legitimate
absence is enough to trigger it. The override is how that gets corrected.

The pure-rule cases need no database; the rest use TEST_DATABASE_URL (see conftest).
"""

import pytest

import services
from models import Gameweek, KeeperSeed, League, Manager, Player, Roster
from rules import KEEPER_FRESH_REMAINING, keeper_status, validate_keeper_selection


# ---- pure rule ------------------------------------------------------------
def test_derivation_is_unchanged_when_no_override_is_given():
    """Guard against the override parameter quietly altering normal behaviour."""
    assert keeper_status(True, False, False, None) == ("draft", KEEPER_FRESH_REMAINING)
    assert keeper_status(False, True, False, None) == ("trade", KEEPER_FRESH_REMAINING)
    assert keeper_status(False, False, False, None)[0] == "waiver"
    assert keeper_status(True, False, True, None)[0] == "waiver", "a drop wins"


@pytest.mark.parametrize("value", ["draft", "waiver", "trade"])
def test_override_replaces_the_derived_label(value):
    acq, _ = keeper_status(False, False, True, None, acquisition=value)
    assert acq == value


def test_a_non_waiver_override_lifts_the_waiver_clock_cap():
    """The label and the clock are damaged by the same missing evidence, so
    correcting only the label would leave the player short a keeper year."""
    capped = keeper_status(True, False, True, 4)          # dropped -> waiver
    assert capped == ("waiver", min(4, KEEPER_FRESH_REMAINING))

    fixed = keeper_status(True, False, True, 4, acquisition="draft")
    assert fixed == ("draft", 4), "the clock stayed capped after correction"


def test_a_waiver_override_still_caps_the_clock():
    acq, remaining = keeper_status(True, False, False, 4, acquisition="waiver")
    assert (acq, remaining) == ("waiver", min(4, KEEPER_FRESH_REMAINING))


# ---- the point of the whole feature ---------------------------------------
def _sel(name, acq, eligible=True, discovery=False):
    return {"player": name, "acquisition": acq, "eligible": eligible,
            "is_discovery": discovery}


def test_a_third_waiver_keeper_is_blocked_until_one_is_corrected():
    """The user-facing consequence: a mis-derived 'waiver' costs a keeper slot."""
    over = [_sel("A", "waiver"), _sel("B", "waiver"), _sel("C", "waiver")]
    errors = validate_keeper_selection(over)
    assert any("waiver" in e for e in errors), errors

    # the commissioner corrects C, who was never actually a waiver pickup
    corrected = [_sel("A", "waiver"), _sel("B", "waiver"), _sel("C", "draft")]
    assert validate_keeper_selection(corrected) == []


# ---- DB: storage, derivation, propagation ---------------------------------
def _seed_league(session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="offseason")
    session.add(lg)
    session.flush()
    gw = Gameweek(number=1, league_id=lg.id)
    session.add(gw)
    session.flush()
    return lg, gw


def _roster(session, lg, gw, mgr_name, fpl, player_name, player_fpl):
    m = Manager(league_id=lg.id, fpl_manager_id=fpl, name=mgr_name,
                display_name=mgr_name)
    p = Player(name=player_name, code=player_fpl * 7, fpl_id=player_fpl,
               position="MID", current_team="ARS")
    session.add_all([m, p])
    session.flush()
    session.add(Roster(manager_id=m.id, gameweek_id=gw.id, player_id=p.id))
    session.commit()
    return m, p


def test_seeds_are_keyed_per_manager_not_per_player(test_session):
    """Two managers seeding the same player used to collide — the map was keyed on
    player alone, so one silently won."""
    lg, gw = _seed_league(test_session)
    a, p = _roster(test_session, lg, gw, "A", "1", "Shared", 5)
    b = Manager(league_id=lg.id, fpl_manager_id="2", name="B", display_name="B")
    test_session.add(b)
    test_session.flush()
    test_session.add(Roster(manager_id=b.id, gameweek_id=gw.id, player_id=p.id))
    test_session.add_all([
        KeeperSeed(league_id=lg.id, manager_id=a.id, player_id=p.id,
                   years_remaining=1, season_year=2025),
        KeeperSeed(league_id=lg.id, manager_id=b.id, player_id=p.id,
                   years_remaining=4, season_year=2025),
    ])
    test_session.commit()

    st = services._derive_keeper_status(test_session, lg)
    assert st[a.id][p.id]["years_remaining"] == 1
    assert st[b.id][p.id]["years_remaining"] == 4


def test_override_round_trip_changes_the_derived_output(test_session):
    lg, gw = _seed_league(test_session)
    m, p = _roster(test_session, lg, gw, "A", "1", "Gabriel", 5)

    before = services._derive_keeper_status(test_session, lg)[m.id][p.id]
    assert before["acquisition"] == "draft", before   # on the GW1 roster

    services.set_keeper_override(test_session, lg, fpl_manager_id="1",
                                 player_fpl_id=5, acquisition="waiver")
    after = services._derive_keeper_status(test_session, lg)[m.id][p.id]
    assert after["acquisition"] == "waiver"

    services.clear_keeper_override(test_session, lg, fpl_manager_id="1",
                                   player_fpl_id=5)
    reverted = services._derive_keeper_status(test_session, lg)[m.id][p.id]
    assert reverted["acquisition"] == "draft"


def test_override_is_audited_with_the_derived_value_it_replaced(test_session):
    from models import AuditLog

    lg, gw = _seed_league(test_session)
    _roster(test_session, lg, gw, "A", "1", "Gabriel", 5)
    services.set_keeper_override(test_session, lg, fpl_manager_id="1",
                                 player_fpl_id=5, acquisition="waiver",
                                 years_remaining=1)
    log = (
        test_session.query(AuditLog).filter_by(action="keeper.override")
        .order_by(AuditLog.created_at.desc()).first()
    )
    assert log is not None
    assert log.details["derived"]["acquisition"] == "draft"
    assert log.details["acquisition"] == "waiver"


def test_a_bad_acquisition_value_is_refused(test_session):
    lg, gw = _seed_league(test_session)
    _roster(test_session, lg, gw, "A", "1", "Gabriel", 5)
    with pytest.raises(services.RuleViolation):
        services.set_keeper_override(test_session, lg, fpl_manager_id="1",
                                     player_fpl_id=5, acquisition="nonsense")
    with pytest.raises(services.RuleViolation):
        services.set_keeper_override(test_session, lg, fpl_manager_id="1",
                                     player_fpl_id=5)   # nothing to set


def test_the_override_reaches_the_manager_facing_keeper_screen(test_session):
    """Managers pick against these values, so the correction has to show up there —
    including the data-acq the client-side waiver warning reads."""
    lg, gw = _seed_league(test_session)
    _roster(test_session, lg, gw, "A", "1", "Gabriel", 5)
    services.set_keeper_override(test_session, lg, fpl_manager_id="1",
                                 player_fpl_id=5, acquisition="waiver")

    out = services.keeper_candidates(test_session, lg, "1")
    row = next(r for r in out["players"] if r["player"] == "Gabriel")
    assert row["acquisition"] == "waiver"

    ctx = services.keeper_overrides_context(test_session, lg)
    mgr = next(x for x in ctx["managers"] if x["manager"] == "A")
    entry = next(r for r in mgr["players"] if r["player"] == "Gabriel")
    assert entry["acq_overridden"] is True
    assert mgr["waiver_eligible"] == 1
