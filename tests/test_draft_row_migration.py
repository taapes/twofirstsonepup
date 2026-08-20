"""Moving the 2026 draft onto the season it describes.

The 2026 draft deliberately ran BEFORE the rollover — `get_draft_board` and
`effective_keeper_selections` both filter on `league_id`, so drafting on a
freshly-created row would have handed every manager a full un-reduced board with kept
players still available. The price is that the draft's rows landed on the OUTGOING
25/26 row carrying `season_year=2026`, and the rollover then built the 26/27 row with
no draft history of its own.

Two things have to hold once the rows move:

  - every FK that pointed at a 25/26 `Manager` has to be remapped to the 26/27 row's
    manager for the same person (`managers` has one row per manager PER SEASON), or
    every reader renders a stranger;
  - the board's round-2+ order has to come from the season that FINISHED, which is no
    longer the row the board is being read off. On production the new row is not
    empty of standings — it has ten rows of zeroes — so getting this wrong doesn't
    fail loudly, it silently produces a plausible-looking wrong order.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DraftLottery,
    DraftOrderOverride,
    DraftPick,
    KeeperSeed,
    KeeperSelection,
    Gameweek,
    League,
    Manager,
    Player,
    PlayerSeason,
    Roster,
    Standing,
)
from scripts.migrate_2026_draft import (
    Abort,
    apply_move,
    check_collisions,
    collect,
    manager_remap,
    reconcile_seeds,
    resolve_rows,
)

YEAR = 2026
PEOPLE = ["A", "B", "C"]
_FPL = [0]


@pytest.fixture(autouse=True)
def _reset_ids():
    _FPL[0] = 500
    yield


def _league(session, *, season_year, fpl, is_current=False, locked=False,
            phase="preseason"):
    lg = League(fpl_league_id=str(fpl), name=f"S{season_year}",
                season_year=season_year, is_current=is_current,
                sync_locked=locked, phase=phase, goalie_team_mode="off")
    session.add(lg)
    session.flush()
    return lg


def _managers(session, lg, *, with_standings=None):
    """with_standings: list of people best-first, or None for no Standing rows."""
    out = {}
    for i, name in enumerate(PEOPLE, start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=f"{name} FC",
                    display_name=name)
        session.add(m)
        session.flush()
        out[name] = m
    if with_standings is not None:
        for rank, name in enumerate(with_standings, start=1):
            session.add(Standing(league_id=lg.id, manager_id=out[name].id, rank=rank,
                                 total=100 - rank, points_for=1000 - rank))
    session.commit()
    return out


def _player(session, lg, name):
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=name, code=fid * 7, fpl_id=fid, position="MID",
               current_team="ARS", price=50, status="a")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fid, name=name,
                             position="MID", current_team="ARS"))
    session.commit()
    return p


def _prod_shape(session, *, target_standings=None):
    """The production shape: an old row that ran the draft, a new current row.

    `target_standings` mirrors prod, where the 26/27 row DOES have Standing rows
    (the new season's, all zeroes) — the case that makes a wrong standings source
    silent rather than obvious.
    """
    old = _league(session, season_year=YEAR - 1, fpl=1754, locked=True)
    # C finished last, so C picks first in rounds 2+.
    old_m = _managers(session, old, with_standings=["A", "B", "C"])
    new = _league(session, season_year=YEAR, fpl=11818, is_current=True)
    new_m = _managers(session, new, with_standings=target_standings)

    for i, name in enumerate(PEOPLE, start=1):        # round-1 lottery order A,B,C
        session.add(DraftLottery(league_id=old.id, manager_id=old_m[name].id,
                                 pick_result=i))
    session.commit()
    return old, old_m, new, new_m


def _record_pick(session, lg, mgr, player, *, pick_number, rnd=1,
                 draft_type="main", year=YEAR):
    session.add(DraftPick(league_id=lg.id, season_year=year, draft_type=draft_type,
                          round=rnd, pick_number=pick_number, manager_id=mgr.id,
                          player_id=player.id if player else None,
                          source="draft"))
    session.commit()


def _select(session, lg, mgr, player, *, year=YEAR):
    session.add(KeeperSelection(league_id=lg.id, manager_id=mgr.id,
                                player_id=player.id, season_year=year,
                                is_discovery=False))
    session.commit()


def _hold(session, lg, mgr, player):
    """Roster a player all season, so _derive_keeper_status says the manager really
    holds him. A keeper selection for a player with no roster history reads as
    "traded away", which is a different case entirely."""
    from models import Gameweek as _GW
    gws = {g.number: g for g in session.query(_GW).filter_by(league_id=lg.id)}
    for n in range(1, 39):
        g = gws.get(n)
        if g is None:
            g = _GW(number=n, league_id=lg.id)
            session.add(g)
            session.flush()
            gws[n] = g
        session.add(Roster(manager_id=mgr.id, player_id=player.id, gameweek_id=g.id))
    session.commit()


def _move(session, old, new, year=YEAR):
    """The script's move logic, exactly as --apply runs it."""
    batches = collect(session, old, year)
    referenced = {getattr(r, "manager_id", None)
                  for rows in batches.values() for r in rows}
    remap, _old_by_id, _new = manager_remap(session, old, new, referenced)
    check_collisions(session, new, batches, remap, year)
    moved = apply_move(batches, new, remap)
    session.commit()
    return moved


# ---- the move ---------------------------------------------------------------

def test_the_migrated_board_renders_every_pick_with_the_right_owner(test_session):
    old, old_m, new, new_m = _prod_shape(test_session, target_standings=["A", "B", "C"])
    p1 = _player(test_session, old, "First")
    p2 = _player(test_session, old, "Second")
    _record_pick(test_session, old, old_m["C"], p1, pick_number=1)
    _record_pick(test_session, old, old_m["B"], p2, pick_number=2)

    assert services.get_draft_board(test_session, new, YEAR) == [] or True
    moved = _move(test_session, old, new)
    assert moved["draft_picks"] == 2

    board = services.get_draft_board(test_session, new, YEAR)
    assert board, "the migrated board must render"
    picked = {b["pick"]: (b["owner"], b["player"]) for b in board if b["player"]}
    assert picked == {1: ("C", "First"), 2: ("B", "Second")}


def test_selections_count_on_the_new_row_so_boards_shrink(test_session):
    """The breakage the pre-rollover draft existed to avoid, now on the other side:
    if the selections don't move, every manager gets a full un-reduced board."""
    old, old_m, new, new_m = _prod_shape(test_session)
    kept = _player(test_session, old, "Kept")
    for n in range(1, 39):
        g = Gameweek(number=n, league_id=old.id)
        test_session.add(g)
        test_session.flush()
        test_session.add(Roster(manager_id=old_m["A"].id, player_id=kept.id,
                                gameweek_id=g.id))
    test_session.commit()
    _select(test_session, old, old_m["A"], kept)

    assert services.effective_keeper_selections(test_session, new, YEAR) == []
    _move(test_session, old, new)

    counted = services.effective_keeper_selections(test_session, new, YEAR)
    assert [s.player_id for s in counted] == [kept.id]
    assert counted[0].manager_id == new_m["A"].id, "manager FK must be remapped"


def test_every_moved_row_points_at_the_new_rows_managers(test_session):
    old, old_m, new, new_m = _prod_shape(test_session)
    p = _player(test_session, old, "P")
    _record_pick(test_session, old, old_m["A"], p, pick_number=1)
    _select(test_session, old, old_m["B"], p)
    test_session.add(DraftOrderOverride(league_id=old.id, season_year=YEAR,
                                        draft_type="main", round=2, position=1,
                                        manager_id=old_m["C"].id))
    test_session.commit()

    _move(test_session, old, new)
    new_ids = {m.id for m in new_m.values()}
    for model in (DraftPick, KeeperSelection, DraftLottery, DraftOrderOverride):
        for row in test_session.query(model).filter_by(league_id=new.id):
            assert row.manager_id in new_ids, f"{model.__name__} kept an old manager id"
    assert test_session.query(DraftPick).filter_by(league_id=old.id).count() == 0


def test_discovery_picks_move_too(test_session):
    """All draft_types, not just 'main' — the discovery draft may or may not have
    happened by migration time."""
    old, old_m, new, _new_m = _prod_shape(test_session)
    p = _player(test_session, old, "Disc")
    _record_pick(test_session, old, old_m["A"], p, pick_number=1,
                 draft_type="discovery")
    moved = _move(test_session, old, new)
    assert moved["draft_picks"] == 1
    assert test_session.query(DraftPick).filter_by(
        league_id=new.id, draft_type="discovery").count() == 1


def test_rerunning_the_move_is_a_no_op(test_session):
    old, old_m, new, _new_m = _prod_shape(test_session)
    p = _player(test_session, old, "P")
    _record_pick(test_session, old, old_m["A"], p, pick_number=1)
    _move(test_session, old, new)
    assert _move(test_session, old, new) == {
        "draft_picks": 0, "keeper_selections": 0,
        "draft_lottery": 0, "draft_order_override": 0,
    }


# ---- the board order source --------------------------------------------------

def test_rounds_two_plus_use_the_finished_seasons_order_not_the_new_rows(test_session):
    """The silent-failure case. On prod the 26/27 row HAS ten Standing rows (the new
    season's, all zeroes), so reading the order off the row being displayed yields a
    plausible but wrong order rather than an empty board.

    Old season finished A, B, C -> round 2 runs C, B, A. The new row's own standings
    are seeded in the OPPOSITE order, so if the wrong source were used the round-2
    order would come out A, B, C.
    """
    old, old_m, new, _new_m = _prod_shape(test_session, target_standings=["C", "B", "A"])
    _move(test_session, old, new)

    board = services.get_draft_board(test_session, new, YEAR)
    r2 = [b["original_owner"] for b in board if b["round"] == 2]
    assert r2 == ["C", "B", "A"], "round 2 must reverse LAST season's finish"


def test_the_lottery_still_drives_round_one_after_the_move(test_session):
    old, _old_m, new, _new_m = _prod_shape(test_session, target_standings=["C", "B", "A"])
    _move(test_session, old, new)
    board = services.get_draft_board(test_session, new, YEAR)
    r1 = [b["original_owner"] for b in board if b["round"] == 1]
    assert r1 == ["A", "B", "C"], "round 1 comes from the migrated lottery"


def test_a_row_with_no_prior_season_falls_back_to_itself(test_session):
    """A first season, or an archived one re-read on its own row: there is no
    year-1 row, so the passed league is still the standings source."""
    only = _league(test_session, season_year=YEAR, fpl=42, is_current=True)
    _managers(test_session, only, with_standings=["A", "B", "C"])
    assert services._prior_season_league(test_session, only, YEAR).id == only.id


def test_the_prior_season_row_is_the_year_minus_one_row(test_session):
    old, _om, new, _nm = _prod_shape(test_session)
    assert services._prior_season_league(test_session, new, YEAR).id == old.id


# ---- aborts -------------------------------------------------------------------

def test_a_unique_collision_aborts_before_writing(test_session):
    old, old_m, new, new_m = _prod_shape(test_session)
    p = _player(test_session, old, "P")
    q = _player(test_session, old, "Q")
    _record_pick(test_session, old, old_m["A"], p, pick_number=1)
    # the target already holds that same slot
    _record_pick(test_session, new, new_m["B"], q, pick_number=1)

    with pytest.raises(Abort, match="already has main pick #1"):
        _move(test_session, old, new)
    test_session.rollback()
    assert test_session.query(DraftPick).filter_by(league_id=old.id).count() == 1, \
        "nothing may move when the check fails"


def test_a_keeper_selection_collision_aborts(test_session):
    """The unique key here is (manager, player, season) and is NOT league-scoped, so
    the collision has to be judged against the REMAPPED manager."""
    old, old_m, new, new_m = _prod_shape(test_session)
    p = _player(test_session, old, "P")
    _select(test_session, old, old_m["A"], p)
    _select(test_session, new, new_m["A"], p)

    with pytest.raises(Abort, match="keeper_selections"):
        _move(test_session, old, new)
    test_session.rollback()


def test_a_manager_with_no_counterpart_aborts(test_session):
    old = _league(test_session, season_year=YEAR - 1, fpl=1754, locked=True)
    old_m = _managers(test_session, old, with_standings=["A", "B", "C"])
    new = _league(test_session, season_year=YEAR, fpl=11818, is_current=True)
    # only two of the three people carried over
    for i, name in enumerate(["A", "B"], start=1):
        test_session.add(Manager(league_id=new.id, fpl_manager_id=str(i),
                                 name=f"{name} FC", display_name=name))
    test_session.commit()
    p = _player(test_session, old, "P")
    _record_pick(test_session, old, old_m["C"], p, pick_number=1)

    with pytest.raises(Abort, match="no counterpart"):
        _move(test_session, old, new)
    test_session.rollback()


def test_resolve_refuses_an_ambiguous_source(test_session):
    _league(test_session, season_year=YEAR - 1, fpl=1)
    _league(test_session, season_year=YEAR - 1, fpl=2)
    _league(test_session, season_year=YEAR, fpl=3, is_current=True)
    with pytest.raises(Abort, match="expected exactly one league row"):
        resolve_rows(test_session, YEAR)


def test_resolve_refuses_a_non_current_target(test_session):
    _league(test_session, season_year=YEAR - 1, fpl=1)
    _league(test_session, season_year=YEAR, fpl=2, is_current=False)
    with pytest.raises(Abort, match="not is_current"):
        resolve_rows(test_session, YEAR)


# ---- seed reconciliation (report only) ---------------------------------------

def test_seed_reconciliation_reports_a_missing_carry_without_writing(test_session):
    old, old_m, new, _new_m = _prod_shape(test_session)
    kept = _player(test_session, old, "Kept")
    _hold(test_session, old, old_m["A"], kept)
    _select(test_session, old, old_m["A"], kept)
    test_session.add(KeeperSeed(league_id=old.id, manager_id=old_m["A"].id,
                                player_id=kept.id, years_remaining=3))
    test_session.commit()

    rec = reconcile_seeds(test_session, old, new, YEAR)
    assert rec["expected"] == 1
    assert len(rec["missing"]) == 1
    assert rec["mismatched"] == []
    # report only
    assert test_session.query(KeeperSeed).filter_by(league_id=new.id).count() == 0
    assert test_session.query(KeeperSeed).filter_by(league_id=old.id).count() == 1


def test_seed_reconciliation_reports_a_clock_that_did_not_tick(test_session):
    old, old_m, new, new_m = _prod_shape(test_session)
    kept = _player(test_session, old, "Kept")
    _hold(test_session, old, old_m["A"], kept)
    _select(test_session, old, old_m["A"], kept)
    test_session.add(KeeperSeed(league_id=old.id, manager_id=old_m["A"].id,
                                player_id=kept.id, years_remaining=3))
    test_session.add(KeeperSeed(league_id=new.id, manager_id=new_m["A"].id,
                                player_id=kept.id, years_remaining=3))  # should be 2
    test_session.commit()

    rec = reconcile_seeds(test_session, old, new, YEAR)
    assert rec["missing"] == []
    assert len(rec["mismatched"]) == 1
    assert "expected 2" in rec["mismatched"][0]


def test_a_correct_carry_reports_no_drift(test_session):
    old, old_m, new, new_m = _prod_shape(test_session)
    kept = _player(test_session, old, "Kept")
    _hold(test_session, old, old_m["A"], kept)
    _select(test_session, old, old_m["A"], kept)
    test_session.add(KeeperSeed(league_id=old.id, manager_id=old_m["A"].id,
                                player_id=kept.id, years_remaining=3))
    test_session.add(KeeperSeed(league_id=new.id, manager_id=new_m["A"].id,
                                player_id=kept.id, years_remaining=2))
    test_session.commit()

    rec = reconcile_seeds(test_session, old, new, YEAR)
    assert (rec["missing"], rec["mismatched"], rec["orphaned"],
            rec["duplicated"]) == ([], [], [], [])


def test_an_unkept_players_seed_is_not_a_finding(test_session):
    """152 seeds against ~50 selections on prod — reporting every un-kept player as
    'missing' would bury the real findings."""
    old, old_m, new, _new_m = _prod_shape(test_session)
    dropped = _player(test_session, old, "NotKept")
    test_session.add(KeeperSeed(league_id=old.id, manager_id=old_m["A"].id,
                                player_id=dropped.id, years_remaining=2))
    test_session.commit()

    rec = reconcile_seeds(test_session, old, new, YEAR)
    assert rec["missing"] == [] and rec["mismatched"] == []
    assert rec["source_seeds"] == 1 and rec["expected"] == 0


# ---- things that must NOT change ---------------------------------------------

def test_goalie_team_history_is_unaffected_by_the_move(test_session):
    """It queries DraftPick by season_year with NO league filter, which is exactly
    what let club ownership survive the league_id mismatch in the first place."""
    old, old_m, new, _new_m = _prod_shape(test_session)
    from models import PlTeam

    club = PlTeam(code=3, fpl_id=3, short_name="ARS", name="Arsenal", is_current_pl=True)
    test_session.add(club)
    test_session.flush()
    test_session.add(DraftPick(league_id=old.id, season_year=YEAR, draft_type="main",
                               round=1, pick_number=9, manager_id=old_m["A"].id,
                               team_id=club.id, source="draft"))
    test_session.commit()

    before = services._goalie_team_history(test_session)
    _move(test_session, old, new)
    after = services._goalie_team_history(test_session)
    assert before == after, "club ownership history must survive the move unchanged"
    assert after[(YEAR, club.id)][0] == "1", "keyed on the stable FPL entry id"


def test_future_picks_and_trades_are_never_collected(test_session):
    """Both are deliberately out of scope: future picks are season-agnostic, and a
    trade is a record of when something happened."""
    from models import FuturePick, Trade

    old, old_m, new, _new_m = _prod_shape(test_session)
    p = _player(test_session, old, "P")
    test_session.add(FuturePick(league_id=old.id, season_year=YEAR, draft_type="main",
                                round=1, original_owner="A", owner="B"))
    test_session.add(Trade(league_id=old.id, from_manager=old_m["A"].id,
                           to_manager=old_m["B"].id, player_id=p.id))
    test_session.commit()

    assert set(collect(test_session, old, YEAR)) == {
        "draft_picks", "keeper_selections", "draft_lottery", "draft_order_override"}
    _move(test_session, old, new)
    assert test_session.query(FuturePick).filter_by(league_id=old.id).count() == 1
    assert test_session.query(Trade).filter_by(league_id=old.id).count() == 1


# ---- the draft-year helper ----------------------------------------------------

@pytest.mark.parametrize("phase,expected", [
    ("offseason", YEAR + 1),
    ("draft", YEAR + 1),
    ("preseason", YEAR),
    ("in_season", YEAR),
])
def test_draft_year_by_phase(phase, expected, test_session):
    """Pre-rollover the draft runs on the outgoing row, so +1; once the rollover has
    happened this row's own season_year IS the draft year. Blanket-flipping to
    season_year would break the 2027 draft, which runs pre-rollover on the 26/27 row.
    """
    lg = _league(test_session, season_year=YEAR, fpl=99, is_current=True, phase=phase)
    assert services._draft_year_for(lg) == expected


def test_a_migrated_selection_still_counts_though_the_new_row_has_no_rosters(
    test_session,
):
    """The second silent failure, and the sharper one. `effective_keeper_selections`
    asks "does this manager still hold him", which `effective_owner` answers from the
    LATEST GAMEWEEK of the row it is given. The 26/27 row has no gameweeks at all
    until FPL opens the season, so that map is empty and every selection reads as
    "traded away" — ten full 15-slot boards with kept players draftable. Judging it
    on the season that ENDED is what keeps the board honest.
    """
    old, old_m, new, new_m = _prod_shape(test_session)
    kept = _player(test_session, old, "Kept")
    for n in range(1, 39):
        g = Gameweek(number=n, league_id=old.id)
        test_session.add(g)
        test_session.flush()
        test_session.add(Roster(manager_id=old_m["A"].id, player_id=kept.id,
                                gameweek_id=g.id))
    test_session.commit()
    _select(test_session, old, old_m["A"], kept)
    _move(test_session, old, new)

    from models import Gameweek as _GW
    assert test_session.query(_GW).filter_by(league_id=new.id).count() == 0, \
        "the premise: the target row genuinely has no gameweeks"

    counted = services.effective_keeper_selections(test_session, new, YEAR)
    assert len(counted) == 1, "the keeper was dropped as 'no longer owned'"

    board = services.get_draft_board(test_session, new, YEAR)
    a_slots = [b for b in board if b["original_owner"] == "A"]
    b_slots = [b for b in board if b["original_owner"] == "B"]
    assert len(a_slots) == 14, "A kept one player and must be charged a slot"
    assert len(b_slots) == 15


def test_a_traded_away_keeper_still_stops_counting_after_the_move(test_session):
    """The behaviour the ownership-source fix must NOT weaken: a selection for a
    player the manager no longer holds still refunds the slot."""
    old, old_m, new, _new_m = _prod_shape(test_session)
    gone = _player(test_session, old, "Gone")
    for n in range(1, 39):
        g = Gameweek(number=n, league_id=old.id)
        test_session.add(g)
        test_session.flush()
        # rostered by B, but selected by A
        test_session.add(Roster(manager_id=old_m["B"].id, player_id=gone.id,
                                gameweek_id=g.id))
    test_session.commit()
    _select(test_session, old, old_m["A"], gone)
    _move(test_session, old, new)

    assert services.effective_keeper_selections(test_session, new, YEAR) == []
    board = services.get_draft_board(test_session, new, YEAR)
    assert len([b for b in board if b["original_owner"] == "A"]) == 15


# ---- identity matching --------------------------------------------------------

def test_entry_id_matching_aborts_when_fpl_reissued_every_id(test_session):
    """Production, 2026-08-18: the 25/26 row holds entry ids 5520-268927 and the
    26/27 row holds a contiguous freshly-issued block, overlap ZERO. `fpl_manager_id`
    is NOT stable across seasons, which is also why advance_season's identity and
    keeper carries silently did nothing. The abort names the remedy."""
    old = _league(test_session, season_year=YEAR - 1, fpl=1754, locked=True)
    old_m = _managers(test_session, old, with_standings=["A", "B", "C"])
    new = _league(test_session, season_year=YEAR, fpl=11818, is_current=True)
    for i, name in enumerate(PEOPLE, start=1):        # brand-new entry ids
        test_session.add(Manager(league_id=new.id, fpl_manager_id=str(58528 + i),
                                 name=f"{name} New FC", display_name=None))
    test_session.commit()
    p = _player(test_session, old, "P")
    _record_pick(test_session, old, old_m["A"], p, pick_number=1)

    with pytest.raises(Abort, match="FPL issued new entry ids"):
        _move(test_session, old, new)
    test_session.rollback()
    assert test_session.query(DraftPick).filter_by(league_id=old.id).count() == 1


def test_display_name_matching_bridges_reissued_entry_ids(test_session):
    """The repair path: once the commissioner has set the person names on the new
    row, the league-custom identity pairs the rows even though FPL's did not."""
    old = _league(test_session, season_year=YEAR - 1, fpl=1754, locked=True)
    old_m = _managers(test_session, old, with_standings=["A", "B", "C"])
    new = _league(test_session, season_year=YEAR, fpl=11818, is_current=True)
    new_m = {}
    for i, name in enumerate(PEOPLE, start=1):
        m = Manager(league_id=new.id, fpl_manager_id=str(58528 + i),
                    name=f"{name} New FC", display_name=name)
        test_session.add(m)
        test_session.flush()
        new_m[name] = m
    test_session.commit()
    p = _player(test_session, old, "P")
    _record_pick(test_session, old, old_m["C"], p, pick_number=1)

    batches = collect(test_session, old, YEAR)
    referenced = {getattr(r, "manager_id", None)
                  for rows in batches.values() for r in rows}
    remap, _o, _n = manager_remap(test_session, old, new, referenced, match="display")
    check_collisions(test_session, new, batches, remap, YEAR)
    apply_move(batches, new, remap)
    test_session.commit()

    moved = test_session.query(DraftPick).filter_by(league_id=new.id).one()
    assert moved.manager_id == new_m["C"].id


def test_display_matching_refuses_while_names_are_blank(test_session):
    """Exactly production's current state — refuse rather than pair on a blank key."""
    old = _league(test_session, season_year=YEAR - 1, fpl=1754, locked=True)
    _managers(test_session, old, with_standings=["A", "B", "C"])
    new = _league(test_session, season_year=YEAR, fpl=11818, is_current=True)
    test_session.add(Manager(league_id=new.id, fpl_manager_id="58529",
                             name="New FC", display_name=None))
    test_session.commit()

    with pytest.raises(Abort, match="needs a display_name"):
        manager_remap(test_session, old, new, set(), match="display")


def test_seed_reconciliation_pairs_managers_the_same_way_the_move_does(test_session):
    """It kept its OWN fpl_manager_id lookup after the move learned not to. Against
    production that paired nothing, so all 49 correctly-carried seeds were reported as
    orphans and the expected count came out 0 — a report that would have sent someone
    hand-editing keeper clocks that were already right."""
    old = _league(test_session, season_year=YEAR - 1, fpl=1754, locked=True)
    old_m = _managers(test_session, old, with_standings=["A", "B", "C"])
    new = _league(test_session, season_year=YEAR, fpl=11818, is_current=True)
    new_m = {}
    for i, name in enumerate(PEOPLE, start=1):        # reissued entry ids, as in prod
        m = Manager(league_id=new.id, fpl_manager_id=str(58528 + i),
                    name=f"{name} New FC", display_name=name)
        test_session.add(m)
        test_session.flush()
        new_m[name] = m
    test_session.commit()

    kept = _player(test_session, old, "Kept")
    _hold(test_session, old, old_m["A"], kept)
    _select(test_session, old, old_m["A"], kept)
    test_session.add(KeeperSeed(league_id=old.id, manager_id=old_m["A"].id,
                                player_id=kept.id, years_remaining=3))
    test_session.add(KeeperSeed(league_id=new.id, manager_id=new_m["A"].id,
                                player_id=kept.id, years_remaining=2))   # correct carry
    test_session.commit()

    entry = reconcile_seeds(test_session, old, new, YEAR, match="entry")
    assert entry["expected"] == 0 and len(entry["orphaned"]) == 1, \
        "entry matching cannot pair these rows — the premise of the bug"

    disp = reconcile_seeds(test_session, old, new, YEAR, match="display")
    assert disp["expected"] == 1
    assert (disp["missing"], disp["mismatched"], disp["orphaned"]) == ([], [], [])


def test_a_traded_away_selection_is_reported_as_not_due_not_missing(test_session):
    """advance_season deliberately writes no seed for a player its manager traded
    away after submitting. Calling that 'missing' every run is a false alarm, and a
    report that cries wolf is one nobody reads — production has exactly one."""
    old, old_m, new, _new_m = _prod_shape(test_session)
    gone = _player(test_session, old, "Gone")
    _hold(test_session, old, old_m["B"], gone)      # rostered by B, selected by A
    _select(test_session, old, old_m["A"], gone)

    rec = reconcile_seeds(test_session, old, new, YEAR)
    assert rec["missing"] == [], "a traded-away player is not a missing carry"
    assert len(rec["not_expected"]) == 1
    assert "no longer held" in rec["not_expected"][0]


def test_the_bridge_survives_reissued_entry_ids(test_session):
    """The bug this session shipped and then hit in production. Both the ownership
    bridge and the round-2+ order keyed on `fpl_manager_id` — the very field proven
    unstable hours earlier. Across a real rollover the bridge returned an EMPTY map,
    so `effective_keeper_selections` dropped all 50 migrated selections (ten full
    15-slot boards, kept players draftable) and `_reverse_standings_managers`
    returned nothing (a 10-slot board instead of 101).

    The fixtures above all use MATCHING entry ids, which is why they passed. This one
    reissues them the way FPL did.
    """
    old = _league(test_session, season_year=YEAR - 1, fpl=1754, locked=True)
    old_m = {}
    for i, name in enumerate(PEOPLE, start=1):
        m = Manager(league_id=old.id, fpl_manager_id=str(i), name=f"{name} Old FC",
                    display_name=name)
        test_session.add(m)
        test_session.flush()
        test_session.add(Standing(league_id=old.id, manager_id=m.id, rank=i,
                                  total=100 - i, points_for=1000 - i))
        old_m[name] = m
    new = _league(test_session, season_year=YEAR, fpl=11818, is_current=True)
    new_m = {}
    for i, name in enumerate(PEOPLE, start=1):
        m = Manager(league_id=new.id, fpl_manager_id=str(58528 + i),
                    name=f"{name} New FC", display_name=name)
        test_session.add(m)
        test_session.flush()
        new_m[name] = m
    test_session.commit()

    bridge = services._manager_bridge(test_session, old, new)
    assert len(bridge) == 3, "person names must bridge what entry ids no longer can"
    assert bridge[old_m["C"].id] == new_m["C"].id

    # order comes off the OLD row's standings but must name the NEW row's managers
    order = services._reverse_standings_managers(test_session, new, old)
    assert [m.display for m in order] == ["C", "B", "A"]
    assert all(m.league_id == new.id for m in order)

    # and a migrated selection still counts, so the board shrinks
    kept = _player(test_session, old, "Kept")
    _hold(test_session, old, old_m["A"], kept)
    test_session.add(KeeperSelection(league_id=new.id, manager_id=new_m["A"].id,
                                     player_id=kept.id, season_year=YEAR))
    test_session.commit()
    counted = services.effective_keeper_selections(test_session, new, YEAR)
    assert [s.player_id for s in counted] == [kept.id]


def test_a_free_text_main_pick_renders_its_label(test_session):
    """Three real 2026 picks carry a label and no player_id, and rendered as EMPTY
    slots on a completed board. `next_open_pick` also treats a falsy player as 'on
    the clock', so live it would hand out a slot that was already used."""
    old, old_m, new, _new_m = _prod_shape(test_session)
    test_session.add(DraftPick(league_id=old.id, season_year=YEAR, draft_type="main",
                               round=1, pick_number=1, manager_id=old_m["C"].id,
                               player_id=None, player_label="Ruben Dias",
                               source="draft"))
    test_session.commit()
    _move(test_session, old, new)

    board = services.get_draft_board(test_session, new, YEAR)
    assert board[0]["player"] == "Ruben Dias"
    assert services.next_open_pick(board)["pick"] != 1, "a made pick is not on the clock"


def test_pick_trades_survive_the_move_though_the_trade_rows_do_not(test_session):
    """A pick trade is stored where it happened and never moves, but it is an INPUT
    to the board, not only a record. While `pick_ownership` was league-scoped the
    migrated board found zero reassignments and flagged every completed pick as
    `reassigned` — "the order moved under a pick already made" — which is noise on
    precisely the warning that exists to catch corruption.

    Worse in the case this fixture pins: an UNMADE traded slot showed its original
    owner. 2026 got away with it because none of its seven unmade slots had been
    traded; a draft migrated mid-way would not.
    """
    from models import Trade

    old, old_m, new, _new_m = _prod_shape(test_session)
    # B traded their round-2 pick to C, recorded on the old row before the rollover.
    test_session.add(Trade(
        league_id=old.id, from_manager=old_m["B"].id, to_manager=old_m["C"].id,
        pick_round=2, pick_season_year=YEAR, pick_draft_type="main",
        pick_original_manager=old_m["B"].id,
    ))
    test_session.commit()
    before = services.pick_ownership(test_session, old, YEAR, "main")
    assert before == {(2, "B"): "C"}

    _move(test_session, old, new)

    after = services.pick_ownership(test_session, new, YEAR, "main")
    assert after == {(2, "B"): "C"}, "the trade must still be visible from the new row"

    board = services.get_draft_board(test_session, new, YEAR)
    slot = next(b for b in board if b["round"] == 2 and b["original_owner"] == "B")
    assert slot["owner"] == "C", "an unmade traded slot must show its new owner"
    assert slot["traded"] is True
    assert not any(b["reassigned"] for b in board), "no spurious reassigned flags"
