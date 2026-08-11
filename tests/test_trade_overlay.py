"""Commissioner-entered trades move the player.

Rosters come from FPL-synced snapshots, and in the offseason `latest_gameweek` is
pinned at GW38 forever because sync stops once the season is frozen. A trade the
commissioner enters is one FPL never processed, so no snapshot will ever reflect it —
the player sat on the wrong team on every screen, the receiving manager could not keep
him, and `advance_season` silently wrote him a fresh keeper clock.

Rosters are canonical truth, so the correction is an overlay on READ. Writing a
fabricated Roster row would be indistinguishable from a synced one, and the per-GW
readers (transactions, anti-tanking, IL reconciliation) would ingest it as fact.

Two league rules are load-bearing here and are pinned below:
  - the acquisition LABEL follows the player, so a waiver pickup traded to you still
    eats one of your two waiver keeper slots;
  - the CLOCK arrives already capped by any drop the sender took, or trading a player
    out and back would restore keeper years the drop was supposed to cost.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime as dt
import uuid

import pytest

import services
from models import (
    Gameweek,
    InjuryList,
    KeeperSeed,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerSeason,
    Roster,
    Standing,
    Trade,
)
from rules import KEEPER_FRESH_REMAINING

LAST_GW = 38
UPCOMING = 2026


ALL_GWS = range(1, LAST_GW + 1)


def _with_gap_at(n):
    """Held all season except gameweek `n` — a drop, unless the IL explains it."""
    return [g for g in ALL_GWS if g != n]


def _seed(session, managers=("A", "B", "C")):
    """A finished season: every gameweek 1..38 exists, as in production. A sparse
    calendar would make _dropped see a gap in every tenure, so every player would
    derive as `waiver` and the label tests would pass for the wrong reason."""
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
    for i, name in enumerate(managers, start=1):
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
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id, name=name,
                             position=pos, current_team="ARS"))
    session.commit()
    return p


def _hold(session, mgr, player, gws, numbers):
    for n in numbers:
        session.add(Roster(manager_id=mgr.id, gameweek_id=gws[n].id, player_id=player.id))
    session.commit()


def _trade(session, lg, frm, to, player, *, when=None, fpl_trade_id=None, event_gw=None,
           pick_round=None):
    t = Trade(league_id=lg.id, from_manager=frm.id, to_manager=to.id,
              player_id=player.id if player else None,
              fpl_trade_id=fpl_trade_id, event_gw=event_gw, pick_round=pick_round)
    if when is not None:
        t.created_at = when
    session.add(t)
    session.commit()
    return t


T1 = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
T2 = dt.datetime(2026, 7, 2, tzinfo=dt.timezone.utc)


# ---- the overlay ----------------------------------------------------------
def test_a_commissioner_trade_moves_the_player(test_session):
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "Szoboszlai", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p)
    assert services.player_ownership(test_session, lg) == {p.id: m["B"].id}


def test_a_chain_resolves_to_the_final_owner(test_session):
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p, when=T1)
    _trade(test_session, lg, m["B"], m["C"], p, when=T2)
    assert services.player_ownership(test_session, lg) == {p.id: m["C"].id}


def test_a_trade_and_a_trade_back_leaves_him_where_he_started(test_session):
    """Order is the ONLY thing that separates this from the chain above — which is
    why trades needed a created_at and why a graph walk can't do this."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p, when=T1)
    _trade(test_session, lg, m["B"], m["A"], p, when=T2)
    assert services.player_ownership(test_session, lg) == {}


def test_a_trade_from_the_wrong_manager_fails_closed(test_session):
    """A typo'd direction must leave the player where the snapshot says, not
    teleport him off a manager who never held him."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["B"], m["C"], p)      # B never had him
    assert services.player_ownership(test_session, lg) == {}


def test_two_trades_out_of_one_manager_apply_once(test_session):
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p, when=T1)
    _trade(test_session, lg, m["A"], m["C"], p, when=T2)
    assert services.player_ownership(test_session, lg) == {p.id: m["B"].id}


def test_a_trade_of_an_unrostered_player_moves_nobody(test_session):
    """Seeding from Trade instead of the snapshot would conjure a squad member."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "Nobody", 1)
    _trade(test_session, lg, m["A"], m["B"], p)
    assert services.player_ownership(test_session, lg) == {}


def test_a_synced_trade_is_never_applied_twice(test_session):
    """The snapshot already reflects it. Detectable only because A RECLAIMED him
    later, so the snapshot names A — with the naive fixture the fold starts at B,
    finds no edge out of B, and the bug is invisible."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)     # A holds him at the end
    _trade(test_session, lg, m["A"], m["B"], p, fpl_trade_id="695349", event_gw=15)
    assert services.player_ownership(test_session, lg) == {}


def test_a_pick_trade_never_moves_a_player(test_session):
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p, pick_round=4)
    assert services.player_ownership(test_session, lg) == {}


# ---- keeper derivation ----------------------------------------------------
def _status(session, lg):
    return services._derive_keeper_status(session, lg, kept_all=True)


def test_ownership_moves_in_both_directions(test_session):
    """Asserting only "the buyer has him" passes an implementation that emits him
    under BOTH managers — which is what breaks record_pick's availability guard."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p)

    st = _status(test_session, lg)
    assert p.id in st.get(m["B"].id, {}), "the buyer can't see the player"
    assert p.id not in st.get(m["A"].id, {}), "the seller still holds him"


def test_a_drafted_player_arrives_labelled_draft(test_session):
    """Must be a GW1 fixture: with a mid-season pickup, started_with_manager is False
    either way and this can't detect the label leaking through from the sender."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p)
    assert _status(test_session, lg)[m["B"].id][p.id]["acquisition"] == "draft"


def test_a_waiver_pickup_still_counts_as_waiver_for_the_buyer(test_session):
    """The league rule the commissioner chose: the taint follows the player, so he
    eats one of the buyer's two waiver keeper slots."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(21, LAST_GW + 1))   # not on GW1
    _trade(test_session, lg, m["A"], m["B"], p)
    assert _status(test_session, lg)[m["B"].id][p.id]["acquisition"] == "waiver"


def test_the_clock_carries_across_the_trade(test_session):
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=m["A"].id, player_id=p.id,
                                years_remaining=1, season_year=2025))
    test_session.commit()
    _trade(test_session, lg, m["A"], m["B"], p)

    row = _status(test_session, lg)[m["B"].id][p.id]
    assert row["years_remaining"] == 1, "the buyer got a fresh clock"
    assert row["eligible"] is True


def test_a_maxed_clock_carries_as_maxed(test_session):
    """So the test above can't pass by always returning something eligible."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=m["A"].id, player_id=p.id,
                                years_remaining=0, season_year=2025))
    test_session.commit()
    _trade(test_session, lg, m["A"], m["B"], p)
    assert _status(test_session, lg)[m["B"].id][p.id]["eligible"] is False


def test_a_trade_cannot_launder_away_a_drop(test_session):
    """Seed 4 + a drop caps the sender at KEEPER_FRESH_REMAINING. Handing the buyer
    the RAW seed instead of the sender's derived clock would restore the years the
    drop cost — trade him out and back and the penalty vanishes."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, _with_gap_at(20))   # dropped at GW20
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=m["A"].id, player_id=p.id,
                                years_remaining=4, season_year=2025))
    test_session.commit()
    _trade(test_session, lg, m["A"], m["B"], p)

    assert _status(test_session, lg)[m["B"].id][p.id]["years_remaining"] == \
        KEEPER_FRESH_REMAINING


def test_an_injury_list_gap_still_protects_the_clock_after_a_trade(test_session):
    """The IL entry is recorded against the SENDER, so the buyer's clock is only
    right if the drop test still runs against the sender's history."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, _with_gap_at(20))   # same gap as above...
    test_session.add_all([
        KeeperSeed(league_id=lg.id, manager_id=m["A"].id, player_id=p.id,
                   years_remaining=4, season_year=2025),
        # ...but explained, so it isn't a drop
        InjuryList(manager_id=m["A"].id, player_id=p.id, start_gw=20, end_gw=20,
                   status="returned"),
    ])
    test_session.commit()
    _trade(test_session, lg, m["A"], m["B"], p)

    assert _status(test_session, lg)[m["B"].id][p.id]["years_remaining"] == 4


def test_untraded_players_are_unchanged(test_session):
    lg, m, gws = _seed(test_session)
    drafted = _player(test_session, lg, "Drafted", 1)
    waivered = _player(test_session, lg, "Waivered", 2)
    _hold(test_session, m["A"], drafted, gws, ALL_GWS)
    _hold(test_session, m["A"], waivered, gws, range(21, LAST_GW + 1))

    st = _status(test_session, lg)[m["A"].id]
    assert st[drafted.id]["acquisition"] == "draft"
    assert st[waivered.id]["acquisition"] == "waiver"


# ---- the surfaces the user reported ---------------------------------------
def test_the_players_tab_shows_the_new_owner_with_their_keeper_facts(test_session):
    """Overlaying the owner column without the keeper column (or vice versa) is
    worse than the bug — it shows the new owner with a blank keeper row."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p)

    row = next(r for r in services.player_portal(test_session, lg) if r["name"] == "P")
    assert row["owner"] == "B"
    assert row["acquisition"] == "draft"
    assert row["keeper_years"] is not None


def test_my_team_gains_and_loses_the_player(test_session):
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    other = _player(test_session, lg, "Other", 2)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _hold(test_session, m["A"], other, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p)

    a = {x["name"] for x in services.get_my_team(test_session, lg, "1")["players"]}
    b = {x["name"] for x in services.get_my_team(test_session, lg, "2")["players"]}
    assert a == {"Other"}, "the seller kept the traded player"
    assert b == {"P"}, "the buyer never received him"


def test_the_buyer_can_keep_him_and_the_seller_cannot(test_session):
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p)

    services.submit_keepers(test_session, lg, fpl_manager_id="2",
                            keeper_fpl_ids=[1], season_year=UPCOMING)
    with pytest.raises(services.RuleViolation):
        services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                keeper_fpl_ids=[1], season_year=UPCOMING)


# ---- a selection for a player since traded away ---------------------------
def _stale(session, lg, m, gws):
    """A holds P and submits him, then trades him to B."""
    p = _player(session, lg, "P", 1)
    _hold(session, m["A"], p, gws, ALL_GWS)
    session.add(KeeperSelection(league_id=lg.id, manager_id=m["A"].id, player_id=p.id,
                                season_year=UPCOMING))
    session.commit()
    _trade(session, lg, m["A"], m["B"], p)
    return p


def test_a_selection_for_a_traded_away_player_stops_counting(test_session):
    """Per the league: nothing is deleted and no trade is blocked — the manager just
    ends up one keeper short."""
    lg, m, gws = _seed(test_session)
    p = _stale(test_session, lg, m, gws)

    assert services.effective_keeper_selections(test_session, lg, UPCOMING) == []
    assert test_session.query(KeeperSelection).count() == 1, "the row was deleted"


def test_a_stale_selection_does_not_cost_the_manager_a_draft_pick(test_session):
    lg, m, gws = _seed(test_session)
    _stale(test_session, lg, m, gws)
    board = services.get_draft_board(test_session, lg, UPCOMING)
    a_picks = [b for b in board if b["owner"] == "A"]
    b_picks = [b for b in board if b["owner"] == "B"]
    assert len(a_picks) == len(b_picks) == 15, "a stale selection ate a pick"


def _next_season(session, old, mgrs):
    """The incoming league row advance_season rolls into, with matching entry ids."""
    nxt = League(fpl_league_id="2", name="S26", season_year=UPCOMING, is_current=False,
                 sync_locked=False, phase="preseason")
    session.add(nxt)
    session.flush()
    for name, m in mgrs.items():
        session.add(Manager(league_id=nxt.id, fpl_manager_id=m.fpl_manager_id,
                            name=name, display_name=name))
    session.commit()
    return nxt


def test_the_rollover_never_invents_a_keeper_clock(test_session):
    """The silent write-through corruption: advance_season looked the player up under
    the SELECTING manager, missed, and fell back to a brand-new 2-year clock — baking
    a keeper the manager doesn't own into the new season, permanently."""
    lg, m, gws = _seed(test_session)
    p = _stale(test_session, lg, m, gws)
    nxt = _next_season(test_session, lg, m)

    services.advance_season(test_session, lg, nxt)

    seeds = test_session.query(KeeperSeed).filter_by(league_id=nxt.id).all()
    assert seeds == [], "the rollover invented a keeper clock for a traded-away player"


def test_the_rollover_carries_the_real_clock_for_the_buyer(test_session):
    """The other half: a player traded in and then kept must carry the SENDER's
    clock, decremented — not a fresh one."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=m["A"].id, player_id=p.id,
                                years_remaining=1, season_year=2025))
    test_session.commit()
    _trade(test_session, lg, m["A"], m["B"], p)
    test_session.add(KeeperSelection(league_id=lg.id, manager_id=m["B"].id,
                                     player_id=p.id, season_year=UPCOMING))
    test_session.commit()
    nxt = _next_season(test_session, lg, m)

    services.advance_season(test_session, lg, nxt)

    seed = test_session.query(KeeperSeed).filter_by(league_id=nxt.id).one()
    assert seed.player_id == p.id
    # 1 carried across the trade, minus one for the season ticking over. A fresh
    # fallback would give KEEPER_FRESH_REMAINING - 1 instead.
    assert seed.years_remaining == 0
    assert seed.years_remaining != KEEPER_FRESH_REMAINING - 1


# ---- deliberate non-changes -----------------------------------------------
def test_the_trade_does_not_invent_a_gameweek_transaction(test_session):
    """get_transactions diffs consecutive snapshots. An offseason trade has no honest
    gameweek to sit in, so overlaying it would report a phantom GW38 add/drop pair."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(21, LAST_GW + 1))
    _trade(test_session, lg, m["A"], m["B"], p)

    moves = services.get_transactions(test_session, lg)
    assert not [x for x in moves if x.get("player") == "P"]


def test_the_fpl_roster_health_check_still_reads_the_snapshot(test_session):
    """Its job is to validate the SYNC. A player-for-pick trade legitimately leaves
    14/16 until the draft, and overlaying it would turn a legal trade into a
    permanent red check while hiding a real sync gap."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["A"], m["B"], p)

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    roster = next(v for k, v in checks.items() if k.startswith("15-man rosters (FPL"))
    assert "A=1" in roster["detail"], "the check stopped reading raw rosters"


def test_a_trade_that_did_not_apply_is_surfaced(test_session):
    """player_ownership skips it deliberately; without this check a typo'd direction
    does nothing at all, silently."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _trade(test_session, lg, m["B"], m["C"], p)      # B never held him

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["site trades applied"]["ok"] is False
    assert "P" in checks["site trades applied"]["detail"]


# ---- pick ownership ordering ----------------------------------------------
def test_a_pick_traded_twice_lands_with_the_later_owner(test_session):
    """pick_ownership used to sort on Trade.id — a random uuid4 — so "latest wins"
    was really "whichever id sorted higher". Ids are forced here so the old ordering
    is adversarial rather than a coin flip."""
    lg, m, _ = _seed(test_session)
    first = Trade(id=uuid.UUID(int=2), league_id=lg.id, from_manager=m["A"].id,
                  to_manager=m["B"].id, pick_round=1, pick_season_year=UPCOMING,
                  pick_draft_type="main", pick_original_manager=m["A"].id,
                  created_at=T1)
    second = Trade(id=uuid.UUID(int=1), league_id=lg.id, from_manager=m["B"].id,
                   to_manager=m["C"].id, pick_round=1, pick_season_year=UPCOMING,
                   pick_draft_type="main", pick_original_manager=m["A"].id,
                   created_at=T2)
    test_session.add_all([first, second])
    test_session.commit()

    own = services.pick_ownership(test_session, lg, UPCOMING)
    assert own[(1, "A")] == "C"
