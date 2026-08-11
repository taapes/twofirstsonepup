"""Season-end payouts follow the ADJUSTED standings.

A commissioner deduction changes where a team finished, and 1st/2nd/3rd/last are a
consequence of where they finished. The standings page and the draft order already
read `get_standings`; the money did not — it sorted the raw synced `Standing.rank`,
so `home.html` could name one manager champion in the standings table and pay another
in the winnings table directly below it.

Four slots turn on finishing position, and first place also collects the entire fines
pool — so a change at the top moves considerably more than its 40%.

Every fixture here deliberately DISAGREES with the raw ranks on the money slots.
Agreeing fixtures pass under either implementation and prove nothing.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import Fine, League, Manager, Standing, StandingAdjustment
from rules import PAYOUT_STRUCTURE

# The user-visible contract. Note the EM DASH in the league labels (rules._PAYOUT_LABELS)
# — hardcoded rather than imported so a rename fails loudly here.
FIRST = "1st place — League"
SECOND = "2nd place — League"
THIRD = "3rd place — League"
FINE = "Last-place fine"
COLLECTED = "Fines collected"


def _seed(session, rows):
    """rows: [(name, total, points_for, raw_rank)] -> (league, {name: Manager})."""
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="offseason")
    session.add(lg)
    session.flush()
    mgrs = {}
    for i, (name, total, pf, rank) in enumerate(rows, start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=rank,
                             total=total, points_for=pf))
        mgrs[name] = m
    session.commit()
    return lg, mgrs


def _adjust(session, lg, manager, total_delta):
    session.add(StandingAdjustment(league_id=lg.id, manager_id=manager.id,
                                   total_delta=total_delta, points_for_delta=0))
    session.commit()


# The load-bearing fixture: raw rank says Ann/Ben/Cal on the podium and Fay last;
# adjusted says Ben/Cal/Ann and Eve last. No slot overlaps, so reverting the fix fails
# every adjusted assertion rather than one incidental one.
DISAGREEING = [
    ("Ann", 60, 600, 1),
    ("Ben", 58, 580, 2),
    ("Cal", 56, 560, 3),
    ("Dee", 40, 400, 4),
    ("Eve", 38, 380, 5),
    ("Fay", 30, 300, 6),
]


def _disagreeing(session):
    lg, mgrs = _seed(session, DISAGREEING)
    _adjust(session, lg, mgrs["Ann"], -5)    # 60 -> 55, drops to 3rd
    _adjust(session, lg, mgrs["Eve"], -10)   # 38 -> 28, drops to last
    return lg, mgrs


def _lines(payouts, name):
    """{label: amount} for one manager. Asserting on `total` is useless — every
    manager carries a -42.18 weekly-pool entry line, so the totals are all polluted."""
    return {
        b["label"]: b["amount"]
        for p in payouts["payouts"] if p["manager"] == name
        for b in p["breakdown"]
    }


def _holder(payouts, label):
    return next((p["manager"] for p in payouts["payouts"]
                 if any(b["label"] == label for b in p["breakdown"])), None)


# ---- the fix --------------------------------------------------------------
def test_without_adjustments_the_slots_follow_the_synced_order(test_session):
    """The guard that this is a fix, not a rewrite — must hold under either
    implementation."""
    lg, _ = _seed(test_session, DISAGREEING)
    p = services.get_payouts(test_session, lg)
    assert _holder(p, FIRST) == "Ann"
    assert _holder(p, SECOND) == "Ben"
    assert _holder(p, THIRD) == "Cal"
    assert _holder(p, FINE) == "Fay"


def test_a_deduction_moves_the_league_winner(test_session):
    lg, _ = _disagreeing(test_session)
    p = services.get_payouts(test_session, lg)
    pot = PAYOUT_STRUCTURE["entry_fee"] * 6
    assert _lines(p, "Ben")[FIRST] == round(pot * 0.40, 2)
    assert FIRST not in _lines(p, "Ann"), "the deducted manager still took the title"


def test_a_deduction_moves_second_and_third(test_session):
    lg, _ = _disagreeing(test_session)
    p = services.get_payouts(test_session, lg)
    pot = PAYOUT_STRUCTURE["entry_fee"] * 6
    assert _lines(p, "Cal")[SECOND] == round(pot * 0.15, 2)
    assert _lines(p, "Ann")[THIRD] == round(pot * 0.05, 2)
    assert SECOND not in _lines(p, "Ben"), "Ben moved up but kept the 2nd-place money"


def test_the_last_place_fine_follows_the_adjustment(test_session):
    lg, _ = _disagreeing(test_session)
    p = services.get_payouts(test_session, lg)
    assert _lines(p, "Eve")[FINE] == -float(PAYOUT_STRUCTURE["last_place_fine"])
    assert FINE not in _lines(p, "Fay"), "the wrong manager was fined $125"


def test_the_fines_pool_follows_first_place(test_session):
    """Why a change at the top matters more than 40%: the winner also sweeps the fines."""
    lg, mgrs = _disagreeing(test_session)
    test_session.add(Fine(league_id=lg.id, manager_id=mgrs["Dee"].id, amount=40,
                          reason="late lineup"))
    test_session.commit()

    p = services.get_payouts(test_session, lg)
    assert _lines(p, "Ben")[COLLECTED] == 165.0   # 125 last-place + 40
    assert COLLECTED not in _lines(p, "Ann")
    assert _lines(p, "Dee")["Fine(s)"] == -40.0


def test_the_money_matches_the_standings_page(test_session):
    """The invariant the bug broke: both tables render on the same page, so they must
    name the same people. Non-vacuous only because this fixture disagrees with raw rank."""
    lg, _ = _disagreeing(test_session)
    standings = services.get_standings(test_session, lg)
    p = services.get_payouts(test_session, lg)
    assert _holder(p, FIRST) == standings[0]["manager"]
    assert _holder(p, FINE) == standings[-1]["manager"]


# ---- edges ----------------------------------------------------------------
def test_a_manager_with_no_standing_row_is_neither_first_nor_last(test_session):
    """Fining someone $125 because their sync row is missing would be a data bug
    paying out as money. They still count toward the pot and owe their buy-in."""
    lg, _ = _seed(test_session, [("Ann", 60, 600, 1), ("Ben", 40, 400, 2)])
    gus = Manager(league_id=lg.id, fpl_manager_id="99", name="Gus", display_name="Gus")
    test_session.add(gus)
    test_session.commit()

    p = services.get_payouts(test_session, lg)
    assert _holder(p, FIRST) == "Ann"
    assert _holder(p, FINE) == "Ben", "an unranked manager was treated as last"
    assert FINE not in _lines(p, "Gus")
    assert FIRST not in _lines(p, "Gus")
    assert p["num_managers"] == 3
    assert p["base_pot"] == PAYOUT_STRUCTURE["entry_fee"] * 3


def test_an_exact_tie_at_the_bottom_breaks_alphabetically(test_session):
    """Deterministic, and the same rule as the standings page and the draft order. The
    raw ranks are deliberately inverted so this also fails under raw-rank resolution."""
    lg, _ = _seed(test_session, [
        ("Top", 50, 500, 1),
        ("Zed", 10, 100, 2),   # raw rank says Ann is last...
        ("Ann", 10, 100, 3),   # ...adjusted tie-break by name says Zed is
    ])
    p = services.get_payouts(test_session, lg)
    assert _holder(p, FINE) == "Zed"


def test_fewer_than_three_managers_skips_the_third_place_slot(test_session):
    lg, _ = _seed(test_session, [("Ann", 60, 600, 1), ("Ben", 40, 400, 2)])
    p = services.get_payouts(test_session, lg)
    assert _holder(p, FIRST) == "Ann"
    assert _holder(p, SECOND) == "Ben"
    assert _holder(p, THIRD) is None
    assert _holder(p, FINE) == "Ben"


def test_a_league_with_no_standings_pays_no_position_slots(test_session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="offseason")
    test_session.add(lg)
    test_session.flush()
    for i, name in enumerate(["Ann", "Ben", "Cal"], start=1):
        test_session.add(Manager(league_id=lg.id, fpl_manager_id=str(i), name=name,
                                 display_name=name))
    test_session.commit()

    p = services.get_payouts(test_session, lg)
    for label in (FIRST, SECOND, THIRD, FINE):
        assert _holder(p, label) is None, f"{label} was paid with no standings"
