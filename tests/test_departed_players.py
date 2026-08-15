"""A player who left the Premier League this transfer window keeps their row (for
history/keeper-clock purposes) but loses their fpl_id — the pool churn is expected
and by design (see player identity docs in CLAUDE.md). The gap this closes is
presentational/operational, not a sync bug: `keeper_candidates` already excludes
these players with a "no longer in the Premier League" reason; `search_players` did
not, so a departed player rendered as an available Draft/+Q target whose button
posted `player_fpl_id=None` — a raw 422 in the UI, and in `approve_queued_pick` (which
resolves the same way) a queued departed player reaches `_resolve_player(db, None)`,
which matches every departed row via `fpl_id IS NULL` and raises an unhandled
MultipleResultsFound instead of a clean refusal.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import DraftQueue, Gameweek, League, Manager, Player, Standing
from rules import RuleViolation

UPCOMING = 2026


def _seed(session):
    """Two managers; reverse standings (no lottery rows) puts B on the clock first.
    One active player, one departed (fpl_id/code both None, as a real transfer-window
    departure leaves them — see sync.py's phase 1b)."""
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

    active = Player(name="Haaland", code=1, fpl_id=1, position="FWD", current_team="MCI")
    departed = Player(name="Salah", code=2, fpl_id=None, position="MID", current_team="LIV")
    session.add_all([active, departed])
    session.commit()

    on_clock = services.next_open_pick(services.get_draft_board(session, lg, UPCOMING))
    assert on_clock["owner"] == "B", "seed assumption — B must be on the clock at pick 1"
    return lg, mgrs, {"active": active, "departed": departed}


def test_a_departed_player_is_excluded_by_default(test_session):
    lg, mgrs, players = _seed(test_session)
    rows = services.search_players(test_session, lg, q="Salah", available_year=UPCOMING)
    assert rows == []


def test_a_departed_player_shows_a_reason_not_a_null_id_when_taken_is_included(
    test_session,
):
    lg, mgrs, players = _seed(test_session)
    rows = services.search_players(
        test_session, lg, q="Salah", available_year=UPCOMING, include_taken=True,
    )
    assert len(rows) == 1
    assert rows[0]["taken"] is True
    assert rows[0]["taken_by"] == "no longer in the Premier League"
    assert rows[0]["fpl_id"] is None


def test_an_active_player_is_unaffected(test_session):
    lg, mgrs, players = _seed(test_session)
    rows = services.search_players(test_session, lg, q="Haaland", available_year=UPCOMING)
    assert len(rows) == 1
    assert rows[0]["taken"] is False
    assert rows[0]["fpl_id"] == 1


def test_resolve_player_refuses_none_cleanly(test_session):
    """The backstop: whatever reaches record_pick with no fpl_id gets a RuleViolation,
    not Player.fpl_id == None matching every departed row and raising
    MultipleResultsFound (an unhandled 500 mid-draft)."""
    _seed(test_session)
    with pytest.raises(RuleViolation, match="no player specified"):
        services._resolve_player(test_session, None)


def test_approve_queued_pick_skips_a_departed_player_instead_of_crashing(test_session):
    lg, mgrs, players = _seed(test_session)
    test_session.add(DraftQueue(
        league_id=lg.id, manager_id=mgrs["B"].id, season_year=UPCOMING,
        draft_type="main", player_id=players["departed"].id, rank=1,
    ))
    test_session.commit()

    with pytest.raises(RuleViolation, match="all unavailable"):
        services.approve_queued_pick(test_session, lg, season_year=UPCOMING)


def test_approve_queued_pick_falls_through_a_departed_player_to_the_next_queued(
    test_session,
):
    """Not just 'doesn't crash' — the queue keeps working past the bad entry."""
    lg, mgrs, players = _seed(test_session)
    test_session.add_all([
        DraftQueue(league_id=lg.id, manager_id=mgrs["B"].id, season_year=UPCOMING,
                  draft_type="main", player_id=players["departed"].id, rank=1),
        DraftQueue(league_id=lg.id, manager_id=mgrs["B"].id, season_year=UPCOMING,
                  draft_type="main", player_id=players["active"].id, rank=2),
    ])
    test_session.commit()

    out = services.approve_queued_pick(test_session, lg, season_year=UPCOMING)
    assert out["player"] == "Haaland"
