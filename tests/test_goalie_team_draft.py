"""Drafting a goalie team: a club is an asset, and every manager ends with exactly one.

From 2026 a manager spends one of fourteen picks on a Premier League club and owns
every keeper at it. Three properties have to hold, and none of them is recoverable
after the fact — which is why they are enforced in `record_pick` rather than only in
the UI:

  - a club goes once,
  - a manager gets one goalie team,
  - a manager cannot spend their LAST slot on an outfielder while clubless.

The fourth property is a negative one, and just as deliberate: this did NOT turn
`record_pick` into a squad-quota enforcer. A sixth defender is still legal.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from auth import hash_password
from models import (
    DraftLottery,
    League,
    Manager,
    Player,
    PlayerSeason,
    PlTeam,
    Standing,
)
from rules import RuleViolation

UPCOMING = 2026

# short_name, code, name
CLUBS = [("ARS", 3, "Arsenal"), ("MCI", 43, "Man City"), ("LIV", 14, "Liverpool")]


def _seed(session, *, mode="redraft", managers=("A", "B"), outfielders=40):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="draft", goalie_team_mode=mode)
    session.add(lg)
    session.flush()

    mgrs = {}
    for i, name in enumerate(managers, start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i, total=10 - i,
                             points_for=100))
        session.add(DraftLottery(league_id=lg.id, manager_id=m.id, pick_result=i))
        mgrs[name] = m

    clubs = {}
    for short, code, name in CLUBS:
        t = PlTeam(code=code, fpl_id=code, short_name=short, name=name, is_current_pl=True)
        session.add(t)
        session.flush()
        clubs[short] = t

    players = {}
    fid = 1
    # Two keepers at every club, so a goalie team is a real bundle rather than a label.
    for short, _code, _name in CLUBS:
        for n in (1, 2):
            p = Player(name=f"{short} keeper {n}", code=fid * 101, fpl_id=fid,
                       position="GKP", current_team=short, total_points=50 * n)
            session.add(p)
            players[p.name] = p
            fid += 1
    for i in range(outfielders):
        p = Player(name=f"Outfielder {i:02d}", code=fid * 101, fpl_id=fid,
                   position=["DEF", "MID", "FWD"][i % 3], current_team="ARS")
        session.add(p)
        players[p.name] = p
        fid += 1
    session.commit()
    return lg, mgrs, clubs, players


def _pick(session, lg, *, pick_number, owner_fpl, **kw):
    return services.record_pick(
        session, lg, season_year=UPCOMING, pick_number=pick_number,
        owner_fpl=owner_fpl, **kw,
    )


def _outfield_fpl_ids(players):
    return sorted(p.fpl_id for p in players.values() if p.position != "GKP")


# ---- the board -------------------------------------------------------------
def test_the_board_is_fourteen_rounds(test_session):
    lg, _, _, _ = _seed(test_session)
    board = services.get_draft_board(test_session, lg, UPCOMING)
    assert max(b["round"] for b in board) == 14
    assert len(board) == 28


def test_with_the_rule_off_the_board_is_still_fifteen(test_session):
    """The archive's guarantee, asserted through the real board."""
    lg, _, _, _ = _seed(test_session, mode="off")
    board = services.get_draft_board(test_session, lg, UPCOMING)
    assert max(b["round"] for b in board) == 15


# ---- drafting a club -------------------------------------------------------
def test_a_club_can_be_drafted_and_shows_on_the_board(test_session):
    lg, _, _, _ = _seed(test_session)
    out = _pick(test_session, lg, pick_number=1, owner_fpl="1", team_code=3)
    assert out["player"] == "Arsenal"

    board = services.get_draft_board(test_session, lg, UPCOMING)
    row = next(b for b in board if b["pick"] == 1)
    assert row["player"] == "Arsenal" and row["is_goalie_team"] is True


def test_a_recorded_club_advances_the_clock(test_session):
    """`next_open_pick` treats a falsy `player` as still-on-the-clock. If a club pick
    rendered blank the draft would never complete, and nobody would see why."""
    lg, _, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", team_code=3)
    board = services.get_draft_board(test_session, lg, UPCOMING)
    assert services.next_open_pick(board)["pick"] == 2


def test_a_club_goes_once(test_session):
    lg, _, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", team_code=3)
    with pytest.raises(RuleViolation, match="already drafted by A"):
        _pick(test_session, lg, pick_number=2, owner_fpl="2", team_code=3)


def test_a_manager_gets_one_goalie_team(test_session):
    lg, _, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", team_code=3)
    with pytest.raises(RuleViolation, match="second goalie team"):
        _pick(test_session, lg, pick_number=3, owner_fpl="1", team_code=43)


def test_a_pick_names_exactly_one_thing(test_session):
    lg, _, _, players = _seed(test_session)
    both = _outfield_fpl_ids(players)[0]
    with pytest.raises(RuleViolation, match="exactly one"):
        _pick(test_session, lg, pick_number=1, owner_fpl="1",
              player_fpl_id=both, team_code=3)
    with pytest.raises(RuleViolation, match="exactly one"):
        _pick(test_session, lg, pick_number=1, owner_fpl="1")


def test_a_club_is_not_draftable_with_the_rule_off(test_session):
    lg, _, _, _ = _seed(test_session, mode="off")
    with pytest.raises(RuleViolation, match="aren't drafted in this league"):
        _pick(test_session, lg, pick_number=1, owner_fpl="1", team_code=3)


# ---- the reserved last slot ------------------------------------------------
def _fill_all_but_last(session, lg, players, owner_fpl):
    """Spend every slot this manager owns except one, all on outfielders."""
    board = services.get_draft_board(session, lg, UPCOMING)
    mine = [b for b in board if b["owner_fpl"] == owner_fpl]
    ids = _outfield_fpl_ids(players)
    for slot, fid in zip(mine[:-1], ids):
        _pick(session, lg, pick_number=slot["pick"], owner_fpl=owner_fpl,
              player_fpl_id=fid)
    return mine[-1], ids[len(mine) - 1:]


def _run_board_to_last_slot_of(session, lg, players, owner_fpl):
    """Draft the whole board out until `owner_fpl` is on the clock for their final,
    still-clubless pick — the state the autodraft has to get right.

    Picks are interleaved, so simply filling one manager's slots leaves the OTHER
    manager on the clock and the autodraft never reaches the reserved slot. Everyone
    else takes a club early so the reserve rule doesn't fire for them too.
    """
    board = services.get_draft_board(session, lg, UPCOMING)
    target = [b for b in board if b["owner_fpl"] == owner_fpl][-1]
    ids = iter(_outfield_fpl_ids(players))
    clubs = iter([c[1] for c in CLUBS])
    gave_club = set()
    for b in board:
        if b["pick"] >= target["pick"]:
            break
        if b["owner_fpl"] != owner_fpl and b["owner_fpl"] not in gave_club:
            gave_club.add(b["owner_fpl"])
            _pick(session, lg, pick_number=b["pick"], owner_fpl=b["owner_fpl"],
                  team_code=next(clubs))
        else:
            _pick(session, lg, pick_number=b["pick"], owner_fpl=b["owner_fpl"],
                  player_fpl_id=next(ids))
    on_clock = services.next_open_pick(services.get_draft_board(session, lg, UPCOMING))
    assert on_clock["pick"] == target["pick"], "fixture didn't reach the reserved slot"
    return target, list(ids)


def test_the_last_slot_must_be_a_club(test_session):
    lg, _, _, players = _seed(test_session)
    last, spare = _fill_all_but_last(test_session, lg, players, "1")
    with pytest.raises(RuleViolation, match="one pick left and no goalie team"):
        _pick(test_session, lg, pick_number=last["pick"], owner_fpl="1",
              player_fpl_id=spare[0])


def test_the_last_slot_accepts_a_club(test_session):
    """The other half — without this the test above passes if the guard refuses
    everything."""
    lg, _, _, players = _seed(test_session)
    last, _spare = _fill_all_but_last(test_session, lg, players, "1")
    out = _pick(test_session, lg, pick_number=last["pick"], owner_fpl="1", team_code=3)
    assert out["player"] == "Arsenal"

    board = services.get_draft_board(test_session, lg, UPCOMING)
    a_picks = [b for b in board if b["owner_fpl"] == "1" and b["player"]]
    assert len(a_picks) == 14
    assert sum(1 for b in a_picks if b["is_goalie_team"]) == 1


def test_a_manager_who_already_has_a_club_may_spend_their_last_slot_freely(test_session):
    lg, _, _, players = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", team_code=3)
    board = services.get_draft_board(test_session, lg, UPCOMING)
    mine = [b for b in board if b["owner_fpl"] == "1"]
    ids = _outfield_fpl_ids(players)
    for slot, fid in zip(mine[1:], ids):
        _pick(test_session, lg, pick_number=slot["pick"], owner_fpl="1", player_fpl_id=fid)
    assert len([b for b in services.get_draft_board(test_session, lg, UPCOMING)
                if b["owner_fpl"] == "1" and b["player"]]) == 14


def test_the_reserve_rule_does_not_fire_early(test_session):
    """Two slots left is not one. Without this the guard could hold a manager's last
    TWO picks hostage and nobody would notice until a live draft."""
    lg, _, _, players = _seed(test_session)
    board = services.get_draft_board(test_session, lg, UPCOMING)
    mine = [b for b in board if b["owner_fpl"] == "1"]
    ids = _outfield_fpl_ids(players)
    for slot, fid in zip(mine[:-2], ids):
        _pick(test_session, lg, pick_number=slot["pick"], owner_fpl="1", player_fpl_id=fid)
    out = _pick(test_session, lg, pick_number=mine[-2]["pick"], owner_fpl="1",
                player_fpl_id=ids[len(mine) - 2])
    assert out["player"].startswith("Outfielder")


def test_the_reserve_rule_is_per_manager(test_session):
    """A's empty slots must not constrain B."""
    lg, _, _, players = _seed(test_session)
    _fill_all_but_last(test_session, lg, players, "1")
    ids = _outfield_fpl_ids(players)
    b_slot = next(b for b in services.get_draft_board(test_session, lg, UPCOMING)
                  if b["owner_fpl"] == "2" and not b["player"])
    out = _pick(test_session, lg, pick_number=b_slot["pick"], owner_fpl="2",
                player_fpl_id=ids[-1])
    assert out["player"].startswith("Outfielder")


# ---- what did NOT change ---------------------------------------------------
def test_a_sixth_defender_is_still_legal(test_session):
    """record_pick has never enforced squad quotas and still doesn't. If this ever
    fails, the goalie-team guards quietly grew into a position checker."""
    lg, _, _, players = _seed(test_session)
    defs = sorted(p.fpl_id for p in players.values() if p.position == "DEF")
    assert len(defs) >= 6
    for i, fid in enumerate(defs[:6], start=1):
        _pick(test_session, lg, pick_number=(i * 2) - 1, owner_fpl="1", player_fpl_id=fid)
    board = services.get_draft_board(test_session, lg, UPCOMING)
    assert len([b for b in board if b["owner_fpl"] == "1" and b["player"]]) == 6


# ---- search ----------------------------------------------------------------
def test_goalkeepers_are_off_the_board(test_session):
    lg, _, _, _ = _seed(test_session)
    rows = services.search_players(test_session, lg, q="keeper", available_year=UPCOMING,
                                   include_taken=True, limit=100)
    assert not [r for r in rows if r["position"] == "GKP"]


def test_goalkeepers_are_still_searchable_with_the_rule_off(test_session):
    lg, _, _, _ = _seed(test_session, mode="off")
    rows = services.search_players(test_session, lg, q="keeper", available_year=UPCOMING,
                                   include_taken=True, limit=100)
    assert [r for r in rows if r["position"] == "GKP"]


def test_the_team_filter_returns_clubs_with_their_keepers(test_session):
    lg, _, _, _ = _seed(test_session)
    rows = services.search_players(test_session, lg, position="TEAM",
                                   available_year=UPCOMING, limit=100)
    assert {r["name"] for r in rows} == {"Arsenal", "Man City", "Liverpool"}
    ars = next(r for r in rows if r["team"] == "ARS")
    assert ars["kind"] == "team" and ars["team_code"] == 3
    assert ars["keepers"] == ["ARS keeper 1", "ARS keeper 2"]
    assert ars["fpl_id"] is None


def test_searching_a_club_by_name_finds_it(test_session):
    lg, _, _, _ = _seed(test_session)
    rows = services.search_players(test_session, lg, q="Arsenal", available_year=UPCOMING,
                                   limit=100)
    assert rows[0]["kind"] == "team" and rows[0]["name"] == "Arsenal"


def test_a_drafted_club_shows_who_took_it(test_session):
    lg, _, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", team_code=3)
    rows = services.search_players(test_session, lg, position="TEAM",
                                   available_year=UPCOMING, include_taken=True, limit=100)
    ars = next(r for r in rows if r["team"] == "ARS")
    assert ars["taken"] and ars["taken_by"] == "drafted: A"
    # and is gone entirely when taken rows are excluded
    open_rows = services.search_players(test_session, lg, position="TEAM",
                                        available_year=UPCOMING, limit=100)
    assert "Arsenal" not in {r["name"] for r in open_rows}


def test_an_owner_sees_every_club_as_taken(test_session):
    """Search mirrors record_pick. A manager with a club must not be offered a Draft
    button that the write path would then refuse."""
    lg, mgrs, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=1, owner_fpl="1", team_code=3)
    rows = services.search_players(test_session, lg, position="TEAM",
                                   available_year=UPCOMING, include_taken=True,
                                   for_manager_id=mgrs["A"].id, limit=100)
    assert all(r["taken"] for r in rows)
    assert next(r for r in rows if r["team"] == "MCI")["taken_by"] == (
        "you already have a goalie team")
    # ...but not for the manager who doesn't have one
    other = services.search_players(test_session, lg, position="TEAM",
                                    available_year=UPCOMING, include_taken=True,
                                    for_manager_id=mgrs["B"].id, limit=100)
    assert not next(r for r in other if r["team"] == "MCI")["taken"]


def test_a_club_reports_its_keepers_aggregate_points(test_session):
    """What you're buying is the club's whole keeper room, so the number shown is the
    sum — not the starter's, and not an average."""
    lg, _, clubs, players = _seed(test_session)
    # A completed season with stats is what stats_season() falls back to while drafting.
    prior = League(fpl_league_id="0", name="prior", season_year=2024, is_current=False,
                   sync_locked=True, phase="offseason")
    test_session.add(prior)
    test_session.flush()
    for name, pts in [("ARS keeper 1", 120), ("ARS keeper 2", 35), ("MCI keeper 1", 90)]:
        p = players[name]
        test_session.add(PlayerSeason(
            league_id=prior.id, player_id=p.id, fpl_id=p.fpl_id, name=p.name,
            position="GKP", current_team=p.current_team, total_points=pts,
        ))
    test_session.commit()

    rows = services.search_players(test_session, lg, position="TEAM",
                                   available_year=UPCOMING, limit=100)
    by_team = {r["team"]: r for r in rows}
    assert by_team["ARS"]["points"] == 155     # 120 + 35, both keepers
    assert by_team["MCI"]["points"] == 90      # the one with a snapshot
    assert by_team["LIV"]["points"] is None    # no snapshot at all, not zero


# ---- queue -----------------------------------------------------------------
def test_a_club_can_be_queued_alongside_players(test_session):
    lg, _, _, players = _seed(test_session)
    fid = _outfield_fpl_ids(players)[0]
    services.add_to_queue(test_session, lg, fpl_manager_id="1", player_fpl_id=fid,
                          season_year=UPCOMING)
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                          season_year=UPCOMING)
    q = services.get_draft_queue(test_session, lg, "1", UPCOMING)
    assert [e["kind"] for e in q] == ["player", "team"]
    assert q[1]["name"] == "Arsenal" and q[1]["team_code"] == 3


def test_queuing_a_club_is_idempotent(test_session):
    lg, _, _, _ = _seed(test_session)
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                          season_year=UPCOMING)
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                          season_year=UPCOMING)
    assert len(services.get_draft_queue(test_session, lg, "1", UPCOMING)) == 1


def test_a_queued_club_can_be_removed(test_session):
    lg, _, _, _ = _seed(test_session)
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                          season_year=UPCOMING)
    services.remove_from_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                               season_year=UPCOMING)
    assert services.get_draft_queue(test_session, lg, "1", UPCOMING) == []


def test_autodraft_takes_a_queued_club(test_session):
    lg, _, _, _ = _seed(test_session)
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                          season_year=UPCOMING)
    out = services.approve_queued_pick(test_session, lg, season_year=UPCOMING)
    assert out["player"] == "Arsenal"
    assert services.get_draft_queue(test_session, lg, "1", UPCOMING) == []


def test_autodraft_skips_a_club_someone_else_took(test_session):
    lg, _, _, _ = _seed(test_session)
    _pick(test_session, lg, pick_number=2, owner_fpl="2", team_code=3)
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                          season_year=UPCOMING)
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=43,
                          season_year=UPCOMING)
    out = services.approve_queued_pick(test_session, lg, season_year=UPCOMING)
    assert out["player"] == "Man City"


def test_autodraft_at_a_reserved_slot_skips_queued_outfielders(test_session):
    """The autodraft has to respect the reserved last slot too — otherwise the one
    manager who ISN'T at their keyboard is the one who ends up without a club."""
    lg, _, _, players = _seed(test_session)
    last, spare = _run_board_to_last_slot_of(test_session, lg, players, "1")
    services.add_to_queue(test_session, lg, fpl_manager_id="1", player_fpl_id=spare[0],
                          season_year=UPCOMING)
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=43,
                          season_year=UPCOMING)
    out = services.approve_queued_pick(test_session, lg, season_year=UPCOMING)
    assert out["pick"] == last["pick"] and out["player"] == "Man City"


def test_autodraft_at_a_reserved_slot_with_no_queued_club_says_why(test_session):
    lg, _, _, players = _seed(test_session)
    _last, spare = _run_board_to_last_slot_of(test_session, lg, players, "1")
    services.add_to_queue(test_session, lg, fpl_manager_id="1", player_fpl_id=spare[0],
                          season_year=UPCOMING)
    with pytest.raises(RuleViolation, match="no goalie team is queued"):
        services.approve_queued_pick(test_session, lg, season_year=UPCOMING)


def test_autodraft_never_treats_a_departed_player_as_available(test_session):
    """Club rows carry fpl_id None. Keying availability on fpl_id alone would put
    None in that set, which every DEPARTED player also matches."""
    lg, _, _, players = _seed(test_session)
    gone = players["Outfielder 00"]
    gone.fpl_id = None
    test_session.commit()
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                          season_year=UPCOMING)
    out = services.approve_queued_pick(test_session, lg, season_year=UPCOMING)
    assert out["player"] == "Arsenal"


# ---- pick trades -----------------------------------------------------------
def test_trading_away_the_last_slot_is_refused(test_session):
    lg, _, _, players = _seed(test_session)
    last, _spare = _fill_all_but_last(test_session, lg, players, "1")
    with pytest.raises(RuleViolation, match="would strand them"):
        services.trade_pick(test_session, lg, from_fpl="1", to_fpl="2",
                            original_fpl="1", round=last["round"],
                            season_year=UPCOMING)


def test_an_ordinary_pick_trade_still_works(test_session):
    lg, _, _, _ = _seed(test_session)
    out = services.trade_pick(test_session, lg, from_fpl="1", to_fpl="2",
                              original_fpl="1", round=3, season_year=UPCOMING)
    assert out["to"] == "B"


# ---- the pages actually render --------------------------------------------
@pytest.fixture
def client(test_session):
    """A TestClient sharing the test database (conftest patches db.SessionLocal,
    which get_db resolves at call time) — never the configured one."""
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _login(client, session, manager, password="pw"):
    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text
    return client


def test_the_draft_page_offers_goalie_teams_instead_of_goalkeepers(client, test_session):
    lg, mgrs, _, _ = _seed(test_session)
    _login(client, test_session, mgrs["A"])
    body = client.get(f"/draft/{UPCOMING}").text
    assert '<option value="TEAM">Goalie teams</option>' in body
    assert "<option>GKP</option>" not in body


def test_the_draft_page_still_offers_gkp_with_the_rule_off(client, test_session):
    lg, mgrs, _, _ = _seed(test_session, mode="off")
    _login(client, test_session, mgrs["A"])
    body = client.get(f"/draft/{UPCOMING}").text
    assert "<option>GKP</option>" in body
    assert 'value="TEAM"' not in body


def test_a_club_row_renders_a_draft_button_that_posts_team_code(client, test_session):
    """The whole wire format in one assertion: a club is picked by `team_code`, and
    nothing on that row carries an fpl_id."""
    lg, mgrs, _, _ = _seed(test_session)
    _login(client, test_session, mgrs["B"])
    body = client.get(f"/draft/{UPCOMING}/search?position=TEAM").text
    assert "Arsenal" in body and "ARS keeper 1, ARS keeper 2" in body
    assert '"team_code": 3' in body
    assert '"player_fpl_id": None' not in body


def test_picking_a_club_through_the_route_lands_on_the_board(client, test_session):
    lg, mgrs, _, _ = _seed(test_session)
    on_clock = services.next_open_pick(services.get_draft_board(test_session, lg, UPCOMING))
    owner = next(m for m in mgrs.values() if m.fpl_manager_id == on_clock["owner_fpl"])
    _login(client, test_session, owner)
    r = client.post(f"/draft/{UPCOMING}/pick", data={"team_code": 3})
    assert r.status_code == 200, r.text
    assert "Arsenal" in r.text and "goalie team" in r.text

    board = services.get_draft_board(test_session, lg, UPCOMING)
    assert next(b for b in board if b["pick"] == on_clock["pick"])["player"] == "Arsenal"


def test_a_refused_club_pick_shows_why(client, test_session):
    lg, mgrs, _, _ = _seed(test_session)
    on_clock = services.next_open_pick(services.get_draft_board(test_session, lg, UPCOMING))
    owner = next(m for m in mgrs.values() if m.fpl_manager_id == on_clock["owner_fpl"])
    _pick(test_session, lg, pick_number=on_clock["pick"], owner_fpl=owner.fpl_manager_id,
          team_code=3)
    _login(client, test_session, owner)
    nxt = services.next_open_pick(services.get_draft_board(test_session, lg, UPCOMING))
    other = next(m for m in mgrs.values() if m.fpl_manager_id == nxt["owner_fpl"])
    client.post("/logout")
    _login(client, test_session, other)
    r = client.post(f"/draft/{UPCOMING}/pick", data={"team_code": 3})
    assert r.status_code == 200
    assert "already drafted by" in r.text


def test_a_queued_club_renders_with_a_remove_form(client, test_session):
    lg, mgrs, _, _ = _seed(test_session)
    _login(client, test_session, mgrs["A"])
    services.add_to_queue(test_session, lg, fpl_manager_id="1", team_code=3,
                          season_year=UPCOMING)
    body = client.get(f"/draft/{UPCOMING}/queue").text
    assert "Arsenal" in body and "goalie team" in body
    assert 'name="team_code" value="3"' in body
