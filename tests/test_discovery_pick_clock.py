"""The discovery draft's per-pick clock, wired to real data — services.py + the route.

Complements tests/test_discovery_clock.py (the pure rules.discovery_clock chain) and
tests/test_discovery_open_early.py (opening the window). Runs against
TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime as dt

import pytest

import services
from models import DraftPick, Gameweek, League, Manager, Standing
from rules import DISCOVERY_PICK_CLOCK_HOURS, RuleViolation

SEASON = 2026
CLOCK = dt.timedelta(hours=DISCOVERY_PICK_CLOCK_HOURS)


def _seed(session, *, anchor_at=None, anchor_pick=1):
    """Three managers. Reverse standings puts C first: R1 C,B,A / R2 A,B,C — 6 picks."""
    lg = League(fpl_league_id="1", name="S", season_year=SEASON, is_current=True,
                sync_locked=False, phase="in_season",
                discovery_open=True,
                discovery_clock_anchor_pick=anchor_pick,
                discovery_clock_anchor_at=anchor_at)
    session.add(lg)
    session.flush()
    session.add(Gameweek(number=1, league_id=lg.id))
    session.flush()
    mgrs = {}
    for i, name in enumerate(["A", "B", "C"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=f"Team{name}",
                    display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i,
                             total=30 - i, points_for=300 - i))
        mgrs[name] = m
    session.commit()
    return lg, mgrs


# ---- discovery_clock_status: wired to real data --------------------------------
def test_the_clock_never_started_reports_nothing_on_the_clock(test_session):
    """No anchor at all (the window's never opened) is a clean 'nothing to show',
    not a crash."""
    lg, _m = _seed(test_session, anchor_at=None)
    assert services.discovery_clock_status(test_session, lg, SEASON) == {
        "pick": None, "deadline": None, "missed": []}


def test_pick_one_is_on_the_clock_right_after_the_window_opens(test_session):
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    status = services.discovery_clock_status(test_session, lg, SEASON)
    assert status["pick"] == 1 and status["missed"] == []


def test_an_expired_first_pick_advances_without_being_filled(test_session):
    """Rule 4 end to end: real board, real anchor, nobody picked."""
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc) - CLOCK
                                          - dt.timedelta(hours=1))
    status = services.discovery_clock_status(test_session, lg, SEASON)
    assert status["pick"] == 2 and status["missed"] == [1]


def test_a_real_pick_advances_the_clock_immediately(test_session):
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2))
    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Some Kid", round=1)
    status = services.discovery_clock_status(test_session, lg, SEASON)
    assert status["pick"] == 2 and status["missed"] == []


# ---- reset_discovery_clock: the mid-draft bootstrap lever ----------------------
def test_reset_discovery_clock_restarts_at_the_given_pick(test_session):
    """The exact tool the 2026 rollout uses: pick 1 (C) already made under no clock
    rules; restart the clock fresh at pick 2 (B), right now."""
    lg, m = _seed(test_session, anchor_at=None)
    test_session.add(DraftPick(league_id=lg.id, season_year=SEASON,
                               draft_type="discovery", round=1, pick_number=1,
                               manager_id=m["C"].id, player_label="Julian Alvarez",
                               source="discovery"))  # no picked_at — pre-clock history
    test_session.commit()

    r = services.reset_discovery_clock(test_session, lg, season_year=SEASON, pick_number=2)
    assert r["anchor_pick"] == 2

    status = services.discovery_clock_status(test_session, lg, SEASON)
    assert status["pick"] == 2, "pick 1 must be invisible to the clock, not 'missed'"
    assert status["missed"] == []
    remaining = status["deadline"] - dt.datetime.now(dt.timezone.utc)
    assert CLOCK - dt.timedelta(minutes=1) < remaining <= CLOCK, \
        "a fresh full 24h window, not backdated to pick 1's timing"


def test_reset_discovery_clock_refuses_pick_zero(test_session):
    lg, _m = _seed(test_session)
    with pytest.raises(RuleViolation, match=">= 1"):
        services.reset_discovery_clock(test_session, lg, season_year=SEASON, pick_number=0)


# ---- the catch-up pick route: rule 5 -------------------------------------------
@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app, follow_redirects=False)


def _login_manager(client, session, manager, password="pw"):
    from auth import hash_password

    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text


def test_a_manager_can_fill_a_missed_slot_after_the_draft_moved_on(
        test_session, client):
    """The concrete point of rule 5. C (pick 1) never picks; the clock expires and
    moves to B (pick 2). C must still be able to submit pick 1 — the draft moving on
    does not forfeit it."""
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc) - CLOCK
                                          - dt.timedelta(hours=1))
    _login_manager(client, test_session, m["C"])

    r = client.post(f"/discovery/{SEASON}/pick",
                    data={"player_name": "Late Arrival", "pick_number": "1"})
    assert r.status_code == 200, r.text
    pick = test_session.query(DraftPick).filter_by(pick_number=1).one()
    assert pick.player_label == "Late Arrival"
    assert pick.picked_at is not None


def test_a_manager_cannot_fill_someone_elses_missed_slot(test_session, client):
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc) - CLOCK
                                          - dt.timedelta(hours=1))
    _login_manager(client, test_session, m["B"])  # B is on the clock (pick 2), not C

    r = client.post(f"/discovery/{SEASON}/pick",
                    data={"player_name": "Not Yours", "pick_number": "1"})
    assert r.status_code == 403
    assert test_session.query(DraftPick).count() == 0


def test_omitting_pick_number_defaults_to_the_managers_own_earliest_open_slot(
        test_session, client):
    """The common case needs no form change: a manager with exactly one open slot
    (the ordinary on-time pick) doesn't have to name it."""
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    _login_manager(client, test_session, m["C"])  # C is on the clock, pick 1

    r = client.post(f"/discovery/{SEASON}/pick", data={"player_name": "On Time"})
    assert r.status_code == 200, r.text
    assert test_session.query(DraftPick).filter_by(pick_number=1).one().player_label \
        == "On Time"


def test_a_manager_can_explicitly_target_their_missed_slot_by_number(
        test_session, client):
    """The route accepts an explicit pick_number rather than always resolving to
    'the' single open slot — the change rule 5 actually required. C's pick 1 has
    been missed and the clock has moved to B (pick 2); C names pick 1 directly."""
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc) - CLOCK
                                          - dt.timedelta(hours=1))
    _login_manager(client, test_session, m["C"])

    r = client.post(f"/discovery/{SEASON}/pick",
                    data={"player_name": "Chosen", "pick_number": "1"})
    assert r.status_code == 200, r.text
    assert test_session.query(DraftPick).filter_by(pick_number=1).one().player_label \
        == "Chosen"


def test_a_manager_who_holds_both_the_current_and_a_missed_slot_sees_both(
        test_session, client):
    """The genuine two-open-slots case, and it falls straight out of a snake draft:
    with R1 order C,B,A and R2 reversed to A,B,C, A holds pick 3 (last of round 1)
    AND pick 4 (first of round 2) — adjacent. Expire picks 1, 2 and 3 (nobody home);
    the clock lands on pick 4, which is ALSO A's — so A now holds pick 3 (missed)
    and pick 4 (current) at once."""
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc)
                                          - 3 * CLOCK - dt.timedelta(hours=1))
    status = services.discovery_clock_status(test_session, lg, SEASON)
    assert status["pick"] == 4 and status["missed"] == [1, 2, 3]

    _login_manager(client, test_session, m["A"])
    r = client.get(f"/discovery/{SEASON}")
    assert r.status_code == 200
    assert "pick 3" in r.text and "pick 4" in r.text


# ---- overwrite must not re-stamp picked_at -------------------------------------
def test_an_admin_overwrite_does_not_move_when_the_pick_originally_landed(
        test_session):
    """A correction of WHAT was picked must not retroactively move WHEN the baton
    passed — the next slot's clock already started at the original picked_at."""
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5))
    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="First Name", round=1)
    original = test_session.query(DraftPick).filter_by(pick_number=1).one().picked_at

    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Corrected Name", round=1,
        overwrite=True)
    corrected = test_session.query(DraftPick).filter_by(pick_number=1).one()
    assert corrected.player_label == "Corrected Name"
    assert corrected.picked_at == original
