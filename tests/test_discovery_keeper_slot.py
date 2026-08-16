"""The discovery (bonus 6th) keeper must cost its owner a draft slot.

`effective_keeper_selections` decides which submitted selections COUNT, by asking the
roster-ownership map whether the manager still holds the player. That question is
meaningless for a discovery keeper: `submit_keepers` deliberately allows the discovery
pick to be ANY player rather than one off the final roster (that is the entire point of
the September discovery draft), so being off-roster is its NORMAL state, not evidence
the manager lost him.

The filter had a carve-out for goalie teams — which have no `rosters` row either — but
not for the discovery keeper, so it was dropped as "no longer effectively owned". The
manager then got the player AND a free pick:

  - availability is derived separately and still reported "kept: X", so no other
    manager could draft him;
  - `get_draft_board` sizes each manager's slots from effective_keeper_selections, so
    the owner was never charged for him.

Found on 2026-08-15, the night before the draft, by scripts/preflight_draft.py's
"no stale keeper selections" check — two real managers were each carrying an extra
pick, which also shifts every later pick NUMBER on the board.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import services
from models import (
    Gameweek,
    KeeperSelection,
    League,
    Manager,
    Player,
    Roster,
    Standing,
)

UPCOMING = 2026
ALL_GWS = range(1, 39)


def _seed(session):
    """A finished season with a full calendar — a sparse one would make _dropped see a
    gap in every tenure and derive everyone as `waiver`."""
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=True, phase="offseason")
    session.add(lg)
    session.flush()
    gws = {}
    for n in ALL_GWS:
        g = Gameweek(number=n, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[n] = g

    mgrs = {}
    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i,
                             total=100 - i, points_for=1000 - i))
        mgrs[name] = m
    session.commit()
    return lg, mgrs, gws


def _player(session, lg, name, fpl_id, pos="MID"):
    p = Player(name=name, code=fpl_id * 7, fpl_id=fpl_id, position=pos,
               current_team="ARS", price=50, status="a")
    session.add(p)
    session.flush()
    return p


def _rostered(session, lg, mgr, player, gws):
    """On this manager's roster all season, so they genuinely own him."""
    for n in ALL_GWS:
        session.add(Roster(manager_id=mgr.id, player_id=player.id,
                           gameweek_id=gws[n].id))
    session.commit()


def _select(session, lg, mgr, player, *, is_discovery=False):
    session.add(KeeperSelection(
        league_id=lg.id, manager_id=mgr.id, player_id=player.id,
        season_year=UPCOMING, is_discovery=is_discovery,
    ))
    session.commit()


def test_an_off_roster_discovery_keeper_still_counts(test_session):
    """The core of it: a discovery keeper the manager never had on their roster is a
    legitimate selection, not a stale one."""
    lg, mgrs, gws = _seed(test_session)
    disc = _player(test_session, lg, "Discovery", 900)
    _select(test_session, lg, mgrs["A"], disc, is_discovery=True)

    counted = services.effective_keeper_selections(test_session, lg, UPCOMING)
    assert [s.player_id for s in counted] == [disc.id]


def test_the_discovery_keeper_costs_its_owner_a_draft_slot(test_session):
    """The consequence that actually reached production: A kept a player and was not
    charged for him, so A had one more pick than B and every later pick number moved."""
    lg, mgrs, gws = _seed(test_session)
    disc = _player(test_session, lg, "Discovery", 900)
    _select(test_session, lg, mgrs["A"], disc, is_discovery=True)

    board = services.get_draft_board(test_session, lg, UPCOMING)
    a = [b for b in board if b["original_owner"] == "A"]
    b = [b for b in board if b["original_owner"] == "B"]
    assert len(a) == 14, "the discovery keeper did not cost A a slot"
    assert len(b) == 15, "B keeps nobody and should hold a full board"


def test_nobody_else_can_draft_the_discovery_keeper(test_session):
    """The other half of the asymmetry — pinned so a future 'fix' can't resolve the
    slot bug by making him draftable instead."""
    lg, mgrs, gws = _seed(test_session)
    disc = _player(test_session, lg, "Discovery", 900)
    _select(test_session, lg, mgrs["A"], disc, is_discovery=True)

    rows = services.search_players(
        test_session, lg, q="Discovery", available_year=UPCOMING,
        include_taken=True, kept_all=True,
    )
    assert len(rows) == 1
    assert rows[0]["taken"] is True
    assert rows[0]["taken_by"] == "kept: A"


def test_an_ordinary_selection_the_manager_lost_still_stops_counting(test_session):
    """The behaviour the carve-out must NOT weaken: a non-discovery selection for a
    player the manager no longer holds is still stale, and still refunds the slot."""
    lg, mgrs, gws = _seed(test_session)
    gone = _player(test_session, lg, "Gone", 901)
    _select(test_session, lg, mgrs["A"], gone, is_discovery=False)

    assert services.effective_keeper_selections(test_session, lg, UPCOMING) == []
    board = services.get_draft_board(test_session, lg, UPCOMING)
    assert len([b for b in board if b["original_owner"] == "A"]) == 15


def test_a_rostered_ordinary_selection_still_counts(test_session):
    """The control: the ordinary path is untouched by the carve-out."""
    lg, mgrs, gws = _seed(test_session)
    held = _player(test_session, lg, "Held", 902)
    _rostered(test_session, lg, mgrs["A"], held, gws)
    _select(test_session, lg, mgrs["A"], held, is_discovery=False)

    counted = services.effective_keeper_selections(test_session, lg, UPCOMING)
    assert [s.player_id for s in counted] == [held.id]
    board = services.get_draft_board(test_session, lg, UPCOMING)
    assert len([b for b in board if b["original_owner"] == "A"]) == 14


def test_an_off_roster_discovery_keeper_shows_on_the_keepers_page(test_session):
    """The bug reported live during draft prep: the slot-count fix (above) never
    touched `_derive_keeper_status`, so the SAME off-roster discovery keeper who
    correctly cost his owner a slot was invisible on the keepers report itself —
    `get_keepers`/`keeper_candidates`'s report view are built entirely from this
    function's output, and it never had a discovery carve-out of its own."""
    lg, mgrs, gws = _seed(test_session)
    disc = _player(test_session, lg, "Discovery", 900, pos="MID")
    _select(test_session, lg, mgrs["A"], disc, is_discovery=True)

    report = services.get_keepers(test_session, lg, viewer_is_admin=True)
    mine = next(r for r in report if r["manager"] == "A")
    row = next(p for p in mine["players"] if p["player"] == "Discovery")
    assert row["acquisition"] == "discovery"
    assert row["years_remaining"] == 4
    assert row["eligible"] is True
    assert row["kept"] is True


def test_a_commissioner_seed_still_overrides_the_discovery_clock(test_session):
    """The override precedence a commissioner already relies on elsewhere must keep
    working for this new path too — a seed wins over the synthesized discovery clock,
    exactly as it wins over a normal derivation."""
    from models import KeeperSeed

    lg, mgrs, gws = _seed(test_session)
    disc = _player(test_session, lg, "Discovery", 900, pos="MID")
    _select(test_session, lg, mgrs["A"], disc, is_discovery=True)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=mgrs["A"].id,
                                player_id=disc.id, years_remaining=1,
                                acquisition="waiver"))
    test_session.commit()

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    st = status[mgrs["A"].id][disc.id]
    assert (st["acquisition"], st["years_remaining"]) == ("waiver", 1)


def test_privacy_default_is_unaffected_by_the_carve_out(test_session):
    """A caller passing no viewer at all keeps the exact pre-fix shape: an ordinary
    rostered candidate's ELIGIBILITY is still public (this function's own documented
    design), while the discovery-only candidate — reachable ONLY through the `kept`
    dict, which itself defaults to empty with no viewer — stays invisible exactly as
    it already was before this fix. The carve-out is additive to an already-scoped
    dict, never a new leak."""
    lg, mgrs, gws = _seed(test_session)
    held = _player(test_session, lg, "Held", 902)
    _rostered(test_session, lg, mgrs["A"], held, gws)
    disc = _player(test_session, lg, "Discovery", 900, pos="MID")
    _select(test_session, lg, mgrs["A"], disc, is_discovery=True)

    status = services._derive_keeper_status(test_session, lg)
    assert held.id in status[mgrs["A"].id], "ordinary eligibility is public, unchanged"
    assert disc.id not in status[mgrs["A"].id], "discovery-only stays gated with no viewer"
