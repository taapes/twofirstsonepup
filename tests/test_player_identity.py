"""Season-scoped player identity: the code rekey and the player_season snapshot.

Regression suite for the Aug-2026 incident, where a 26/27 sync rewrote 570 of 841
player rows in place because `players` is keyed on fpl_id and FPL reassigns element
ids every season. Every test here runs against TEST_DATABASE_URL (see conftest) —
sync_players mutates the whole pool, so it must never touch the configured DB.
"""

import asyncio
import uuid

import pytest

import services
import sync
from models import Gameweek, League, Manager, Player, PlayerSeason, Roster

GKP, DEF, MID, FWD = 1, 2, 3, 4
POS = {GKP: "GKP", DEF: "DEF", MID: "MID", FWD: "FWD"}


def _feed(elements, teams=("ARS", "LIV")):
    """A bootstrap-static payload shaped like the draft API's."""
    return {
        "elements": elements,
        "element_types": [
            {"id": i, "singular_name_short": s} for i, s in POS.items()
        ],
        "teams": [{"id": i + 1, "short_name": t} for i, t in enumerate(teams)],
    }


def _el(fpl_id, code, name, pos=DEF, team=1, **kw):
    return {
        "id": fpl_id, "code": code, "web_name": name, "element_type": pos,
        "team": team, "status": "a", "total_points": 0, **kw,
    }


def _run_sync(payload, monkeypatch):
    """Run sync_players against a stubbed feed. The classic bootstrap is fetched
    best-effort by the real code and is allowed to fail; we serve the same payload."""

    async def _get_json(client, url):
        return payload

    monkeypatch.setattr(sync, "_get_json", _get_json)
    asyncio.run(sync.sync_players())


@pytest.fixture
def live_league(test_session):
    """A current, unfrozen league — sync_players no-ops without one, which would
    make every assertion below pass vacuously."""
    lg = League(
        fpl_league_id="9999", name="Test League", season_year=2026,
        is_current=True, sync_locked=False,
    )
    test_session.add(lg)
    test_session.commit()
    return lg


def _players(session):
    return {p.name: p for p in session.query(Player)}


# --------------------------------------------------------------------------
def test_no_hijack_when_element_id_is_reused_same_position(
    test_session, live_league, monkeypatch
):
    """The motivating case. FPL hands Gabriel's old element id to J.Timber, and
    BOTH are defenders — so position alone cannot tell them apart. The un-coded
    Gabriel row must survive untouched rather than being renamed in place."""
    gabriel = Player(code=None, fpl_id=5, name="Gabriel", position="DEF",
                     current_team="ARS")
    test_session.add(gabriel)
    test_session.commit()
    gabriel_id = gabriel.id

    _run_sync(_feed([_el(5, 999, "J.Timber", DEF)]), monkeypatch)
    test_session.expire_all()

    old = test_session.get(Player, gabriel_id)
    assert old.name == "Gabriel", "the existing row was hijacked"
    assert old.code is None
    assert old.fpl_id is None, "its element id should have been released"

    timber = test_session.query(Player).filter_by(code=999).one()
    assert timber.id != gabriel_id and timber.fpl_id == 5


def test_no_hijack_when_position_also_differs(test_session, live_league, monkeypatch):
    """Weaker variant of the same rule — the position check alone would catch it."""
    old = Player(code=None, fpl_id=7, name="Someone", position="DEF")
    test_session.add(old)
    test_session.commit()
    old_id = old.id

    _run_sync(_feed([_el(7, 4242, "Different", MID)]), monkeypatch)
    test_session.expire_all()

    assert test_session.get(Player, old_id).name == "Someone"
    assert test_session.query(Player).filter_by(code=4242).one().id != old_id


def test_straight_id_swap_survives_the_partial_unique_index(
    test_session, live_league, monkeypatch
):
    """Two players exchange element ids across a season. Assigning naively would
    violate uq_players_fpl_id_live mid-transaction; phase 1b frees both first."""
    a = Player(code=111, fpl_id=5, name="A", position="DEF")
    b = Player(code=222, fpl_id=12, name="B", position="MID")
    test_session.add_all([a, b])
    test_session.commit()
    a_id, b_id = a.id, b.id

    _run_sync(
        _feed([_el(12, 111, "A", DEF), _el(5, 222, "B", MID)]), monkeypatch
    )  # no IntegrityError == the point of the test
    test_session.expire_all()

    assert test_session.get(Player, a_id).fpl_id == 12
    assert test_session.get(Player, b_id).fpl_id == 5


def test_code_match_wins_over_fpl_id(test_session, live_league, monkeypatch):
    """A player whose element id moved is still the same row, and the row that
    inherited their old id does not steal their identity."""
    p = Player(code=555, fpl_id=3, name="Mover", position="MID", current_team="ARS")
    test_session.add(p)
    test_session.commit()
    pid = p.id

    _run_sync(_feed([_el(30, 555, "Mover", MID, team=2)]), monkeypatch)
    test_session.expire_all()

    same = test_session.get(Player, pid)
    assert same.fpl_id == 30, "matched by code, updated in place"
    assert same.current_team == "LIV"
    assert test_session.query(Player).count() == 1, "no duplicate row created"


def test_incident_replay_history_survives_a_new_season(
    test_session, live_league, monkeypatch
):
    """End to end: a squad from season 1 must still read correctly after season 2
    shuffles every element id. This is the bug that shipped."""
    # --- season 1: two players, a manager, a roster ---
    _run_sync(
        _feed([_el(5, 111, "Gabriel", DEF, team=1),
               _el(12, 222, "Salah", MID, team=2)]),
        monkeypatch,
    )
    test_session.expire_all()
    p = _players(test_session)
    gw = Gameweek(number=1, league_id=live_league.id)
    mgr = Manager(league_id=live_league.id, fpl_manager_id="1", name="Team A")
    test_session.add_all([gw, mgr])
    test_session.flush()
    for name in ("Gabriel", "Salah"):
        test_session.add(Roster(manager_id=mgr.id, gameweek_id=gw.id,
                                player_id=p[name].id))
    test_session.commit()

    season1_squad = services.get_rosters(test_session, live_league)
    assert sorted(x["name"] for x in season1_squad[0]["players"]) == ["Gabriel", "Salah"]
    assert {x["name"]: x["team"] for x in season1_squad[0]["players"]} == {
        "Gabriel": "ARS", "Salah": "LIV",
    }

    # --- freeze season 1, open season 2 with ids shuffled AND clubs changed ---
    live_league.sync_locked = True
    live_league.is_current = False
    season2 = League(fpl_league_id="8888", name="S2", season_year=2027,
                     is_current=True, sync_locked=False)
    test_session.add(season2)
    test_session.commit()

    _run_sync(
        _feed([_el(12, 111, "Gabriel", DEF, team=2),   # id 5 -> 12, ARS -> LIV
               _el(5, 222, "Salah", MID, team=1)]),    # id 12 -> 5, LIV -> ARS
        monkeypatch,
    )
    test_session.expire_all()

    # season 1's page must be unchanged — same players, same 25/26 clubs
    after = services.get_rosters(test_session, live_league)
    assert {x["name"]: x["team"] for x in after[0]["players"]} == {
        "Gabriel": "ARS", "Salah": "LIV",
    }, "historical squad picked up the new season's clubs"

    # ...while the live pool has moved on
    assert test_session.query(Player).filter_by(code=111).one().current_team == "LIV"


def test_season_identity_is_scoped_and_non_empty(
    test_session, live_league, monkeypatch
):
    _run_sync(_feed([_el(5, 111, "Gabriel", DEF)]), monkeypatch)
    test_session.expire_all()

    ident = services.season_identity(test_session, live_league)
    assert ident, "empty identity map would render every squad blank"
    (ps,) = ident.values()
    assert (ps.name, ps.position, ps.current_team) == ("Gabriel", "DEF", "ARS")

    other = League(fpl_league_id="7777", name="Other", season_year=2030)
    test_session.add(other)
    test_session.commit()
    assert services.season_identity(test_session, other) == {}


def test_player_season_row_per_league_and_element(
    test_session, live_league, monkeypatch
):
    _run_sync(_feed([_el(5, 111, "A", DEF), _el(6, 222, "B", MID)]), monkeypatch)
    rows = test_session.query(PlayerSeason).filter_by(league_id=live_league.id).all()
    assert {r.fpl_id for r in rows} == {5, 6}
    # player_id must point at the stable player row, not be a copy of the PK
    ids = {r.player_id for r in rows}
    assert ids == {p.id for p in test_session.query(Player)}


def test_frozen_league_snapshot_is_not_rewritten(
    test_session, live_league, monkeypatch
):
    """The whole point: once a season is frozen its snapshot stops moving."""
    _run_sync(_feed([_el(5, 111, "Gabriel", DEF, team=1)]), monkeypatch)
    test_session.expire_all()
    before = test_session.query(PlayerSeason).filter_by(
        league_id=live_league.id).one()
    assert before.current_team == "ARS"

    live_league.sync_locked = True
    live_league.is_current = False
    s2 = League(fpl_league_id="8888", name="S2", season_year=2027,
                is_current=True, sync_locked=False)
    test_session.add(s2)
    test_session.commit()

    _run_sync(_feed([_el(5, 111, "Gabriel", DEF, team=2)]), monkeypatch)
    test_session.expire_all()

    frozen = test_session.query(PlayerSeason).filter_by(
        league_id=live_league.id).one()
    assert frozen.current_team == "ARS", "a frozen season's snapshot was rewritten"
    assert test_session.query(PlayerSeason).filter_by(
        league_id=s2.id).one().current_team == "LIV"


def test_departed_player_keeps_identity_and_releases_its_slot(
    test_session, live_league, monkeypatch
):
    """A player who leaves the PL must keep their name (history still renders) but
    give up their element id, so a newcomer can take it without a collision."""
    gone = Player(code=111, fpl_id=5, name="Departed", position="FWD")
    test_session.add(gone)
    test_session.commit()
    gone_id = gone.id

    _run_sync(_feed([_el(5, 999, "Newcomer", FWD)]), monkeypatch)
    test_session.expire_all()

    old = test_session.get(Player, gone_id)
    assert old.name == "Departed" and old.code == 111
    assert old.fpl_id is None
    assert test_session.query(Player).filter_by(code=999).one().fpl_id == 5


def test_sync_writes_the_full_name_alongside_the_web_name(
    test_session, live_league, monkeypatch
):
    """`name` is FPL's web_name — the short form — and first_name/second_name used to
    be discarded. Matching a discovery pick needs the long one: a manager writes
    "Nick Woltemade" and the pool says "Woltemade"."""
    _run_sync(_feed([
        _el(5, 111, "Woltemade", FWD, first_name="Nick", second_name="Woltemade"),
    ]), monkeypatch)
    test_session.expire_all()

    p = test_session.query(Player).filter_by(code=111).one()
    assert p.name == "Woltemade", "display still uses web_name"
    assert p.full_name == "Nick Woltemade"


def test_a_missing_full_name_stays_null_rather_than_blank(
    test_session, live_league, monkeypatch
):
    """None and "" would both be falsy to the matcher, but only None distinguishes
    "FPL sent nothing" from "synced and genuinely empty"."""
    _run_sync(_feed([_el(5, 111, "Mystery", FWD)]), monkeypatch)
    test_session.expire_all()
    assert test_session.query(Player).filter_by(code=111).one().full_name is None
