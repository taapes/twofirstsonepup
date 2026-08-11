"""record_pick refuses a player who isn't actually available.

The board hides taken players and the search results render them with a pill and no
Draft button, so in normal use this never fires. It exists because the board is not
the only way in: a stale board on a second device, a double-click race, the
token-authenticated /admin API and the autodraft queue all reach record_pick
directly. And the failure it prevents is not self-correcting — once two managers
believe they own the same player, re-picking doesn't undo it.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DraftPick,
    Gameweek,
    KeeperSelection,
    League,
    Manager,
    Player,
    Roster,
    Standing,
)

UPCOMING = 2026


def _seed(session):
    """Two managers, three players, nobody kept or drafted yet."""
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="draft")
    session.add(lg)
    session.flush()
    gw = Gameweek(number=1, league_id=lg.id)
    session.add(gw)
    session.flush()

    mgrs = {}
    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i, total=10 - i,
                             points_for=100))
        mgrs[name] = m

    players = {}
    for i, name in enumerate(["Haaland", "Palmer", "Saka"], start=1):
        p = Player(name=name, code=i * 7, fpl_id=i, position="MID", current_team="ARS")
        session.add(p)
        session.flush()
        session.add(Roster(manager_id=mgrs["A"].id, gameweek_id=gw.id, player_id=p.id))
        players[name] = p
    session.commit()
    return lg, mgrs, players


def _pick(session, lg, *, pick_number, owner_fpl, player_fpl_id, **kw):
    return services.record_pick(
        session, lg, season_year=UPCOMING, pick_number=pick_number,
        owner_fpl=owner_fpl, player_fpl_id=player_fpl_id, **kw,
    )


# ---- the guard ------------------------------------------------------------
def test_an_available_player_is_still_draftable(test_session):
    """Guard against the check refusing everything, which every test below would
    otherwise pass for the wrong reason."""
    lg, _, _ = _seed(test_session)
    out = _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=1)
    assert out["player"] == "Haaland"


def test_a_players_keeper_cannot_be_drafted_by_someone_else(test_session):
    lg, mgrs, players = _seed(test_session)
    test_session.add(KeeperSelection(
        league_id=lg.id, manager_id=mgrs["B"].id, player_id=players["Palmer"].id,
        season_year=UPCOMING,
    ))
    test_session.commit()

    with pytest.raises(services.RuleViolation, match="kept by B"):
        _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=2)
    assert test_session.query(DraftPick).count() == 0


def test_a_player_cannot_be_drafted_twice(test_session):
    """Only the SLOT was guarded before, so the same player could be recorded at two
    different picks and both managers would see them on their roster."""
    lg, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=1)

    with pytest.raises(services.RuleViolation, match="already drafted by A at #1"):
        _pick(test_session, lg, pick_number=2, owner_fpl="2", player_fpl_id=1)
    assert test_session.query(DraftPick).count() == 1


def test_the_guard_is_not_waived_by_overwrite(test_session):
    """overwrite grants permission to replace a SLOT. That is a different thing from
    permission to take a player someone else is keeping — and the live draft passes
    overwrite=is_admin, so conflating them would leave the commissioner unprotected
    for the whole draft."""
    lg, mgrs, players = _seed(test_session)
    test_session.add(KeeperSelection(
        league_id=lg.id, manager_id=mgrs["B"].id, player_id=players["Palmer"].id,
        season_year=UPCOMING,
    ))
    test_session.commit()
    _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=1)

    with pytest.raises(services.RuleViolation, match="kept by B"):
        _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=2,
              overwrite=True)


def test_a_slot_can_be_re_recorded_with_the_same_player(test_session):
    """The current slot is excluded from the already-drafted check, or an admin
    correcting a pick would be blocked by the pick they're correcting."""
    lg, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=1)
    out = _pick(test_session, lg, pick_number=1, owner_fpl="2", player_fpl_id=1,
                overwrite=True)
    assert out["player"] == "Haaland"
    assert test_session.query(DraftPick).count() == 1


def test_deleting_the_conflicting_pick_frees_the_player(test_session):
    """The documented way out: the guard names what to fix, and fixing it works."""
    lg, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=1)
    row = test_session.query(DraftPick).one()
    services.delete_draft_pick(test_session, lg, str(row.id))

    out = _pick(test_session, lg, pick_number=2, owner_fpl="2", player_fpl_id=1)
    assert out["player"] == "Haaland"


def test_the_two_drafts_do_not_block_each_other(test_session):
    """Draft picks are scoped to their own draft_type, matching search_players."""
    lg, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=1)
    # a DIFFERENT slot number, or the current-slot exclusion hides the main pick and
    # this passes whether or not draft_type is actually part of the query
    out = _pick(test_session, lg, pick_number=2, owner_fpl="2", player_fpl_id=1,
                draft_type="discovery")
    assert out["player"] == "Haaland"


def test_a_keeper_blocks_the_discovery_draft_too(test_session):
    """Keeper selections are not scoped to a draft — a kept player is off the board
    for both, which is what search_players already reports."""
    lg, mgrs, players = _seed(test_session)
    test_session.add(KeeperSelection(
        league_id=lg.id, manager_id=mgrs["B"].id, player_id=players["Saka"].id,
        season_year=UPCOMING,
    ))
    test_session.commit()

    with pytest.raises(services.RuleViolation, match="kept by B"):
        _pick(test_session, lg, pick_number=1, owner_fpl="1", player_fpl_id=3,
              draft_type="discovery")
