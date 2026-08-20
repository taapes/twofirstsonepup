"""The rollover assertions on /admin/health.

Every one of these would have caught the 26/27 breakage on day one instead of a day
later by accident. `advance_season` pairs managers on `fpl_manager_id`, FPL reissued
every id at that rollover, and each carry `continue`s on a miss — so identity,
logins and keeper clocks were all dropped while the rollover reported success and
wrote `managers_carried=0, keepers_seeded=0` to an audit log nobody read.

So these check the observable CONSEQUENCES rather than the carry itself: can people
log in, do the clocks exist, is the draft on the right row, is the cron syncing this
season. A check that reads the same field the broken code reads would have been just
as silent.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime

import pytest

import services
from models import (
    DraftPick,
    Gameweek,
    KeeperSeed,
    KeeperSelection,
    League,
    Manager,
    Player,
    Standing,
    SyncLog,
)

PEOPLE = ["A", "B", "C"]
_FPL = [0]


@pytest.fixture(autouse=True)
def _reset_ids():
    _FPL[0] = 700
    yield


def _league(session, *, season_year, fpl, is_current=False, locked=False,
            phase="preseason"):
    lg = League(fpl_league_id=str(fpl), name=f"S{season_year}",
                season_year=season_year, is_current=is_current,
                sync_locked=locked, phase=phase, goalie_team_mode="off")
    session.add(lg)
    session.flush()
    return lg


def _managers(session, lg, *, with_password=True, with_display=True):
    out = {}
    for i, name in enumerate(PEOPLE, start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=f"{name} FC",
                    display_name=name if with_display else None,
                    password_hash="pbkdf2$x" if with_password else None)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i,
                             total=100 - i, points_for=1000 - i))
        out[name] = m
    session.commit()
    return out


def _player(session, name):
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=name, code=fid * 7, fpl_id=fid, position="MID",
               current_team="ARS")
    session.add(p)
    session.commit()
    return p


def _rolled_over(session, *, new_password=True, new_display=True):
    """Old row with a completed keeper submission, new row current — the shape the
    rollover leaves behind."""
    old = _league(session, season_year=2025, fpl=1754, locked=True)
    old_m = _managers(session, old)
    new = _league(session, season_year=2026, fpl=11818, is_current=True)
    new_m = _managers(session, new, with_password=new_password,
                      with_display=new_display)
    kept = _player(session, "Kept")
    session.add(KeeperSelection(league_id=old.id, manager_id=old_m["A"].id,
                                player_id=kept.id, season_year=2026))
    session.commit()
    return old, old_m, new, new_m, kept


def _check(db, league, name):
    return next((c for c in services.data_health(db, league) if c["check"] == name),
                None)


# ---- logins ------------------------------------------------------------------

def test_dead_logins_are_flagged(test_session):
    """All ten password hashes were NULL on the 26/27 row. A single NULL is the
    legitimate first-time-set state; ten at once is a failed carry."""
    _old, _om, new, _nm, _k = _rolled_over(test_session, new_password=False)
    c = _check(test_session, new, "managers can log in")
    assert c["ok"] is False
    assert "3 with no password" in c["detail"]


def test_healthy_logins_pass(test_session):
    _old, _om, new, _nm, _k = _rolled_over(test_session)
    assert _check(test_session, new, "managers can log in")["ok"] is True


def test_missing_display_names_are_flagged(test_session):
    """The pre-existing check — pinned here because it is the same failure."""
    _old, _om, new, _nm, _k = _rolled_over(test_session, new_display=False)
    assert _check(test_session, new, "all managers have a person name")["ok"] is False


# ---- keeper clocks -----------------------------------------------------------

def test_uncarried_keeper_clocks_are_flagged(test_session):
    """0 seeds against a season whose predecessor had submitted keepers. Production
    was 0 against 152."""
    _old, _om, new, _nm, _k = _rolled_over(test_session)
    c = _check(test_session, new, "keeper clocks carried from last season")
    assert c["ok"] is False
    assert "did not run" in c["detail"]


def test_carried_keeper_clocks_pass(test_session):
    _old, _om, new, new_m, kept = _rolled_over(test_session)
    test_session.add(KeeperSeed(league_id=new.id, manager_id=new_m["A"].id,
                                player_id=kept.id, years_remaining=2))
    test_session.commit()
    assert _check(test_session, new,
                  "keeper clocks carried from last season")["ok"] is True


def test_no_keeper_check_when_last_season_submitted_nothing(test_session):
    """A first season, or one nobody kept into — absence of seeds says nothing, so
    the check should not appear at all rather than fire spuriously."""
    _old = _league(test_session, season_year=2025, fpl=1754, locked=True)
    _managers(test_session, _old)
    new = _league(test_session, season_year=2026, fpl=11818, is_current=True)
    _managers(test_session, new)
    assert _check(test_session, new, "keeper clocks carried from last season") is None


# ---- draft row ---------------------------------------------------------------

def test_a_draft_left_on_the_old_row_is_flagged(test_session):
    """Exactly the un-migrated state: 94 picks for 2026 sitting on the 25/26 row."""
    old, old_m, new, _nm, _k = _rolled_over(test_session)
    p = _player(test_session, "Drafted")
    test_session.add(DraftPick(league_id=old.id, season_year=2026, draft_type="main",
                               round=1, pick_number=1, manager_id=old_m["A"].id,
                               player_id=p.id, source="draft"))
    test_session.commit()

    c = _check(test_session, new, "this season's draft is on this season's row")
    assert c["ok"] is False
    assert "migrate_2026_draft" in c["detail"]


def test_a_migrated_draft_passes(test_session):
    old, _om, new, new_m, _k = _rolled_over(test_session)
    p = _player(test_session, "Drafted")
    test_session.add(DraftPick(league_id=new.id, season_year=2026, draft_type="main",
                               round=1, pick_number=1, manager_id=new_m["A"].id,
                               player_id=p.id, source="draft"))
    test_session.commit()
    assert _check(test_session, new,
                  "this season's draft is on this season's row")["ok"] is True


def test_no_draft_anywhere_is_not_a_failure(test_session):
    """Before the draft has run there is nothing to be on the wrong row."""
    _old, _om, new, _nm, _k = _rolled_over(test_session)
    assert _check(test_session, new,
                  "this season's draft is on this season's row")["ok"] is True


# ---- the cron syncing the wrong league ---------------------------------------

def test_a_frozen_skip_while_this_season_is_live_is_flagged(test_session):
    """The green-but-syncing-nothing case. Every sub-task skips a frozen league and
    the skip sets ok=True, so the only evidence is the note."""
    _old, _om, new, _nm, _k = _rolled_over(test_session)
    test_session.add(SyncLog(
        kind="league", ok=True,
        finished_at=datetime.datetime.now(datetime.timezone.utc),
        notes="season 2025 is frozen (sync_locked); skipped"))
    test_session.commit()

    c = _check(test_session, new, "sync is targeting this season")
    assert c is not None and c["ok"] is False
    assert "different league" in c["detail"]


def test_a_normal_sync_does_not_trip_it(test_session):
    _old, _om, new, _nm, _k = _rolled_over(test_session)
    test_session.add(SyncLog(
        kind="league", ok=True,
        finished_at=datetime.datetime.now(datetime.timezone.utc),
        notes="10 managers"))
    test_session.commit()
    assert _check(test_session, new, "sync is targeting this season") is None


def test_a_deliberately_frozen_season_does_not_trip_it(test_session):
    """Browsing an archived season: the skip is correct there, not a misconfiguration."""
    old = _league(test_session, season_year=2025, fpl=1754, is_current=True,
                  locked=True)
    _managers(test_session, old)
    test_session.add(SyncLog(
        kind="league", ok=True,
        finished_at=datetime.datetime.now(datetime.timezone.utc),
        notes="season 2025 is frozen (sync_locked); skipped"))
    test_session.commit()
    assert _check(test_session, old, "sync is targeting this season") is None
