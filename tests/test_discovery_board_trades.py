"""The discovery board has to honour pick trades, like every other reader of them.

Until 2026-09-07 `get_discovery_board` built its slots from the reverse-standings snake
alone and never called `pick_ownership`. So a traded discovery pick was recorded, showed
up in the future-picks grid and in `manager_assets`, and was then ignored by the ONE
board that runs the draft: four of the real 2026 slots would have called a manager who
no longer owned the pick, and `approve_queued_pick` would have autodrafted from his
queue.

`get_draft_board`, `get_future_picks`, `manager_assets` and `draft_preparation` all
folded ownership. This was the only reader that didn't — which is the whole failure
mode, so these tests check the two boards agree rather than just that this one works.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import services
from models import DraftPick, DraftQueue, Gameweek, League, Manager, Player, Standing

SEASON = 2026


def _seed(session):
    """Three managers. Reverse standings puts C first, so the snake is
    R1: C, B, A  /  R2: A, B, C — asymmetric enough that a reassignment is visible."""
    lg = League(fpl_league_id="1", name="S", season_year=SEASON, is_current=True,
                sync_locked=False, phase="in_season")
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


def _slot(board, pick):
    return next(b for b in board if b["pick"] == pick)


def _owner_of(board, round_, original):
    return next(b["owner"] for b in board
                if b["round"] == round_ and b["original_owner"] == original)


# ---- the fix ------------------------------------------------------------------
def test_a_traded_discovery_pick_changes_who_is_on_the_clock(test_session):
    lg, m = _seed(test_session)
    before = services.get_discovery_board(test_session, lg, SEASON)
    assert _owner_of(before, 2, "A") == "A"

    services.trade_pick(test_session, lg, from_fpl="1", to_fpl="3", original_fpl="1",
                        round=2, season_year=SEASON, draft_type="discovery")

    after = services.get_discovery_board(test_session, lg, SEASON)
    assert _owner_of(after, 2, "A") == "C", "the board ignored the trade entirely"


def test_a_traded_slot_says_who_it_came_from(test_session):
    lg, m = _seed(test_session)
    services.trade_pick(test_session, lg, from_fpl="1", to_fpl="3", original_fpl="1",
                        round=2, season_year=SEASON, draft_type="discovery")
    slot = next(b for b in services.get_discovery_board(test_session, lg, SEASON)
                if b["round"] == 2 and b["original_owner"] == "A")
    assert slot["traded"] is True
    assert slot["owner"] == "C" and slot["original_owner"] == "A"


def test_a_trade_moves_the_owner_and_not_the_running_order(test_session):
    """`pick_number` is positional. A trade changes WHO holds a slot, never where the
    slot sits — reordering the snake would re-point every recorded pick number."""
    lg, m = _seed(test_session)
    before = [(b["pick"], b["round"], b["original_owner"])
              for b in services.get_discovery_board(test_session, lg, SEASON)]
    services.trade_pick(test_session, lg, from_fpl="1", to_fpl="3", original_fpl="1",
                        round=2, season_year=SEASON, draft_type="discovery")
    after = [(b["pick"], b["round"], b["original_owner"])
             for b in services.get_discovery_board(test_session, lg, SEASON)]
    assert before == after


def test_an_untraded_board_gains_no_condition_keys(test_session):
    """Condition metadata is attached ONLY to a conditional slot, so an ordinary row
    keeps the shape every consumer already iterates — the rule `get_draft_board`
    follows and that an exact-equality test one module over depends on."""
    lg, m = _seed(test_session)
    for b in services.get_discovery_board(test_session, lg, SEASON):
        assert set(b) == {"pick", "round", "owner", "owner_fpl", "original_owner",
                          "traded", "reassigned", "player"}
        assert b["traded"] is False and b["reassigned"] is False


def test_the_two_boards_agree_about_who_owns_a_traded_pick(test_session):
    """The actual invariant. These are separate functions over one truth, and the bug
    was that only one of them consulted it."""
    lg, m = _seed(test_session)
    services.trade_pick(test_session, lg, from_fpl="1", to_fpl="3", original_fpl="1",
                        round=2, season_year=SEASON, draft_type="discovery")
    own = services.pick_ownership(test_session, lg, SEASON, "discovery")
    assert own[(2, "A")] == "C"
    assert _owner_of(services.get_discovery_board(test_session, lg, SEASON), 2, "A") == "C"


# ---- a completed pick is never re-attributed ----------------------------------
def test_a_recorded_pick_keeps_the_manager_who_actually_made_it(test_session):
    """A trade entered AFTER a selection must not hand that selection to the new owner
    — the positional-`pick_number` rule the main board documents."""
    lg, m = _seed(test_session)
    board = services.get_discovery_board(test_session, lg, SEASON)
    slot = next(b for b in board if b["round"] == 2 and b["original_owner"] == "A")
    test_session.add(DraftPick(
        league_id=lg.id, season_year=SEASON, draft_type="discovery", round=2,
        pick_number=slot["pick"], manager_id=m["A"].id, player_label="Woltemade",
        source="discovery"))
    test_session.commit()

    services.trade_pick(test_session, lg, from_fpl="1", to_fpl="3", original_fpl="1",
                        round=2, season_year=SEASON, draft_type="discovery")

    after = _slot(services.get_discovery_board(test_session, lg, SEASON), slot["pick"])
    assert after["owner"] == "A", "the completed pick was re-attributed"
    assert after["player"] == "Woltemade"
    assert after["reassigned"] is True, "the disagreement must be surfaced, not hidden"


# ---- the operational payoff ---------------------------------------------------
def test_the_autodraft_uses_the_new_owners_queue(test_session):
    """Why this matters on draft night: `approve_queued_pick` resolves the on-the-clock
    manager from this board, so an unfixed board would have drafted from the queue of
    someone who no longer owned the pick."""
    lg, m = _seed(test_session)
    players = {}
    for name, fpl_id in (("Wirtz", 11), ("Sesko", 22)):
        pl = Player(name=name, code=fpl_id * 7, fpl_id=fpl_id, position="MID",
                    current_team="LIV")
        test_session.add(pl)
        players[name] = pl
    test_session.commit()

    first = services.next_open_pick(
        services.get_discovery_board(test_session, lg, SEASON))
    assert first["owner"] == "C", "seed assumption: C picks first"

    # C gives pick 1 away to A, then each queues a DIFFERENT player, so the name that
    # comes out names whose queue was consulted.
    services.trade_pick(test_session, lg, from_fpl="3", to_fpl="1", original_fpl="3",
                        round=1, season_year=SEASON, draft_type="discovery")
    test_session.add_all([
        DraftQueue(league_id=lg.id, manager_id=m["C"].id, season_year=SEASON,
                   draft_type="discovery", player_id=players["Wirtz"].id, rank=1),
        DraftQueue(league_id=lg.id, manager_id=m["A"].id, season_year=SEASON,
                   draft_type="discovery", player_id=players["Sesko"].id, rank=1),
    ])
    test_session.commit()

    on_clock = services.next_open_pick(
        services.get_discovery_board(test_session, lg, SEASON))
    assert on_clock["owner"] == "A", "the traded slot still called C"

    out = services.approve_queued_pick(test_session, lg, season_year=SEASON,
                                       draft_type="discovery")
    assert "Sesko" in str(out), f"autodrafted from the wrong manager's queue: {out}"
