"""The rules around the edges that a goalie team quietly breaks.

None of these are the draft or the keeper clock. They are the places where a rule
written for individually-owned goalkeepers becomes either wrong or unsatisfiable once
a club owns them:

  - a keeper signed in January is owned the instant he signs, so flagging him
    "added after the draft" puts a demonstrably-owned player on the ineligible report;
  - the discovery keeper may be ANY player, which is the one door left open into
    individual goalkeeper ownership;
  - the injury and international lists demand a same-position replacement, and the
    only goalkeepers you own are the ones you already have;
  - a traded club must move as ONE thing, and its clock must not reset on the way.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DraftLottery,
    DraftPick,
    Gameweek,
    League,
    Manager,
    Player,
    PlayerPoolSnapshot,
    PlayerSeason,
    PlTeam,
    Roster,
    Standing,
    Trade,
)
from rules import KEEPER_FRESH_DRAFT, RuleViolation

SEASON = 2026
UPCOMING = SEASON + 1
CLUBS = [("ARS", 3, "Arsenal"), ("MCI", 43, "Man City"), ("LIV", 14, "Liverpool")]
_FPL = [0]


def _seed(session, *, mode="keeper", season=SEASON):
    lg = League(fpl_league_id="1", name=f"S{season}", season_year=season,
                is_current=True, sync_locked=False, phase="offseason",
                goalie_team_mode=mode)
    session.add(lg)
    session.flush()
    gws = {}
    for n in range(1, 39):
        g = Gameweek(number=n, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[n] = g
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
    clubs = {}
    for short, code, name in CLUBS:
        t = PlTeam(code=code, fpl_id=code, short_name=short, name=name,
                   is_current_pl=True)
        session.add(t)
        session.flush()
        clubs[short] = t
    session.commit()
    return lg, mgrs, clubs, gws


def _player(session, lg, gws, name, pos, *, owner=None, team="ARS"):
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=name, code=fid * 7, fpl_id=fid, position=pos, current_team=team)
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fid, name=name,
                             position=pos, current_team=team))
    if owner is not None:
        for g in gws.values():
            session.add(Roster(manager_id=owner.id, gameweek_id=g.id, player_id=p.id))
    session.commit()
    return p


def _draft_club(session, lg, manager, code, *, season=SEASON, pick=1):
    team = session.query(PlTeam).filter_by(code=code).one()
    session.add(DraftPick(league_id=lg.id, season_year=season, draft_type="main",
                          round=1, pick_number=pick, manager_id=manager.id,
                          team_id=team.id, source="draft"))
    session.commit()
    return team


@pytest.fixture(autouse=True)
def _reset_ids():
    _FPL[0] = 0
    yield


# ---- ineligibility ---------------------------------------------------------
def _snapshot(session, lg, players):
    for p in players:
        session.add(PlayerPoolSnapshot(league_id=lg.id, fpl_id=p.fpl_id))
    session.commit()


def test_a_goalkeeper_signed_after_the_draft_is_not_ineligible(test_session):
    """He's owned the instant he signs — whoever has his club gets him, with no
    transaction. An 'ineligible' flag on a player somebody demonstrably owns is worse
    than no flag at all."""
    lg, mgrs, _clubs, gws = _seed(test_session)
    drafted = _player(test_session, lg, gws, "Drafted", "MID")
    _snapshot(test_session, lg, [drafted])
    new_gk = _player(test_session, lg, gws, "January Keeper", "GKP")
    new_fwd = _player(test_session, lg, gws, "January Striker", "FWD")

    services.flag_ineligible(test_session, lg)
    flagged = {r["name"] for r in services.ineligible_players(test_session, lg)}
    assert flagged == {"January Striker"}
    assert new_gk.name not in flagged and new_fwd.name in flagged


def test_a_goalkeeper_signed_after_the_draft_is_still_ineligible_with_the_rule_off(
    test_session,
):
    lg, mgrs, _clubs, gws = _seed(test_session, mode="off")
    drafted = _player(test_session, lg, gws, "Drafted", "MID")
    _snapshot(test_session, lg, [drafted])
    _player(test_session, lg, gws, "January Keeper", "GKP")

    services.flag_ineligible(test_session, lg)
    flagged = {r["name"] for r in services.ineligible_players(test_session, lg)}
    assert "January Keeper" in flagged


def test_a_defender_signed_after_the_draft_is_still_exempt(test_session):
    """The pre-existing exemption must survive — this rule added one, it didn't
    replace it."""
    lg, mgrs, _clubs, gws = _seed(test_session)
    drafted = _player(test_session, lg, gws, "Drafted", "MID")
    _snapshot(test_session, lg, [drafted])
    _player(test_session, lg, gws, "January Defender", "DEF")

    services.flag_ineligible(test_session, lg)
    flagged = {r["name"] for r in services.ineligible_players(test_session, lg)}
    assert "January Defender" not in flagged


# ---- the discovery keeper --------------------------------------------------
def test_a_goalkeeper_cannot_be_the_discovery_keeper(test_session):
    """The last door into individual goalkeeper ownership: the discovery keeper may
    be ANY player, so it bypasses the roster candidate list entirely."""
    lg, mgrs, _clubs, gws = _seed(test_session)
    for i in range(3):
        _player(test_session, lg, gws, f"Own{i}", "MID", owner=mgrs["A"])
    off_roster_gk = _player(test_session, lg, gws, "Somebody Else's Keeper", "GKP")

    with pytest.raises(RuleViolation, match="goalkeepers are kept as a club"):
        services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                keeper_fpl_ids=[], season_year=UPCOMING,
                                discovery_fpl_id=off_roster_gk.fpl_id)


def test_an_outfielder_is_still_a_valid_discovery_keeper(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    off_roster = _player(test_session, lg, gws, "Future Star", "FWD")
    out = services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                  keeper_fpl_ids=[], season_year=UPCOMING,
                                  discovery_fpl_id=off_roster.fpl_id)
    assert [k["player"] for k in out["keepers"]] == ["Future Star"]


# ---- injury / international lists ------------------------------------------
def test_a_goalkeeper_cannot_go_on_the_injury_list(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    gk1 = _player(test_session, lg, gws, "GK One", "GKP", owner=mgrs["A"])
    gk2 = _player(test_session, lg, gws, "GK Two", "GKP", owner=mgrs["A"])
    with pytest.raises(RuleViolation, match="aren't on the injury list"):
        services.place_on_il(test_session, lg, fpl_manager_id="1",
                             injured_fpl_id=gk1.fpl_id,
                             replacement_fpl_id=gk2.fpl_id, start_gw=5)


def test_a_goalkeeper_cannot_go_on_the_international_list(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    gk1 = _player(test_session, lg, gws, "GK One", "GKP", owner=mgrs["A"])
    gk2 = _player(test_session, lg, gws, "GK Two", "GKP", owner=mgrs["A"])
    with pytest.raises(RuleViolation, match="aren't on the international list"):
        services.place_on_intl(test_session, lg, fpl_manager_id="1",
                               away_fpl_id=gk1.fpl_id,
                               replacement_fpl_id=gk2.fpl_id, start_gw=5)


def test_an_outfielder_still_uses_the_injury_list_normally(test_session):
    """Guard against the goalkeeper rule refusing every IL move."""
    lg, mgrs, _clubs, gws = _seed(test_session)
    hurt = _player(test_session, lg, gws, "Hurt", "MID", owner=mgrs["A"])
    cover = _player(test_session, lg, gws, "Cover", "MID")
    out = services.place_on_il(test_session, lg, fpl_manager_id="1",
                               injured_fpl_id=hurt.fpl_id,
                               replacement_fpl_id=cover.fpl_id, start_gw=5)
    assert out["player"] == "Hurt"


def test_a_goalkeeper_can_still_be_ild_with_the_rule_off(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session, mode="off")
    gk1 = _player(test_session, lg, gws, "GK One", "GKP", owner=mgrs["A"])
    gk2 = _player(test_session, lg, gws, "GK Two", "GKP")
    out = services.place_on_il(test_session, lg, fpl_manager_id="1",
                               injured_fpl_id=gk1.fpl_id,
                               replacement_fpl_id=gk2.fpl_id, start_gw=5)
    assert out["player"] == "GK One"


# ---- trading a goalie team -------------------------------------------------
def test_a_club_can_be_traded_and_ownership_follows(test_session):
    lg, mgrs, clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    services.trade_goalie_team(test_session, lg, from_fpl="1", to_fpl="2", team_code=3)
    owner = services.goalie_team_owner(test_session, lg)
    assert owner[clubs["ARS"].id] == "2"


def test_a_trade_moves_the_club_as_one_row(test_session):
    """Never one row per goalkeeper: the roster-seeded ownership overlay would refuse
    every one of them, and the keeper chain would label them independently."""
    lg, mgrs, clubs, gws = _seed(test_session)
    for n in (1, 2, 3):
        _player(test_session, lg, gws, f"ARS GK{n}", "GKP", team="ARS")
    _draft_club(test_session, lg, mgrs["A"], 3)
    services.trade_goalie_team(test_session, lg, from_fpl="1", to_fpl="2", team_code=3)
    rows = test_session.query(Trade).filter(Trade.team_id.isnot(None)).all()
    assert len(rows) == 1 and rows[0].team_id == clubs["ARS"].id
    assert all(r.player_id is None for r in rows)


def test_a_traded_club_carries_its_clock_and_its_label(test_session):
    """A trade changes ownership and NOTHING else. If the clock reset here, trading a
    club out and back would launder a spent one clean."""
    lg, mgrs, clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3, season=SEASON - 2)
    _draft_club(test_session, lg, mgrs["A"], 3, season=SEASON - 1, pick=2)
    _draft_club(test_session, lg, mgrs["A"], 3, season=SEASON, pick=3)
    before = services.keeper_candidates(test_session, lg, "1")["goalie_team"]
    assert before["years_remaining"] == KEEPER_FRESH_DRAFT - 2

    services.trade_goalie_team(test_session, lg, from_fpl="1", to_fpl="2", team_code=3)
    after = services.keeper_candidates(test_session, lg, "2")["goalie_team"]
    assert after["player"] == "Arsenal"
    assert after["years_remaining"] == before["years_remaining"]
    assert after["acquisition"] == before["acquisition"]
    assert services.keeper_candidates(test_session, lg, "1")["goalie_team"] is None


def test_a_trade_in_the_wrong_direction_fails_closed(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    with pytest.raises(RuleViolation, match="doesn't hold Arsenal"):
        services.trade_goalie_team(test_session, lg, from_fpl="2", to_fpl="1",
                                   team_code=3)


def test_a_one_way_trade_to_someone_who_has_a_club_is_refused(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3, pick=1)
    _draft_club(test_session, lg, mgrs["B"], 43, pick=2)
    with pytest.raises(RuleViolation, match="already has a goalie team"):
        services.trade_goalie_team(test_session, lg, from_fpl="1", to_fpl="2",
                                   team_code=3)


def test_two_managers_can_swap_clubs(test_session):
    """The swap has to be judged against the state before EITHER leg — sequencing it
    leaves the receiver momentarily holding two and the second leg refuses."""
    lg, mgrs, clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3, pick=1)
    _draft_club(test_session, lg, mgrs["B"], 43, pick=2)
    services.record_trade(test_session, lg, a_fpl="1", b_fpl="2",
                          a_players=[], b_players=[], a_picks=[], b_picks=[],
                          a_clubs=["3"], b_clubs=["43"])
    owner = services.goalie_team_owner(test_session, lg)
    assert owner[clubs["ARS"].id] == "2" and owner[clubs["MCI"].id] == "1"


def test_a_club_must_be_traded_for_a_club(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3, pick=1)
    _draft_club(test_session, lg, mgrs["B"], 43, pick=2)
    with pytest.raises(RuleViolation, match="traded for a goalie team"):
        services.record_trade(test_session, lg, a_fpl="1", b_fpl="2",
                              a_players=[], b_players=[], a_picks=[], b_picks=[],
                              a_clubs=["3"], b_clubs=[])


def test_an_ordinary_trade_is_unaffected(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    p = _player(test_session, lg, gws, "Traded", "MID", owner=mgrs["A"])
    out = services.record_trade(test_session, lg, a_fpl="1", b_fpl="2",
                                a_players=[str(p.fpl_id)], b_players=[],
                                a_picks=[], b_picks=[])
    assert out["assets_moved"] == 1


def test_the_club_shows_up_as_a_tradeable_asset(test_session):
    lg, mgrs, clubs, gws = _seed(test_session)
    for n in (1, 2):
        _player(test_session, lg, gws, f"ARS GK{n}", "GKP", team="ARS")
    _draft_club(test_session, lg, mgrs["A"], 3)
    assets = services.manager_assets(test_session, lg, "1")
    assert assets["club"]["team_code"] == 3
    assert assets["club"]["keepers"] == ["ARS GK1", "ARS GK2"]
    assert services.manager_assets(test_session, lg, "2")["club"] is None


def test_no_club_asset_with_the_rule_off(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session, mode="off")
    _draft_club(test_session, lg, mgrs["A"], 3)
    assert services.manager_assets(test_session, lg, "1")["club"] is None


# ---- season-year alignment (Item 8: self-healed rollover) ------------------
def test_a_club_drafted_with_matching_season_year_resolves_to_owner(test_session):
    """Regression: before the rollover, league.season_year (2025) lagged the draft's
    season_year (2026), so goalie_team_owner filtered out all pre-rollover picks.
    After the rollover, league.season_year=2026 matches DraftPick.season_year=2026,
    and club ownership resolves correctly. This verifies the self-healing."""
    lg, mgrs, clubs, gws = _seed(test_session, season=2026)
    _draft_club(test_session, lg, mgrs["A"], 3, season=2026)
    owner = services.goalie_team_owner(test_session, lg)
    assert owner[clubs["ARS"].id] == "1"
