"""services.draft_preparation — assembling the model from real league data.

The pure model is tested in test_draftprep_model.py. This is about the wiring, where
the failure modes are different: reading a table the tool must never read, taking a
player's position from the wrong season, losing a pick trade, or letting a player who
has left the Premier League be predicted as a keeper.

The load-bearing test here is the blind-prediction differential. Everything else the
tool does is a judgement call that can be argued with; reading other managers'
submitted keepers would be a broken promise.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DraftLottery,
    Gameweek,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerProjection,
    PlayerSeason,
    Roster,
    Standing,
    Trade,
)

LAST_GW = 38
YEAR = 2026
ALL_GWS = range(1, LAST_GW + 1)


def _seed(session, managers=("A", "B")):
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
        session.add_all([
            Standing(league_id=lg.id, manager_id=m.id, rank=i, total=100 - i,
                     points_for=1000 - i),
            DraftLottery(league_id=lg.id, manager_id=m.id, pick_result=i),
        ])
        mgrs[name] = m
    session.commit()
    return lg, mgrs, gws


_FPL = [0]


def _player(session, lg, name, pos, points, *, owner=None, gws=None, fpl=True,
            season_pos=None):
    """A player, optionally rostered all season, optionally with a projection.

    `season_pos` sets the 25/26 PlayerSeason position independently of the live one,
    which is how the wrong-season-position trap gets exercised.
    """
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=name, code=fid * 7, fpl_id=fid if fpl else None, position=pos,
               current_team="ARS", price=50, status="a")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fid, name=name,
                             position=season_pos or pos, current_team="ARS"))
    if points is not None:
        session.add(PlayerProjection(season_year=YEAR, player_id=p.id, raw_name=name,
                                     raw_team="ARS", raw_position=pos, price=5.0,
                                     points=points))
    if owner is not None:
        for n in (gws or ALL_GWS):
            session.add(Roster(manager_id=owner.id, gameweek_id=gws_of(session, lg)[n].id,
                               player_id=p.id))
    session.commit()
    return p


_GW_CACHE = {}


def gws_of(session, lg):
    if lg.id not in _GW_CACHE:
        _GW_CACHE[lg.id] = {
            g.number: g for g in session.query(Gameweek).filter_by(league_id=lg.id)
        }
    return _GW_CACHE[lg.id]


def _squad(session, lg, mgr, *, prefix, points, positions=None):
    """15 rostered players for one manager, descending in projected points."""
    positions = positions or (["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3)
    out = []
    for i, pos in enumerate(positions):
        out.append(_player(session, lg, f"{prefix}{i}", pos, points[i], owner=mgr))
    return out


@pytest.fixture(autouse=True)
def _clear_gw_cache():
    _GW_CACHE.clear()
    yield
    _GW_CACHE.clear()


# ---- the promise ----------------------------------------------------------
def test_the_prediction_ignores_submitted_keepers_entirely(test_session):
    """The differential that makes 'blind' verifiable.

    Assert the prediction is byte-identical before and after inserting selections
    that name the manager's five WORST candidates — the maximally different answer.
    Asserting `"kept" not in result` would pass even if the code read the table and
    simply didn't echo it back.
    """
    lg, mgrs, _ = _seed(test_session)
    squad = _squad(test_session, lg, mgrs["A"], prefix="A",
                   points=[300 - 10 * i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B",
           points=[200 - 5 * i for i in range(15)])

    before = services.draft_preparation(test_session, lg, YEAR)
    predicted = [r.name for r in before["predictions"][mgrs["A"].id]["keepers"]]
    assert predicted, "fixture produced no prediction to compare"

    # the five worst — deliberately the opposite of what the model chose
    for p in squad[-5:]:
        test_session.add(KeeperSelection(league_id=lg.id, manager_id=mgrs["A"].id,
                                         player_id=p.id, season_year=YEAR))
    test_session.commit()

    after = services.draft_preparation(test_session, lg, YEAR)
    assert [r.name for r in after["predictions"][mgrs["A"].id]["keepers"]] == predicted
    assert {p.name for p in squad[-5:]} != set(predicted), "fixture wasn't contradictory"


def test_it_never_queries_the_keeper_selection_table(test_session):
    """Structural backstop to the differential above: catches intent, not just
    behaviour, so a read that happens not to change the answer still fails."""
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[300 - 10 * i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[200 - 5 * i for i in range(15)])

    seen = []
    real_query = test_session.query

    def spy(*entities, **kw):
        seen.extend(str(e) for e in entities)
        return real_query(*entities, **kw)

    test_session.query = spy
    try:
        services.draft_preparation(test_session, lg, YEAR)
    finally:
        test_session.query = real_query
    assert not [s for s in seen if "KeeperSelection" in s], seen


def test_it_writes_nothing(test_session):
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[300 - 10 * i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[200 - 5 * i for i in range(15)])
    services.draft_preparation(test_session, lg, YEAR)
    assert not test_session.new and not test_session.dirty and not test_session.deleted


# ---- pool definition ------------------------------------------------------
def test_a_player_with_no_projection_is_excluded_even_if_available(test_session):
    """Keyed on having a projection, NOT on Player.status — those two sets coincide
    in today's data by coincidence, and status changes on every sync."""
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[300 - 10 * i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[200 - 5 * i for i in range(15)])
    ghost = _player(test_session, lg, "NoProjection", "MID", None)   # status='a'

    out = services.draft_preparation(test_session, lg, YEAR)
    assert "NoProjection" in out["excluded"]
    assert ghost.id not in {r.player_id for r in out["pool"]}


def test_a_player_who_left_the_league_is_never_predicted_as_a_keeper(test_session):
    """No fpl_id means he can't be kept OR drafted, however good his projection."""
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[100 - i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[90 - i for i in range(15)])
    _player(test_session, lg, "Departed", "MID", 999, owner=mgrs["A"], fpl=False)

    out = services.draft_preparation(test_session, lg, YEAR)
    kept = [r.name for r in out["predictions"][mgrs["A"].id]["keepers"]]
    assert "Departed" not in kept
    assert "Departed" in out["departed"].get("A", [])


def test_position_comes_from_the_live_season_not_the_old_snapshot(test_session):
    """_derive_keeper_status resolves identity through season_identity, so ITS
    position is 25/26. Using it would put a player in the wrong 26/27 quota."""
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[100 - i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[90 - i for i in range(15)])
    switched = _player(test_session, lg, "Switched", "FWD", 500, owner=mgrs["A"],
                       season_pos="DEF")

    out = services.draft_preparation(test_session, lg, YEAR)
    kept = out["predictions"][mgrs["A"].id]["keepers"]
    rec = next(r for r in kept if r.player_id == switched.id)
    assert rec.position == "FWD", "used last season's position"


# ---- order and pick trades ------------------------------------------------
def test_the_slot_order_matches_the_live_board_machinery(test_session):
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[300 - 10 * i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[200 - 5 * i for i in range(15)])
    out = services.draft_preparation(test_session, lg, YEAR)
    # each manager needs 15 - keepers picks, so the draft runs that many rounds
    needed = {m: 15 - len(out["predictions"][m]["keepers"]) for m in out["predictions"]}
    assert out["rounds"] == max(needed.values())
    assert len(out["slots"]) == sum(needed.values())


def test_a_traded_pick_is_attributed_to_the_manager_who_holds_it(test_session):
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[300 - 10 * i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[200 - 5 * i for i in range(15)])

    def _r1_owner(frm, to):
        test_session.query(Trade).delete()
        test_session.add(Trade(league_id=lg.id, from_manager=frm.id, to_manager=to.id,
                               pick_round=1, pick_season_year=YEAR,
                               pick_draft_type="main", pick_original_manager=frm.id))
        test_session.commit()
        out = services.draft_preparation(test_session, lg, YEAR)
        slot = next(s for s in out["slots"]
                    if s["round"] == 1 and s["original"] == frm.id)
        return out["names"][slot["manager"]]

    # and flip it: a one-directional assertion passes with the overlay deleted
    assert _r1_owner(mgrs["A"], mgrs["B"]) == "B"
    assert _r1_owner(mgrs["B"], mgrs["A"]) == "A"


# ---- the simulation, end to end -------------------------------------------
def test_every_slot_is_resolved_and_nobody_exceeds_fifteen(test_session):
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[300 - 10 * i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[200 - 5 * i for i in range(15)])
    for i in range(40):   # a pool deep enough to fill both squads
        _player(test_session, lg, f"Pool{i}", ["GKP", "DEF", "MID", "FWD"][i % 4], 150 - i)

    out = services.draft_preparation(test_session, lg, YEAR)
    assert len(out["sim"]["picks"]) == len(out["slots"])
    for mid, squad in out["sim"]["squads"].items():
        assert len(squad) <= 15, out["names"][mid]


def test_gone_by_reports_the_pick_a_player_is_expected_to_go(test_session):
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[100 - i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[90 - i for i in range(15)])
    star = _player(test_session, lg, "Star", "MID", 999)
    for i in range(30):
        _player(test_session, lg, f"Pool{i}", ["GKP", "DEF", "MID", "FWD"][i % 4], 50 - i)

    out = services.draft_preparation(test_session, lg, YEAR)
    assert out["gone_by"].get(star.id) == 1, "the best available didn't go first"


def test_no_projections_imported_is_reported_not_crashed(test_session):
    lg, mgrs, _ = _seed(test_session)
    _player(test_session, lg, "Someone", "MID", None, owner=mgrs["A"])
    out = services.draft_preparation(test_session, lg, YEAR)
    assert out["available"] is False and "projections" in out["reason"]


def test_a_predicted_keeper_is_not_also_draftable(test_session):
    """Otherwise the availability maths is wrong in the most direct way possible:
    the tool would tell you a player is on the board while also predicting the
    manager who owns him will keep him."""
    lg, mgrs, _ = _seed(test_session)
    _squad(test_session, lg, mgrs["A"], prefix="A", points=[100 - i for i in range(15)])
    _squad(test_session, lg, mgrs["B"], prefix="B", points=[90 - i for i in range(15)])
    star = _player(test_session, lg, "Star", "MID", 999, owner=mgrs["A"])
    for i in range(40):
        _player(test_session, lg, f"Pool{i}", ["GKP", "DEF", "MID", "FWD"][i % 4], 50 - i)

    out = services.draft_preparation(test_session, lg, YEAR)
    kept = {r.player_id for r in out["predictions"][mgrs["A"].id]["keepers"]}
    assert star.id in kept, "fixture didn't produce the keeper it needs"
    drafted = {r["player"].player_id for r in out["sim"]["picks"] if r["player"]}
    assert star.id not in drafted, "a predicted keeper was drafted anyway"
    assert star.id not in out["gone_by"]


# ---- the page -------------------------------------------------------------
def test_the_page_is_owner_only(test_session):
    """Same gate as /admin/players: a logged-in non-owner gets 403, not a redirect."""
    from fastapi.testclient import TestClient

    from auth import hash_password
    from main import app

    lg, mgrs, _ = _seed(test_session)
    mgrs["A"].password_hash = hash_password("pw")
    test_session.commit()

    client = TestClient(app, follow_redirects=False)
    assert client.get("/draft-prep").status_code in (303, 307)   # anonymous
    client.post("/login", data={"manager_id": "1", "password": "pw"})
    assert client.get("/draft-prep").status_code == 403


def test_the_page_renders_without_projections(test_session):
    """The empty state has to be a message, not a 500 — this is what a fresh
    deployment or a pre-import league looks like."""
    from fastapi.testclient import TestClient

    import auth
    from main import app

    lg, mgrs, _ = _seed(test_session)
    _player(test_session, lg, "Someone", "MID", None, owner=mgrs["A"])
    mgrs["A"].fpl_manager_id = auth.owner_entry_id()
    test_session.commit()

    from auth import hash_password
    mgrs["A"].password_hash = hash_password("pw")
    test_session.commit()
    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"manager_id": auth.owner_entry_id(), "password": "pw"})
    r = client.get("/draft-prep")
    assert r.status_code == 200
    assert b"No projections imported" in r.content


# ---- live mode ------------------------------------------------------------
# draft_preparation_live uses actual keeper_selections and DraftPick rows
# instead of predicting. These tests confirm the wiring, not the sim model.

def _seed_live(session):
    """League in draft phase (keepers revealed), with projections."""
    from models import DraftLottery, Gameweek, Standing
    lg = League(fpl_league_id="99", name="Live", season_year=2025, is_current=True,
                sync_locked=False, phase="draft", keepers_locked=True)
    session.add(lg)
    session.flush()
    for n in range(1, 39):
        session.add(Gameweek(number=n, league_id=lg.id))
    session.flush()
    mgrs = {}
    for i, name in enumerate(("X", "Y"), start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(100 + i),
                    name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add_all([
            Standing(league_id=lg.id, manager_id=m.id, rank=i,
                     total=50 - i, points_for=500 - i),
            DraftLottery(league_id=lg.id, manager_id=m.id, pick_result=i),
        ])
        mgrs[name] = m
    session.commit()
    return lg, mgrs


def _proj_player(session, lg, name, pos, points):
    from models import PlayerProjection, PlayerSeason
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=name, code=fid * 13, fpl_id=fid, position=pos,
               current_team="CHE", price=50, status="a")
    session.add(p)
    session.flush()
    session.add_all([
        PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fid,
                     name=name, position=pos, current_team="CHE"),
        PlayerProjection(season_year=YEAR, player_id=p.id, raw_name=name,
                         raw_team="CHE", raw_position=pos, price=5.0, points=points),
    ])
    session.commit()
    return p


def test_live_uses_actual_keeper_selections(test_session):
    """Actual submitted keepers appear in predictions, not the model's guess."""
    lg, mgrs = _seed_live(test_session)
    players = [_proj_player(test_session, lg, f"P{i}", pos, 200 - i * 10)
               for i, pos in enumerate(["GKP", "DEF", "DEF", "MID", "MID",
                                        "MID", "FWD", "FWD", "GKP", "DEF",
                                        "DEF", "MID", "MID", "FWD", "FWD"])]
    # Roster all for X so they're keeper-eligible
    for p in players:
        for n in range(1, 39):
            gw = test_session.query(Gameweek).filter_by(
                league_id=lg.id, number=n).first()
            test_session.add(Roster(manager_id=mgrs["X"].id,
                                    gameweek_id=gw.id, player_id=p.id))
    test_session.commit()

    # Select two specific players as X's keepers
    kept = players[:2]
    for p in kept:
        test_session.add(KeeperSelection(league_id=lg.id, manager_id=mgrs["X"].id,
                                         player_id=p.id, season_year=YEAR))
    test_session.commit()

    out = services.draft_preparation_live(test_session, lg, YEAR)
    assert out["available"]
    assert out["live"] is True
    actual = {r.player_id for r in out["predictions"][mgrs["X"].id]["keepers"]}
    assert actual == {p.id for p in kept}


def test_live_actual_picks_appear_and_are_excluded_from_pool(test_session):
    """DraftPick rows show up as 'actual=True' and remove the player from available."""
    from models import DraftPick as DP
    lg, mgrs = _seed_live(test_session)
    players = [_proj_player(test_session, lg, f"Q{i}", pos, 150 - i * 8)
               for i, pos in enumerate(["GKP", "DEF", "DEF", "MID", "MID",
                                        "MID", "FWD", "FWD", "GKP", "DEF",
                                        "DEF", "MID", "MID", "FWD", "FWD"])]

    drafted_player = players[3]   # some MID
    test_session.add(DP(league_id=lg.id, season_year=YEAR, draft_type="main",
                        round=1, pick_number=1, manager_id=mgrs["X"].id,
                        player_id=drafted_player.id))
    test_session.commit()

    out = services.draft_preparation_live(test_session, lg, YEAR)
    assert out["picks_made"] == 1

    # The drafted player must not be in the remaining available pool for the sim
    pool_ids = {r.player_id for r in out["pool"]}
    assert drafted_player.id in pool_ids, "player should be in the overall pool"
    # But the pick-1 row should be actual=True and not in the sim's available
    pick1 = next(r for r in out["sim"]["picks"] if r["pick"] == 1)
    assert pick1["actual"] is True
    assert pick1["player"] is not None
    assert pick1["player"].player_id == drafted_player.id


def test_live_gone_by_uses_real_pick_number(test_session):
    """gone_by for a drafted player is the actual pick number, not the sim's guess."""
    from models import DraftPick as DP
    lg, mgrs = _seed_live(test_session)
    players = [_proj_player(test_session, lg, f"R{i}", pos, 180 - i * 9)
               for i, pos in enumerate(["GKP", "DEF", "DEF", "MID", "MID",
                                        "MID", "FWD", "FWD", "GKP", "DEF",
                                        "DEF", "MID", "MID", "FWD", "FWD"])]

    target = players[5]   # any player
    test_session.add(DP(league_id=lg.id, season_year=YEAR, draft_type="main",
                        round=1, pick_number=3, manager_id=mgrs["Y"].id,
                        player_id=target.id))
    test_session.commit()

    out = services.draft_preparation_live(test_session, lg, YEAR)
    assert out["gone_by"].get(target.id) == 3


def test_live_simulation_seeded_from_actual_picks(test_session):
    """A player already drafted is not re-drafted in the simulation."""
    from models import DraftPick as DP
    lg, mgrs = _seed_live(test_session)
    players = [_proj_player(test_session, lg, f"S{i}", pos, 160 - i * 7)
               for i, pos in enumerate(["GKP", "DEF", "DEF", "MID", "MID",
                                        "MID", "FWD", "FWD", "GKP", "DEF",
                                        "DEF", "MID", "MID", "FWD", "FWD"])]

    taken = players[2]   # any DEF
    test_session.add(DP(league_id=lg.id, season_year=YEAR, draft_type="main",
                        round=1, pick_number=2, manager_id=mgrs["X"].id,
                        player_id=taken.id))
    test_session.commit()

    out = services.draft_preparation_live(test_session, lg, YEAR)
    sim_player_ids = {r["player"].player_id for r in out["sim"]["picks"]
                      if r["player"] and not r.get("actual")}
    assert taken.id not in sim_player_ids, "already-drafted player re-appeared in sim"
