"""Trades against the v2 app-owned squad ledger.

`record_trade` used to write `Trade` rows and nothing else. On this branch the squad
of record is the ledger (`V2RosterMove` folded by `rules.fold_moves`), so a trade that
only touched the trade log left every squad view — my team, the lineup editor, the
trade picker itself — showing the pre-trade roster. These tests pin both halves: the
ledger actually moves, and the readers actually read it.

DB-backed (see conftest): needs TEST_DATABASE_URL, and SKIPS silently without it.
"""

import pytest

import services
from models import (
    Gameweek,
    League,
    Manager,
    Player,
    PlayerSeason,
    Roster,
    Trade,
    V2RosterMove,
)
from rules import RuleViolation

# 15 = 2 GKP, 5 DEF, 5 MID, 3 FWD; the XI below is a legal 4-4-2.
SQUAD_SHAPE = ["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3


@pytest.fixture
def league(test_session):
    lg = League(
        fpl_league_id="8888", name="Ledger Trade Test", season_year=2026,
        is_current=True, sync_locked=False,
    )
    test_session.add(lg)
    test_session.commit()
    return lg


@pytest.fixture
def gw(test_session, league):
    g = Gameweek(number=1, league_id=league.id)
    test_session.add(g)
    test_session.commit()
    return g


def _manager(session, league, fpl, person):
    m = Manager(league_id=league.id, fpl_manager_id=fpl, name=f"{person} FC",
                display_name=person)
    session.add(m)
    session.commit()
    return m


def _player(session, league, fpl_id, name, position):
    """A player plus this season's identity snapshot — read paths resolve squads
    through PlayerSeason, so a Player row alone is invisible to them."""
    p = Player(code=900000 + fpl_id, fpl_id=fpl_id, name=name, position=position,
               current_team="ARS")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=league.id, player_id=p.id, fpl_id=fpl_id,
                             name=name, position=position, current_team="ARS"))
    session.commit()
    return p


def _squad(session, league, manager, gw, start_fpl_id, *, ledger=True, rosters=True):
    """Give a manager a full 15-man squad. `ledger` writes the v2 moves; `rosters`
    writes the FPL-synced snapshot. Separable on purpose: a test that proves a reader
    uses the ledger has to be able to make the two disagree."""
    players = []
    for i, pos in enumerate(SQUAD_SHAPE):
        p = _player(session, league, start_fpl_id + i, f"P{start_fpl_id + i}", pos)
        players.append(p)
        if ledger:
            session.add(V2RosterMove(league_id=league.id, manager_id=manager.id,
                                     player_id=p.id, gw_number=1, action="add",
                                     source="initial"))
        if rosters:
            session.add(Roster(manager_id=manager.id, player_id=p.id, gameweek_id=gw.id))
    session.commit()
    return players


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setenv("APP_ENGINE", "on")


@pytest.fixture
def two_squads(test_session, league, gw):
    a = _manager(test_session, league, "101", "Ann")
    b = _manager(test_session, league, "102", "Bob")
    return a, b, _squad(test_session, league, a, gw, 1), _squad(test_session, league, b, gw, 101)


def _ledger(session, league, manager):
    return services.get_v2_squad(session, league, manager.fpl_manager_id)


# --------------------------------------------------------------------------
def test_balanced_trade_with_a_pick_moves_players_and_the_pick(
    test_session, league, gw, engine_on, two_squads
):
    """The shape the old code got wrong: two players each way plus a pick attached.
    All four players move in the ledger, the pick reassigns, and both squads are
    still 15 — headcount never moves, so no lineup gate is disturbed."""
    a, b, a_players, b_players = two_squads
    services.record_trade(
        test_session, league, a_fpl="101", b_fpl="102",
        a_players=[a_players[7].fpl_id, a_players[8].fpl_id],   # two MIDs
        b_players=[b_players[7].fpl_id, b_players[8].fpl_id],
        a_picks=[f"{league.season_year + 1}:main:1:Ann"], b_picks=[],
    )

    squad_a, squad_b = _ledger(test_session, league, a), _ledger(test_session, league, b)
    assert len(squad_a) == 15 and len(squad_b) == 15
    for p in (a_players[7], a_players[8]):
        assert p.id in squad_b and p.id not in squad_a
    for p in (b_players[7], b_players[8]):
        assert p.id in squad_a and p.id not in squad_b

    own = services.pick_ownership(test_session, league, league.season_year + 1, "main")
    assert own[(1, "Ann")] == "Bob"
    # the trade log still records every asset
    assert test_session.query(Trade).filter_by(league_id=league.id).count() == 5


def test_unbalanced_trade_is_rejected_and_writes_nothing(
    test_session, league, gw, engine_on, two_squads
):
    """A pure player-for-pick (and any other unequal player count) is refused before
    anything is written — no Trade row, no ledger move, no half-applied trade."""
    a, b, a_players, b_players = two_squads
    for a_ps, b_ps, picks in (
        ([a_players[7].fpl_id], [], [f"{league.season_year + 1}:main:1:Bob"]),  # player for pick
        ([a_players[7].fpl_id, a_players[8].fpl_id], [b_players[7].fpl_id], []),  # 2-for-1
    ):
        with pytest.raises(RuleViolation):
            services.record_trade(
                test_session, league, a_fpl="101", b_fpl="102",
                a_players=a_ps, b_players=b_ps, a_picks=[], b_picks=picks,
            )
        test_session.rollback()

    assert test_session.query(Trade).filter_by(league_id=league.id).count() == 0
    assert (
        test_session.query(V2RosterMove)
        .filter_by(league_id=league.id, source="trade")
        .count() == 0
    )
    assert len(_ledger(test_session, league, a)) == 15


def test_trade_rejects_a_player_the_sender_does_not_own(
    test_session, league, gw, engine_on, two_squads
):
    a, b, a_players, b_players = two_squads
    with pytest.raises(RuleViolation):
        services.record_trade(
            test_session, league, a_fpl="101", b_fpl="102",
            a_players=[b_players[7].fpl_id],   # Ann offering one of Bob's players
            b_players=[b_players[8].fpl_id],
            a_picks=[], b_picks=[],
        )
    test_session.rollback()
    assert test_session.query(Trade).filter_by(league_id=league.id).count() == 0


def test_trade_rejects_a_player_the_receiver_already_owns(
    test_session, league, gw, engine_on
):
    """Exclusive ownership: a player can't land on a squad that already holds him.
    Reachable only through a corrupt ledger, which is exactly when a silent duplicate
    add would be worst."""
    a = _manager(test_session, league, "101", "Ann")
    b = _manager(test_session, league, "102", "Bob")
    a_players = _squad(test_session, league, a, gw, 1)
    b_players = _squad(test_session, league, b, gw, 101)
    shared = a_players[7]
    test_session.add(V2RosterMove(league_id=league.id, manager_id=b.id,
                                  player_id=shared.id, gw_number=1, action="add",
                                  source="initial"))
    test_session.commit()

    with pytest.raises(RuleViolation):
        services.record_trade(
            test_session, league, a_fpl="101", b_fpl="102",
            a_players=[shared.fpl_id], b_players=[b_players[8].fpl_id],
            a_picks=[], b_picks=[],
        )
    test_session.rollback()
    assert test_session.query(Trade).filter_by(league_id=league.id).count() == 0


def test_engine_off_records_the_trade_without_touching_the_ledger(
    test_session, league, gw, monkeypatch, two_squads
):
    """With the engine off the FPL snapshot is still the squad of record — and this
    league's ledger may never have been seeded — so the trade must not append moves
    (nor validate ownership against a ledger nobody is reading)."""
    monkeypatch.setenv("APP_ENGINE", "off")
    a, b, a_players, b_players = two_squads
    services.record_trade(
        test_session, league, a_fpl="101", b_fpl="102",
        a_players=[a_players[7].fpl_id], b_players=[b_players[7].fpl_id],
        a_picks=[], b_picks=[],
    )
    assert test_session.query(Trade).filter_by(league_id=league.id).count() == 2
    assert (
        test_session.query(V2RosterMove)
        .filter_by(league_id=league.id, source="trade")
        .count() == 0
    )


def test_manager_assets_lists_the_ledger_squad_not_the_fpl_snapshot(
    test_session, league, gw, engine_on
):
    """The trade picker's own list. The FPL snapshot here is deliberately a different
    squad: if the picker read it, a manager would be offering players they traded away
    (and unable to offer the ones they got)."""
    a = _manager(test_session, league, "101", "Ann")
    owned = _squad(test_session, league, a, gw, 1, rosters=False)
    stale = _squad(test_session, league, a, gw, 201, ledger=False)

    assets = services.manager_assets(test_session, league, "101")
    listed = {p["fpl_id"] for p in assets["players"]}
    assert listed == {p.fpl_id for p in owned}
    assert listed.isdisjoint({p.fpl_id for p in stale})


def test_set_lineup_sizes_against_the_ledger_squad(
    test_session, league, gw, engine_on
):
    """The 15-man gate has to count the ledger. A 14-man ledger squad is refused even
    though the FPL snapshot says 15 — and the legal XI drawn from the ledger passes."""
    a = _manager(test_session, league, "101", "Ann")
    players = _squad(test_session, league, a, gw, 1)
    test_session.add(V2RosterMove(league_id=league.id, manager_id=a.id,
                                  player_id=players[14].id, gw_number=1,
                                  action="drop", source="free_agent"))
    test_session.commit()

    with pytest.raises(RuleViolation) as e:
        services.set_lineup(test_session, league, "101", 1,
                            [p.fpl_id for p in players[:11]],
                            [p.fpl_id for p in players[11:]], allow_locked=True)
    assert "14 players" in str(e.value)
    test_session.rollback()

    # restore the 15th, then a legal 4-4-2 out of the ledger squad
    test_session.add(V2RosterMove(league_id=league.id, manager_id=a.id,
                                  player_id=players[14].id, gw_number=1,
                                  action="add", source="free_agent"))
    test_session.commit()
    starters = [players[0]] + players[2:6] + players[7:11] + players[12:14]
    bench = [players[1], players[6], players[11], players[14]]
    services.set_lineup(test_session, league, "101", 1,
                        [p.fpl_id for p in starters], [p.fpl_id for p in bench],
                        allow_locked=True)
    saved = services.get_lineup(test_session, league, "101", 1)
    assert saved["starters"] == [p.fpl_id for p in starters]


def test_lineup_editor_shows_the_ledger_squad(test_session, league, gw, engine_on):
    a = _manager(test_session, league, "101", "Ann")
    owned = _squad(test_session, league, a, gw, 1, rosters=False)
    _squad(test_session, league, a, gw, 201, ledger=False)

    editor = services.get_lineup_editor(test_session, league, "101", 1)
    assert {p["fpl_id"] for p in editor["players"]} == {p.fpl_id for p in owned}
    assert editor["squad_size"] == 15


def test_v2_execute_trade_is_gone(test_session):
    """It was a second, dead trade writer (strict 1-for-1, no callers). Two writers
    for one trade is the split this work exists to close — keep it deleted."""
    assert not hasattr(services, "v2_execute_trade")
