"""The in-progress squad view: /my-team and /teams render blank all preseason
because they read Roster snapshots, and the new league row has none until FPL's
first post-rollover sync. get_teams_in_progress / get_my_team_in_progress show
each manager's kept players UNION their draft picks so far instead.

The 2026 draft's keeper selections and DraftPick rows can live on a DIFFERENT
league row than the CURRENT one (pre-rollover, the draft runs on the outgoing
row) — the cross-row bridging via Manager.fpl_manager_id is the load-bearing
part of this fix, not just the union itself.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DraftPick,
    Gameweek,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerSeason,
    PlTeam,
    Roster,
)
from rules import KEEPER_FRESH_DRAFT

ALL_GWS = range(1, 39)
_FPL = [0]


def _next_fpl():
    _FPL[0] += 1
    return _FPL[0]


def _league(session, *, season_year, phase, is_current=True, goalie_team_mode="off"):
    lg = League(
        fpl_league_id=str(_next_fpl()), name=f"S{season_year}", season_year=season_year,
        is_current=is_current, sync_locked=not is_current, phase=phase,
        goalie_team_mode=goalie_team_mode,
    )
    session.add(lg)
    session.flush()
    return lg


def _gws(session, lg):
    gws = {}
    for n in ALL_GWS:
        g = Gameweek(number=n, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[n] = g
    session.commit()
    return gws


def _manager(session, lg, fpl_manager_id, name):
    m = Manager(league_id=lg.id, fpl_manager_id=fpl_manager_id, name=name, display_name=name)
    session.add(m)
    session.commit()
    return m


def _player(session, name, pos="MID", team="ARS"):
    fid = _next_fpl()
    p = Player(name=name, code=fid * 7, fpl_id=fid, position=pos, current_team=team)
    session.add(p)
    session.commit()
    return p


def _rostered_all_season(session, mgr, player, gws):
    for n in ALL_GWS:
        session.add(Roster(manager_id=mgr.id, player_id=player.id, gameweek_id=gws[n].id))
    session.commit()


def _kept(session, lg, mgr, player, season_year, *, is_discovery=False):
    session.add(KeeperSelection(
        league_id=lg.id, manager_id=mgr.id, player_id=player.id,
        season_year=season_year, is_discovery=is_discovery,
    ))
    session.commit()


def _drafted(session, lg, mgr, *, season_year, pick_number, player=None, team_id=None):
    session.add(DraftPick(
        league_id=lg.id, season_year=season_year, draft_type="main", round=1,
        pick_number=pick_number, manager_id=mgr.id,
        player_id=(player.id if player else None), team_id=team_id, source="draft",
    ))
    session.commit()


# ---- get_teams_in_progress: single-row (pre-rollover) cases ----------------

def test_kept_players_show_real_facts_and_picks_show_the_blank_convention(test_session):
    """A manager with keepers AND fresh picks: keepers carry real derived facts
    (acquisition/years/eligible), a freshly drafted player gets the fresh 4-year
    draft convention (never None — the shared _roster_card.html template does
    `years_remaining > 0`, which crashes on None)."""
    lg = _league(test_session, season_year=2025, phase="draft")
    gws = _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")

    kept = _player(test_session, "Kept Player")
    _rostered_all_season(test_session, mgr, kept, gws)
    _kept(test_session, lg, mgr, kept, 2026)

    fresh = _player(test_session, "Fresh Pick", pos="DEF")
    _drafted(test_session, lg, mgr, season_year=2026, pick_number=1, player=fresh)

    out = services.get_teams_in_progress(test_session, lg)
    row = next(t for t in out if t["manager"] == "Scott")
    by_name = {p["player"]: p for p in row["players"]}

    assert set(by_name) == {"Kept Player", "Fresh Pick"}
    assert by_name["Kept Player"]["acquisition"] == "draft"
    assert by_name["Kept Player"]["kept"] is True
    assert by_name["Fresh Pick"]["acquisition"] == "draft"
    assert by_name["Fresh Pick"]["years_remaining"] == KEEPER_FRESH_DRAFT
    assert by_name["Fresh Pick"]["eligible"] is True
    assert by_name["Fresh Pick"]["kept"] is False
    # never None: the template does `years_remaining > 0`
    for p in row["players"]:
        assert p["years_remaining"] is not None
        assert p["eligible"] is not None


def test_zero_picks_shows_only_keepers_no_placeholder_rows(test_session):
    lg = _league(test_session, season_year=2025, phase="draft")
    gws = _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    kept = _player(test_session, "Kept Player")
    _rostered_all_season(test_session, mgr, kept, gws)
    _kept(test_session, lg, mgr, kept, 2026)

    out = services.get_teams_in_progress(test_session, lg)
    row = next(t for t in out if t["manager"] == "Scott")
    assert [p["player"] for p in row["players"]] == ["Kept Player"]


def test_manager_with_nothing_yet_still_appears_with_empty_players(test_session):
    lg = _league(test_session, season_year=2025, phase="draft")
    _gws(test_session, lg)
    _manager(test_session, lg, "1", "Scott")

    out = services.get_teams_in_progress(test_session, lg)
    row = next(t for t in out if t["manager"] == "Scott")
    assert row["players"] == []


# ---- cross-row (post-rollover) ---------------------------------------------

def test_cross_row_selections_and_picks_bridge_to_the_current_managers(test_session):
    """The 2026 draft's data lives on the OLD (25/26) league row, with THAT row's
    own Manager ids. The current league is the NEW (26/27) row, whose Manager
    rows share the same fpl_manager_id but have different UUIDs. Both must still
    appear under the current row's manager."""
    old = _league(test_session, season_year=2025, phase="offseason", is_current=False)
    old_gws = _gws(test_session, old)
    old_mgr = _manager(test_session, old, "42", "Scott")

    kept = _player(test_session, "Kept Player")
    _rostered_all_season(test_session, old_mgr, kept, old_gws)
    _kept(test_session, old, old_mgr, kept, 2026)

    fresh = _player(test_session, "Fresh Pick", pos="DEF")
    _drafted(test_session, old, old_mgr, season_year=2026, pick_number=1, player=fresh)

    new = _league(test_session, season_year=2026, phase="preseason", is_current=True)
    new_mgr = _manager(test_session, new, "42", "Scott")  # same fpl_manager_id, new row

    out = services.get_teams_in_progress(test_session, new)
    row = next(t for t in out if t["manager"] == "Scott")
    assert row["manager_fpl"] == "42"
    assert {p["player"] for p in row["players"]} == {"Kept Player", "Fresh Pick"}
    # bridged under the NEW row's manager id, not the old one
    assert new_mgr.id != old_mgr.id


def test_a_manager_with_no_current_row_counterpart_is_not_dropped(test_session):
    old = _league(test_session, season_year=2025, phase="offseason", is_current=False)
    old_gws = _gws(test_session, old)
    old_mgr = _manager(test_session, old, "99", "Departed Manager")
    fresh = _player(test_session, "Orphan Pick")
    _drafted(test_session, old, old_mgr, season_year=2026, pick_number=1, player=fresh)

    new = _league(test_session, season_year=2026, phase="preseason", is_current=True)
    # deliberately no Manager row with fpl_manager_id="99" on `new`

    out = services.get_teams_in_progress(test_session, new)
    row = next(t for t in out if t["manager"] == "Departed Manager")
    assert [p["player"] for p in row["players"]] == ["Orphan Pick"]


# ---- goalie-team club picks -------------------------------------------------

def test_redraft_mode_club_pick_renders_with_the_club_name(test_session):
    lg = _league(test_session, season_year=2025, phase="draft", goalie_team_mode="redraft")
    _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    team = PlTeam(code=3, fpl_id=3, short_name="ARS", name="Arsenal", is_current_pl=True)
    test_session.add(team)
    test_session.commit()
    _drafted(test_session, lg, mgr, season_year=2026, pick_number=1, team_id=team.id)

    out = services.get_teams_in_progress(test_session, lg)
    row = next(t for t in out if t["manager"] == "Scott")
    assert [p["player"] for p in row["players"]] == ["Arsenal"]
    assert row["players"][0]["eligible"] is False  # not keepable in redraft mode


def test_keeper_mode_club_pick_shows_real_clock_via_club_status(test_session):
    lg = _league(test_session, season_year=2025, phase="draft", goalie_team_mode="keeper")
    _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    team = PlTeam(code=3, fpl_id=3, short_name="ARS", name="Arsenal", is_current_pl=True)
    test_session.add(team)
    test_session.commit()
    _drafted(test_session, lg, mgr, season_year=2026, pick_number=1, team_id=team.id)

    out = services.get_teams_in_progress(test_session, lg)
    row = next(t for t in out if t["manager"] == "Scott")
    assert [p["player"] for p in row["players"]] == ["Arsenal"]
    assert row["players"][0]["acquisition"] == "draft"
    assert row["players"][0]["years_remaining"] == KEEPER_FRESH_DRAFT


# ---- get_my_team_in_progress ------------------------------------------------

def test_get_my_team_in_progress_shows_kept_and_drafted_players(test_session):
    lg = _league(test_session, season_year=2025, phase="draft")
    gws = _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    kept = _player(test_session, "Kept Player", pos="MID")
    _rostered_all_season(test_session, mgr, kept, gws)
    _kept(test_session, lg, mgr, kept, 2026)
    fresh = _player(test_session, "Fresh Pick", pos="DEF")
    _drafted(test_session, lg, mgr, season_year=2026, pick_number=1, player=fresh)

    team = services.get_my_team_in_progress(test_session, lg, "1")
    assert team is not None
    assert team["manager"] == "Scott"
    assert team["gameweek"] is None
    names = {p["name"] for p in team["players"]}
    assert names == {"Kept Player", "Fresh Pick"}


def test_get_my_team_in_progress_none_for_unknown_manager(test_session):
    lg = _league(test_session, season_year=2025, phase="draft")
    _gws(test_session, lg)
    assert services.get_my_team_in_progress(test_session, lg, "nope") is None


# ---- regression: get_my_team / get_keepers unchanged after the refactor ----

def test_get_my_team_still_reads_the_real_roster_unchanged(test_session):
    """get_my_team must behave exactly as before the _rich_player_rows
    extraction: rows come from the FPL-synced roster (_squad_players, which
    requires a PlayerSeason snapshot), not the keeper/draft-pick union."""
    lg = _league(test_session, season_year=2025, phase="in_season")
    gws = _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    rostered = _player(test_session, "Rostered Player", pos="FWD")
    test_session.add(PlayerSeason(
        league_id=lg.id, player_id=rostered.id, fpl_id=rostered.fpl_id,
        name=rostered.name, position=rostered.position, current_team=rostered.current_team,
    ))
    test_session.commit()
    _rostered_all_season(test_session, mgr, rostered, gws)
    # a keeper selection alone (no roster row) must NOT appear via get_my_team
    off_roster = _player(test_session, "Off Roster Keeper")
    _kept(test_session, lg, mgr, off_roster, 2026, is_discovery=True)

    team = services.get_my_team(test_session, lg, "1")
    names = {p["name"] for p in team["players"]}
    assert names == {"Rostered Player"}


def test_get_keepers_unaffected_by_phase(test_session):
    """The public /v1 shape (get_keepers) must not change regardless of phase —
    only the ROUTES branch on phase, never the underlying service."""
    lg = _league(test_session, season_year=2025, phase="draft")
    gws = _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    kept = _player(test_session, "Kept Player")
    _rostered_all_season(test_session, mgr, kept, gws)
    _kept(test_session, lg, mgr, kept, 2026)

    before = services.get_keepers(test_session, lg)
    lg.phase = "in_season"
    test_session.commit()
    after = services.get_keepers(test_session, lg)
    assert before == after


# ---- route-level: phase regression + the public /v1 contract --------------

@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _auth_headers(monkeypatch):
    monkeypatch.setenv("SYNC_AUTH_TOKEN", "test-token-in-progress")
    return {"X-Auth-Token": "test-token-in-progress"}


def test_teams_route_uses_get_keepers_outside_draft_and_preseason(
    client, test_session, monkeypatch
):
    """A phase this fix doesn't target (in_season) must render exactly as before
    — proof the route only branches for draft/preseason."""
    lg = _league(test_session, season_year=2025, phase="in_season")
    gws = _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    kept = _player(test_session, "Kept Player")
    _rostered_all_season(test_session, mgr, kept, gws)
    _kept(test_session, lg, mgr, kept, 2026)

    r = client.get("/teams", headers=_auth_headers(monkeypatch))
    assert r.status_code == 200
    assert "Kept Player" in r.text


def test_teams_route_uses_in_progress_view_during_preseason(
    client, test_session, monkeypatch
):
    lg = _league(test_session, season_year=2026, phase="preseason")
    _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    fresh = _player(test_session, "Fresh Pick")
    _drafted(test_session, lg, mgr, season_year=2026, pick_number=1, player=fresh)

    r = client.get("/teams", headers=_auth_headers(monkeypatch))
    assert r.status_code == 200
    assert "Fresh Pick" in r.text


def test_v1_keepers_route_identical_regardless_of_phase(client, test_session, monkeypatch):
    """The unauthenticated /v1 API is a public contract: its output must not
    vary with league.phase, even though /teams and /my-team now do."""
    lg = _league(test_session, season_year=2025, phase="draft")
    gws = _gws(test_session, lg)
    mgr = _manager(test_session, lg, "1", "Scott")
    kept = _player(test_session, "Kept Player")
    _rostered_all_season(test_session, mgr, kept, gws)
    _kept(test_session, lg, mgr, kept, 2026)

    key = lg.fpl_league_id
    during_draft = client.get(f"/v1/leagues/{key}/keepers").json()

    lg.phase = "preseason"
    test_session.commit()
    during_preseason = client.get(f"/v1/leagues/{key}/keepers").json()

    assert during_draft == during_preseason
