"""The draft-prep page under the goalie-team rule: a club board, and no keeper board.

The pure model is covered in tests/test_draftprep_goalie_shape.py. This is the wiring
around it — the three things that go wrong when a whole asset class moves out of the
player pool and into its own table:

  - goalkeepers reading as "excluded (no projection)", burying the handful of genuine
    gaps under eighty names;
  - a rostered goalkeeper reading as "left the Premier League", which looks like data
    corruption and isn't;
  - a manager's simulated squad flagged red for being 13 instead of 15.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DraftLottery,
    Gameweek,
    League,
    Manager,
    Player,
    PlayerProjection,
    PlayerSeason,
    PlTeam,
    Roster,
    Standing,
)

YEAR = 2026
CLUBS = [("ARS", 3, "Arsenal"), ("MCI", 43, "Man City"), ("LIV", 14, "Liverpool")]
_FPL = [0]


def _seed(session, *, mode="redraft"):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=True, phase="offseason", goalie_team_mode=mode)
    session.add(lg)
    session.flush()
    gw = Gameweek(number=38, league_id=lg.id)
    session.add(gw)
    session.flush()

    mgrs = {}
    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add_all([
            Standing(league_id=lg.id, manager_id=m.id, rank=i, total=100 - i,
                     points_for=1000 - i),
            DraftLottery(league_id=lg.id, manager_id=m.id, pick_result=i),
        ])
        mgrs[name] = m

    for short, code, name in CLUBS:
        session.add(PlTeam(code=code, fpl_id=code, short_name=short, name=name,
                           is_current_pl=True))
    session.commit()
    return lg, mgrs, gw


def _player(session, lg, gw, name, pos, points, *, team="ARS", owner=None):
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=name, code=fid * 7, fpl_id=fid, position=pos, current_team=team,
               price=50, status="a")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fid, name=name,
                             position=pos, current_team=team))
    if points is not None:
        session.add(PlayerProjection(season_year=YEAR, player_id=p.id, raw_name=name,
                                     raw_team=team, raw_position=pos, price=5.0,
                                     points=points))
    if owner is not None:
        session.add(Roster(manager_id=owner.id, gameweek_id=gw.id, player_id=p.id))
    session.commit()
    return p


def _full_pool(session, lg, gw, mgrs):
    """Each manager's 15-man 25/26 squad (2 keepers + 13 outfielders), plus keepers at
    the other clubs so every goalie team is a real bundle."""
    shape = ["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    for who, mgr in mgrs.items():
        for i, pos in enumerate(shape):
            _player(session, lg, gw, f"{who}{i:02d}", pos, 200 - i, owner=mgr)
    # a free pool to draft from
    for i in range(40):
        _player(session, lg, gw, f"Pool{i:02d}", ["DEF", "MID", "FWD"][i % 3], 150 - i)
    # keepers at the other two clubs, unowned
    for short, _code, _name in CLUBS[1:]:
        for n in (1, 2):
            _player(session, lg, gw, f"{short} GK{n}", "GKP", 100 * n, team=short)


@pytest.fixture(autouse=True)
def _reset_ids():
    _FPL[0] = 0
    yield


# ---- the club board --------------------------------------------------------
def test_the_club_board_ranks_every_current_club(test_session):
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    out = services.draft_preparation(test_session, lg, YEAR)
    assert out["available"], out.get("reason")

    board = out["goalie_teams"]
    assert {c["short_name"] for c in board["clubs"]} == {"ARS", "MCI", "LIV"}
    # sorted best-first by value
    assert [c["value"] for c in board["clubs"]] == sorted(
        (c["value"] for c in board["clubs"]), reverse=True)


def test_a_clubs_projection_is_its_whole_keeper_room(test_session):
    """The sum, not the starter's — the manager gets both."""
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    out = services.draft_preparation(test_session, lg, YEAR)
    mci = next(c for c in out["goalie_teams"]["clubs"] if c["short_name"] == "MCI")
    assert mci["points"] == 300          # 100 + 200
    assert [k["name"] for k in mci["keepers"]] == ["MCI GK1", "MCI GK2"]
    assert [k["points"] for k in mci["keepers"]] == [100.0, 200.0]


def test_there_is_no_club_board_with_the_rule_off(test_session):
    lg, mgrs, gw = _seed(test_session, mode="off")
    _full_pool(test_session, lg, gw, mgrs)
    out = services.draft_preparation(test_session, lg, YEAR)
    assert out["goalie_teams"] is None


# ---- goalkeepers leave the player model ------------------------------------
def test_goalkeepers_are_not_in_the_drafted_pool(test_session):
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    out = services.draft_preparation(test_session, lg, YEAR)
    assert not [r for r in out["pool"] if r.position == "GKP"]
    assert "GKP" not in out["replacement"]


def test_goalkeepers_are_not_reported_as_excluded(test_session):
    """They aren't players the model failed to price — they're a different asset. If
    this regresses, the genuine gaps are buried under every keeper in the league."""
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    # one genuine gap: an outfielder with no projection at all
    _player(test_session, lg, gw, "NoProjection", "MID", None)
    out = services.draft_preparation(test_session, lg, YEAR)
    assert out["excluded"] == ["NoProjection"]


def test_a_rostered_goalkeeper_is_not_reported_as_departed(test_session):
    """"Left the Premier League" reads as data corruption. He didn't leave; he stopped
    being an individually keepable asset."""
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    out = services.draft_preparation(test_session, lg, YEAR)
    assert not out["departed"]
    assert sorted(out["off_board_keepers"]["A"]) == ["A00", "A01"]


def test_with_the_rule_off_goalkeepers_are_ordinary_players(test_session):
    lg, mgrs, gw = _seed(test_session, mode="off")
    _full_pool(test_session, lg, gw, mgrs)
    out = services.draft_preparation(test_session, lg, YEAR)
    assert [r for r in out["pool"] if r.position == "GKP"]
    assert "GKP" in out["replacement"]
    assert not out["off_board_keepers"]


# ---- the simulation --------------------------------------------------------
def test_every_manager_gets_a_reserved_slot_and_thirteen_outfielders(test_session):
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    out = services.draft_preparation(test_session, lg, YEAR)
    reserved = [r for r in out["sim"]["picks"] if r["reason"] == "goalie team"]
    assert len(reserved) == 2
    for mid, squad in out["sim"]["squads"].items():
        assert len(squad) == 13, out["names"][mid]


# ---- the page renders ------------------------------------------------------
@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _owner_login(client, session, manager):
    """Log in AS the owner — the page has two gates (the site-wide login middleware
    and is_owner), and patching only the second leaves the first redirecting."""
    import auth
    from auth import hash_password

    manager.fpl_manager_id = auth.owner_entry_id()
    manager.password_hash = hash_password("pw")
    session.commit()
    r = client.post("/login", data={"manager_id": auth.owner_entry_id(),
                                    "password": "pw"})
    assert r.status_code == 303, r.text


def test_the_page_shows_the_club_board_and_no_gkp_filter(client, test_session):
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    _owner_login(client, test_session, mgrs["A"])
    r = client.get("/draft-prep")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Goalie teams" in body
    assert "Man City" in body and "MCI GK1" in body
    assert "<option>GKP</option>" not in body
    # the ledger's squad column must not flag 13 as wrong
    assert 'class="num neg">13<' not in body


def test_the_page_still_shows_gkp_with_the_rule_off(client, test_session):
    lg, mgrs, gw = _seed(test_session, mode="off")
    _full_pool(test_session, lg, gw, mgrs)
    _owner_login(client, test_session, mgrs["A"])
    r = client.get("/draft-prep")
    assert r.status_code == 200, r.text
    body = r.text
    assert "<option>GKP</option>" in body
    assert "<h2>Goalie teams</h2>" not in body


# ---- live mode -------------------------------------------------------------
def _live(session, mgrs, gw):
    """Flip to the live assistant: keepers revealed, so the page reads real picks."""
    lg = session.query(League).one()
    lg.phase = "draft"
    session.commit()
    return lg


def test_live_mode_shows_a_recorded_club_pick_by_name(test_session):
    """The club isn't in the player pool, so without its own label the row renders
    blank and reads as a slot nobody has used."""
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    _live(test_session, mgrs, gw)
    board = services.get_draft_board(test_session, lg, YEAR)
    slot = services.next_open_pick(board)
    services.record_pick(test_session, lg, season_year=YEAR,
                         pick_number=slot["pick"], owner_fpl=slot["owner_fpl"],
                         team_code=3, round=slot["round"])

    out = services.draft_preparation_live(test_session, lg, YEAR)
    row = next(r for r in out["sim"]["picks"] if r["pick"] == slot["pick"])
    assert row["actual"] is True
    assert row["player"] is None and row["goalie_team"] == "Arsenal"


def test_live_mode_marks_who_owns_each_club(test_session):
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    _live(test_session, mgrs, gw)
    slot = services.next_open_pick(services.get_draft_board(test_session, lg, YEAR))
    services.record_pick(test_session, lg, season_year=YEAR,
                         pick_number=slot["pick"], owner_fpl=slot["owner_fpl"],
                         team_code=3, round=slot["round"])

    out = services.draft_preparation_live(test_session, lg, YEAR)
    clubs = {c["short_name"]: c for c in out["goalie_teams"]["clubs"]}
    assert clubs["ARS"]["owner"] == slot["owner"]
    assert clubs["MCI"]["owner"] is None


def test_live_mode_stops_reserving_a_slot_once_the_club_is_taken(test_session):
    """The reservation is a forecast until the pick is made, then it's a fact. Holding
    one back afterwards shows the manager finishing an outfielder short."""
    lg, mgrs, gw = _seed(test_session)
    _full_pool(test_session, lg, gw, mgrs)
    _live(test_session, mgrs, gw)
    slot = services.next_open_pick(services.get_draft_board(test_session, lg, YEAR))
    owner_fpl = slot["owner_fpl"]
    services.record_pick(test_session, lg, season_year=YEAR,
                         pick_number=slot["pick"], owner_fpl=owner_fpl,
                         team_code=3, round=slot["round"])

    out = services.draft_preparation_live(test_session, lg, YEAR)
    owner_id = next(m.id for m in mgrs.values() if m.fpl_manager_id == owner_fpl)
    other_id = next(m.id for m in mgrs.values() if m.fpl_manager_id != owner_fpl)
    reserved = [r for r in out["sim"]["picks"] if r["reason"] == "goalie team"]
    assert [r["manager"] for r in reserved] == [other_id]
    # both still end on thirteen outfielders
    assert len(out["sim"]["squads"][owner_id]) == 13
    assert len(out["sim"]["squads"][other_id]) == 13
