"""Anti-tanking counts only the zeros a manager is answerable for.

The rule fires on >=3 rostered players at 0 real-life minutes for >=3 straight
gameweeks. Counting all 15 picks flatly produced false positives from three
structural causes the manager does not control:

  * a club with no fixture that gameweek (a blank GW)
  * a player parked on the injury or international list
  * the spare goalkeeper — a squad must carry two and only one can start

Every fixture here sits a squad at exactly 3 raw zeros for 3 straight gameweeks: the
flag trips under the old flat count and must NOT trip once the excuse is applied. A
fixture at 4 or 5 zeros would pass either way and prove nothing.

Also covers the other half: a dismissed flag has to disappear from ALL the surfaces,
not just the homepage list.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    Fixture,
    Gameweek,
    GameweekPoints,
    InjuryList,
    InternationalList,
    League,
    Manager,
    Player,
    PlayerSeason,
)

GWS = (10, 11, 12)  # three consecutive gameweeks -> exactly the window length


def _league(session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="in_season")
    session.add(lg)
    session.flush()
    gws = {}
    for n in GWS:
        gw = Gameweek(league_id=lg.id, number=n)
        session.add(gw)
        session.flush()
        gws[n] = gw
    m = Manager(league_id=lg.id, fpl_manager_id="1", name="Ann", display_name="Ann")
    session.add(m)
    session.flush()
    return lg, m, gws


def _player(session, lg, fpl_id, name, position, team):
    """A player in the global pool plus this season's snapshot (position and club are
    read off the snapshot, since FPL recycles element ids every August)."""
    p = Player(code=900000 + fpl_id, fpl_id=fpl_id, name=name,
               position=position, current_team=team)
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id,
                             name=name, position=position, current_team=team))
    return p


def _squad(session, lg):
    """15 players: 2 GKs, the rest outfield. ids 1..15, clubs spread over 4 sides."""
    out = {}
    for i in range(1, 16):
        pos = "GKP" if i <= 2 else "MID"
        team = ["AVL", "BHA", "CHE", "EVE"][i % 4]
        out[i] = _player(session, lg, i, f"P{i}", pos, team)
    return out


def _points(session, gws, mgr, zero_ids):
    """Same squad every GW; `zero_ids` post 0 minutes, everyone else plays 90."""
    for n in GWS:
        session.add(GameweekPoints(
            manager_id=mgr.id, gameweek_id=gws[n].id, total_points=50,
            player_points=[
                {"fpl_id": i, "position": i, "is_starting": i <= 11,
                 "minutes": 0 if i in zero_ids else 90, "points": 0}
                for i in range(1, 16)
            ],
        ))


def _fixtures(session, lg, teams_playing, gws=GWS):
    """One fixture per GW per club in `teams_playing` (paired up); anyone missing is
    blank that week."""
    fid = 0
    for n in gws:
        pairs = list(teams_playing)
        for a, b in zip(pairs[::2], pairs[1::2]):
            fid += 1
            session.add(Fixture(league_id=lg.id, fpl_fixture_id=fid, event=n,
                                home_team=a, away_team=b, finished=True))


def _counts(session, lg, mgr):
    return services._tanking_counts_by_manager(session, lg)[mgr.id]["counts"]


def _flagged(session, lg):
    return {f["manager"] for f in services.get_flags(session, lg)}


ALL_TEAMS = ["AVL", "BHA", "CHE", "EVE"]


# ---- the baseline: the rule still fires -----------------------------------
def test_three_unexcused_zeros_for_three_weeks_is_still_flagged(test_session):
    """The guard that this is a fix, not a switch-off. Three outfielders at zero,
    every club playing, nobody on a list — that is the violation."""
    lg, mgr, gws = _league(test_session)
    _squad(test_session, lg)
    _points(test_session, gws, mgr, zero_ids={3, 4, 5})
    _fixtures(test_session, lg, ALL_TEAMS)
    test_session.commit()

    assert _counts(test_session, lg, mgr) == {10: 3, 11: 3, 12: 3}
    assert _flagged(test_session, lg) == {"Ann"}


# ---- the goalkeeper allowance ---------------------------------------------
def test_the_spare_goalkeeper_does_not_count(test_session):
    """Two outfielders and the backup keeper at zero is 2, not 3 — no flag."""
    lg, mgr, gws = _league(test_session)
    _squad(test_session, lg)
    _points(test_session, gws, mgr, zero_ids={1, 3, 4})  # id 1 is a GK
    _fixtures(test_session, lg, ALL_TEAMS)
    test_session.commit()

    assert _counts(test_session, lg, mgr) == {10: 2, 11: 2, 12: 2}
    assert _flagged(test_session, lg) == set()


def test_both_goalkeepers_at_zero_count(test_session):
    """Every club fields a keeper, so two at zero means neither is starting anywhere.
    The allowance is all-or-nothing: both count, and with one outfielder that is 3."""
    lg, mgr, gws = _league(test_session)
    _squad(test_session, lg)
    _points(test_session, gws, mgr, zero_ids={1, 2, 3})  # ids 1,2 are the GKs
    _fixtures(test_session, lg, ALL_TEAMS)
    test_session.commit()

    assert _counts(test_session, lg, mgr) == {10: 3, 11: 3, 12: 3}
    assert _flagged(test_session, lg) == {"Ann"}


# ---- blank gameweeks -------------------------------------------------------
def test_a_club_with_no_fixture_does_not_count(test_session):
    """EVE is blank all three weeks, so its players' zeros drop out."""
    lg, mgr, gws = _league(test_session)
    _squad(test_session, lg)
    # ids 3 and 7 are EVE (i % 4 == 3) and blank; id 5 is BHA and playing, so one
    # unexcused zero is left out of three
    _points(test_session, gws, mgr, zero_ids={3, 7, 5})
    _fixtures(test_session, lg, ["AVL", "BHA"])  # CHE and EVE blank
    test_session.commit()

    assert _counts(test_session, lg, mgr) == {10: 1, 11: 1, 12: 1}
    assert _flagged(test_session, lg) == set()


def test_a_gameweek_with_no_fixtures_at_all_excuses_nobody(test_session):
    """Missing fixture data is missing data, not twenty blank clubs — excusing
    everyone there would silently disable the rule for every historical season."""
    lg, mgr, gws = _league(test_session)
    _squad(test_session, lg)
    _points(test_session, gws, mgr, zero_ids={3, 4, 5})
    test_session.commit()  # no Fixture rows at all

    assert _counts(test_session, lg, mgr) == {10: 3, 11: 3, 12: 3}
    assert _flagged(test_session, lg) == {"Ann"}


# ---- injury list / international duty --------------------------------------
@pytest.mark.parametrize("model, extra", [
    (InjuryList, {}),
    (InternationalList, {"tournament": "AFCON"}),
])
def test_a_covered_absence_does_not_count(test_session, model, extra):
    """Both lists preserve keeper eligibility already; neither absence is the
    manager's doing, so neither counts here."""
    lg, mgr, gws = _league(test_session)
    squad = _squad(test_session, lg)
    _points(test_session, gws, mgr, zero_ids={3, 4, 5})
    _fixtures(test_session, lg, ALL_TEAMS)
    test_session.add(model(player_id=squad[3].id, manager_id=mgr.id,
                           start_gw=10, end_gw=None, status="active", **extra))
    test_session.commit()

    assert _counts(test_session, lg, mgr) == {10: 2, 11: 2, 12: 2}
    assert _flagged(test_session, lg) == set()


def test_coverage_only_excuses_the_gameweeks_it_spans(test_session):
    """A closed entry stops excusing once it ends — GW12 goes back to 3 and, being a
    single week, is short of the window."""
    lg, mgr, gws = _league(test_session)
    squad = _squad(test_session, lg)
    _points(test_session, gws, mgr, zero_ids={3, 4, 5})
    _fixtures(test_session, lg, ALL_TEAMS)
    test_session.add(InjuryList(player_id=squad[3].id, manager_id=mgr.id,
                                start_gw=10, end_gw=11, status="returned"))
    test_session.commit()

    assert _counts(test_session, lg, mgr) == {10: 2, 11: 2, 12: 3}
    assert _flagged(test_session, lg) == set()


# ---- a dismissed flag disappears everywhere --------------------------------
def test_clearing_a_flag_clears_it_from_every_surface(test_session):
    """get_flags keeps cleared windows so admin can restore them. My Team and the
    homepage Flagged Actions table must not still call the manager flagged — they
    render on the same page as the list showing it dismissed."""
    lg, mgr, gws = _league(test_session)
    _squad(test_session, lg)
    _points(test_session, gws, mgr, zero_ids={3, 4, 5})
    _fixtures(test_session, lg, ALL_TEAMS)
    test_session.commit()

    window = services._window_label([10, 11, 12])
    before = services._manager_status(test_session, lg, mgr)["tanking"]["state"]
    assert before == "flagged"

    services.clear_flag(test_session, lg, mgr.fpl_manager_id, window)

    after = services._manager_status(test_session, lg, mgr)["tanking"]["state"]
    assert after != "flagged", "My Team still shows a dismissed flag"
    tanking = [a for a in services.flagged_actions(test_session, lg)
               if a["category"] == "Anti-tanking"]
    assert not any("flagged" in a["detail"] for a in tanking), \
        "Flagged Actions still lists a dismissed flag"
    # admin's own list keeps it, marked cleared
    windows = services.get_flags(test_session, lg)[0]["windows"]
    assert [(w["label"], w["cleared"]) for w in windows] == [(window, True)]


def test_restoring_a_flag_brings_it_back(test_session):
    lg, mgr, gws = _league(test_session)
    _squad(test_session, lg)
    _points(test_session, gws, mgr, zero_ids={3, 4, 5})
    _fixtures(test_session, lg, ALL_TEAMS)
    test_session.commit()

    window = services._window_label([10, 11, 12])
    services.clear_flag(test_session, lg, mgr.fpl_manager_id, window)
    services.restore_flag(test_session, lg, mgr.fpl_manager_id, window)

    assert services._manager_status(test_session, lg, mgr)["tanking"]["state"] == "flagged"
