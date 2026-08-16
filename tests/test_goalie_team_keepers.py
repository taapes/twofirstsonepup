"""Keeping a goalie team, and no longer keeping a goalkeeper.

Two rules land together and they have to land together, because either one alone is
broken:

  - an individual goalkeeper stops being keepable the moment the rule is on. Without
    it a manager keeps Raya AND drafts Arsenal, owns him twice, and spends a slot on
    a player he already has;
  - under `goalie_team_mode = 'keeper'` the club itself becomes keepable, on its own
    clock — a club has no `rosters` rows, so none of the roster-continuity machinery
    that dates a player's tenure applies to it.

The rest is the places a club silently vanishes because the code assumes a keeper is
a person: an inner join on PlayerSeason, a player-keyed ownership map, and a rollover
that skips anything it can't resolve without saying so.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DraftLottery,
    DraftPick,
    Gameweek,
    KeeperSeed,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerSeason,
    PlTeam,
    Roster,
    Standing,
)
from rules import KEEPER_FRESH_DRAFT, RuleViolation

SEASON = 2026          # the season being played
UPCOMING = SEASON + 1  # the season keepers are being chosen for
CLUBS = [("ARS", 3, "Arsenal"), ("MCI", 43, "Man City"), ("LIV", 14, "Liverpool")]
_FPL = [0]


def _seed(session, *, mode="keeper", season=SEASON, fpl_league_id="1", current=True):
    lg = League(fpl_league_id=fpl_league_id, name=f"S{season}", season_year=season,
                is_current=current, sync_locked=False, phase="offseason",
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
        t = session.query(PlTeam).filter_by(code=code).one_or_none()
        if t is None:
            t = PlTeam(code=code, fpl_id=code, short_name=short, name=name,
                       is_current_pl=True)
            session.add(t)
            session.flush()
        clubs[short] = t
    session.commit()
    return lg, mgrs, clubs, gws


def _player(session, lg, gws, name, pos, *, owner=None, team="ARS"):
    """A player, rostered all season by `owner` — i.e. held from GW1, so the keeper
    derivation reads him as draft-acquired with a full clock."""
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


def _squad(session, lg, mgrs, gws):
    """Each manager holds two keepers and a few outfielders all season."""
    out = {}
    for who, mgr in mgrs.items():
        out[who] = {
            "gk": [_player(session, lg, gws, f"{who} GK{n}", "GKP", owner=mgr)
                   for n in (1, 2)],
            "out": [_player(session, lg, gws, f"{who} OF{n}", "MID", owner=mgr)
                    for n in range(6)],
        }
    return out


def _draft_club(session, lg, manager, code, *, season=SEASON, pick=1):
    """Record that `manager` drafted this club for `season` — how a goalie team is
    acquired, and the only thing the club clock is derived from."""
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


# ---- goalkeepers stop being keepable ---------------------------------------
def test_a_goalkeeper_is_not_a_keeper_candidate(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _squad(test_session, lg, mgrs, gws)
    cands = services.keeper_candidates(test_session, lg, "1")
    gks = [p for p in cands["players"] if p["position"] == "GKP"]
    assert gks, "fixture must actually contain goalkeepers"
    assert all(not p["eligible"] for p in gks)
    assert all(p["reason"] == "goalkeepers are kept as a club" for p in gks)


def test_a_goalkeeper_is_still_keepable_with_the_rule_off(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session, mode="off")
    _squad(test_session, lg, mgrs, gws)
    cands = services.keeper_candidates(test_session, lg, "1")
    gks = [p for p in cands["players"] if p["position"] == "GKP"]
    assert gks and all(p["eligible"] for p in gks)


def test_submitting_a_goalkeeper_is_refused_and_says_why(test_session):
    """The message matters. "4-year limit / dropped" sends a manager looking for a
    clock bug that isn't there."""
    lg, mgrs, _clubs, gws = _seed(test_session)
    squad = _squad(test_session, lg, mgrs, gws)
    gk = squad["A"]["gk"][0]
    with pytest.raises(RuleViolation, match="goalkeepers are kept as a club"):
        services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                keeper_fpl_ids=[gk.fpl_id], season_year=UPCOMING)


def test_an_outfielder_is_unaffected(test_session):
    """Guard against the goalkeeper rule refusing everything."""
    lg, mgrs, _clubs, gws = _seed(test_session)
    squad = _squad(test_session, lg, mgrs, gws)
    out = services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                  keeper_fpl_ids=[squad["A"]["out"][0].fpl_id],
                                  season_year=UPCOMING)
    assert len(out["keepers"]) == 1


# ---- the club clock --------------------------------------------------------
def test_a_drafted_club_starts_on_a_full_clock(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    cands = services.keeper_candidates(test_session, lg, "1")
    gt = cands["goalie_team"]
    assert gt["player"] == "Arsenal" and gt["acquisition"] == "draft"
    assert gt["years_remaining"] == KEEPER_FRESH_DRAFT and gt["eligible"]


def test_a_manager_with_no_club_has_no_goalie_team_entry(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    assert services.keeper_candidates(test_session, lg, "2")["goalie_team"] is None


def test_a_relegated_club_is_void(test_session):
    lg, mgrs, clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    clubs["ARS"].is_current_pl = False
    test_session.commit()
    gt = services.keeper_candidates(test_session, lg, "1")["goalie_team"]
    assert not gt["eligible"]
    assert "relegated" in gt["reason"]


def test_a_seeded_clock_overrides_the_derivation(test_session):
    """The rollover writes a KeeperSeed; the next season must read it rather than
    re-deriving four years from the original draft."""
    lg, mgrs, clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=mgrs["A"].id,
                                team_id=clubs["ARS"].id, years_remaining=1,
                                season_year=SEASON))
    test_session.commit()
    gt = services.keeper_candidates(test_session, lg, "1")["goalie_team"]
    assert gt["years_remaining"] == 1 and gt["eligible"]


def test_a_maxed_club_cannot_be_kept(test_session):
    lg, mgrs, clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=mgrs["A"].id,
                                team_id=clubs["ARS"].id, years_remaining=0,
                                season_year=SEASON))
    test_session.commit()
    assert not services.keeper_candidates(test_session, lg, "1")["goalie_team"]["eligible"]
    with pytest.raises(RuleViolation, match="ineligible"):
        services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                keeper_fpl_ids=[], season_year=UPCOMING,
                                keeper_team_code=3)


# ---- submitting a club -----------------------------------------------------
def test_a_club_can_be_kept_and_counts_toward_the_five(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    squad = _squad(test_session, lg, mgrs, gws)
    _draft_club(test_session, lg, mgrs["A"], 3)
    ids = [p.fpl_id for p in squad["A"]["out"][:4]]
    out = services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                  keeper_fpl_ids=ids, season_year=UPCOMING,
                                  keeper_team_code=3)
    assert len(out["keepers"]) == 5
    assert "Arsenal" in [k["player"] for k in out["keepers"]]

    # ...and a fifth player on top of the club is one too many
    with pytest.raises(RuleViolation, match="limit is 5"):
        services.submit_keepers(
            test_session, lg, fpl_manager_id="1",
            keeper_fpl_ids=[p.fpl_id for p in squad["A"]["out"][:5]],
            season_year=UPCOMING, keeper_team_code=3)


def test_you_cannot_keep_someone_elses_club(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    with pytest.raises(RuleViolation, match="not B's goalie team"):
        services.submit_keepers(test_session, lg, fpl_manager_id="2",
                                keeper_fpl_ids=[], season_year=UPCOMING,
                                keeper_team_code=3)


def test_a_club_cannot_be_kept_in_redraft_mode(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session, mode="redraft")
    _draft_club(test_session, lg, mgrs["A"], 3)
    assert services.keeper_candidates(test_session, lg, "1")["goalie_team"] is None
    with pytest.raises(RuleViolation, match="aren't kept in this league"):
        services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                keeper_fpl_ids=[], season_year=UPCOMING,
                                keeper_team_code=3)


# ---- a kept club must not vanish -------------------------------------------
def test_a_kept_club_still_counts_after_submission(test_session):
    """`effective_keeper_selections` filters through a PLAYER-keyed ownership map,
    which has no opinion about a club — so a club selection gets dropped on the floor
    and the manager silently loses the draft slot it was meant to save."""
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    services.submit_keepers(test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
                            season_year=UPCOMING, keeper_team_code=3)
    kept = services.effective_keeper_selections(test_session, lg, UPCOMING)
    assert [s.team_id for s in kept] == [_clubs["ARS"].id]


def test_a_kept_club_costs_a_draft_pick(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    before = services.get_draft_board(test_session, lg, UPCOMING)
    services.submit_keepers(test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
                            season_year=UPCOMING, keeper_team_code=3)
    after = services.get_draft_board(test_session, lg, UPCOMING)
    a_before = sum(1 for b in before if b["owner_fpl"] == "1")
    a_after = sum(1 for b in after if b["owner_fpl"] == "1")
    assert (a_before, a_after) == (14, 13)


def test_a_kept_club_appears_in_the_selections_report(test_session):
    """It's joined against PlayerSeason for the player rows — a club has none, so the
    inner join silently drops it."""
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    services.submit_keepers(test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
                            season_year=UPCOMING, keeper_team_code=3)
    rows = services.get_keeper_selections(test_session, lg, UPCOMING,
                                          viewer_is_admin=True)
    assert rows == [{"manager": "A", "keepers": [
        {"player": "Arsenal", "position": "TEAM", "is_discovery": False}]}]


# ---- privacy ---------------------------------------------------------------
def test_a_kept_club_is_private_until_keepers_are_revealed(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    services.submit_keepers(test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
                            season_year=UPCOMING, keeper_team_code=3)

    # no viewer at all — this is what /v1 passes
    anon = services.get_keepers(test_session, lg)
    assert anon[0]["goalie_team"]["kept"] is False
    # the owner sees their own
    mine = services.get_keepers(test_session, lg, viewer_fpl="1")
    assert mine[0]["goalie_team"]["kept"] is True
    # ...and not somebody else's
    theirs = services.get_keepers(test_session, lg, viewer_fpl="2")
    assert theirs[0]["goalie_team"]["kept"] is False
    # admin sees everything
    admin = services.get_keepers(test_session, lg, viewer_is_admin=True)
    assert admin[0]["goalie_team"]["kept"] is True


def test_a_kept_club_is_public_once_revealed(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _draft_club(test_session, lg, mgrs["A"], 3)
    services.submit_keepers(test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
                            season_year=UPCOMING, keeper_team_code=3)
    lg.keepers_locked = True
    test_session.commit()
    assert services.get_keepers(test_session, lg)[0]["goalie_team"]["kept"] is True


# ---- rollover --------------------------------------------------------------
def test_the_rollover_carries_the_club_clock_and_ticks_it(test_session):
    """advance_season looks each selection up by player_id and `continue`s on a miss —
    so without its own branch a club keeper is dropped with no error and no audit, and
    the clock never ticks at all."""
    old, mgrs, clubs, gws = _seed(test_session)
    _draft_club(test_session, old, mgrs["A"], 3)
    services.submit_keepers(test_session, old, fpl_manager_id="1", keeper_fpl_ids=[],
                            season_year=UPCOMING, keeper_team_code=3)

    new, new_mgrs, _c, _g = _seed(test_session, season=UPCOMING, fpl_league_id="2",
                                  current=False)
    services.advance_season(test_session, old, new)

    seed = (test_session.query(KeeperSeed)
            .filter_by(league_id=new.id, team_id=clubs["ARS"].id).one())
    assert seed.manager_id == new_mgrs["A"].id
    assert seed.years_remaining == KEEPER_FRESH_DRAFT - 1


def test_the_rollover_does_not_invent_a_clock_for_a_club_nobody_holds(test_session):
    """A selection naming a club the manager no longer has must be skipped, not given
    a fresh clock — the same failure mode the player path already guards."""
    old, mgrs, clubs, gws = _seed(test_session)
    _draft_club(test_session, old, mgrs["A"], 3)
    services.submit_keepers(test_session, old, fpl_manager_id="1", keeper_fpl_ids=[],
                            season_year=UPCOMING, keeper_team_code=3)
    # the draft pick is corrected away — A never actually had Arsenal
    test_session.query(DraftPick).delete()
    test_session.commit()

    new, _nm, _c, _g = _seed(test_session, season=UPCOMING, fpl_league_id="2",
                             current=False)
    services.advance_season(test_session, old, new)
    assert test_session.query(KeeperSeed).filter_by(league_id=new.id).count() == 0


# ---- health checks ---------------------------------------------------------
def _checks(session, lg):
    return {c["check"]: c for c in services.data_health(session, lg)}


def test_health_reports_the_goalie_team_draft(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session, mode="redraft")
    _draft_club(test_session, lg, mgrs["A"], 3, season=UPCOMING, pick=1)
    checks = _checks(test_session, lg)
    assert checks["one goalie team per manager"]["ok"] is True
    assert "1/2" in checks["one goalie team per manager"]["detail"]
    assert checks["no club drafted twice"]["ok"] is True


def test_the_database_itself_refuses_a_second_club_and_a_shared_one(test_session):
    """The health checks above are a READOUT; this is the enforcement.

    Written as a DB test on purpose: the partial indexes are the last line, the one
    that holds even against a script, a migration or a future service that forgets.
    (It also means the health check's failure branch is unreachable from the app,
    which is the point of having both.)
    """
    from sqlalchemy.exc import IntegrityError

    lg, mgrs, clubs, gws = _seed(test_session, mode="redraft")
    _draft_club(test_session, lg, mgrs["A"], 3, season=UPCOMING, pick=1)

    for who, club, pick in [("A", "MCI", 98), ("B", "ARS", 99)]:
        with pytest.raises(IntegrityError):
            test_session.add(DraftPick(
                league_id=lg.id, season_year=UPCOMING, draft_type="main", round=1,
                pick_number=pick, manager_id=mgrs[who].id, team_id=clubs[club].id,
                source="draft"))
            test_session.commit()
        test_session.rollback()


def test_health_skips_goalkeepers_in_the_keeper_seed_check(test_session):
    """A goalkeeper can never be an individual keeper, so he can never need a seed.
    Left in, this lists every keeper in the league forever."""
    lg, mgrs, _clubs, gws = _seed(test_session, mode="redraft")
    squad = _squad(test_session, lg, mgrs, gws)
    for p in squad["A"]["out"] + squad["B"]["out"]:
        test_session.add(KeeperSeed(league_id=lg.id, manager_id=mgrs["A"].id,
                                    player_id=p.id, years_remaining=2))
    test_session.commit()
    assert _checks(test_session, lg)["rostered players have a keeper seed"]["ok"] is True


def test_health_flags_a_submitted_keeper_that_no_longer_counts(test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    squad = _squad(test_session, lg, mgrs, gws)
    kept = squad["A"]["out"][0]
    services.submit_keepers(test_session, lg, fpl_manager_id="1",
                            keeper_fpl_ids=[kept.fpl_id], season_year=UPCOMING)
    # he leaves A's roster entirely — the selection stops counting
    test_session.query(Roster).filter_by(player_id=kept.id).delete()
    test_session.commit()
    check = _checks(test_session, lg)["submitted keepers all still count"]
    assert check["ok"] is False and "A" in check["detail"]


# ---- the selection page ----------------------------------------------------
@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _login(client, session, manager, password="pw"):
    from auth import hash_password

    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text


def test_the_page_offers_the_club_checkbox(client, test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _squad(test_session, lg, mgrs, gws)
    _draft_club(test_session, lg, mgrs["A"], 3)
    _login(client, test_session, mgrs["A"])
    body = client.get("/keepers/candidates?fpl_manager_id=1").text
    assert 'name="keeper_team_code" value="3"' in body
    assert "Arsenal" in body
    # ...and the goalkeepers on the same page are checkboxes you can't tick
    assert body.count("disabled") >= 2


def test_the_page_has_no_club_checkbox_in_redraft_mode(client, test_session):
    lg, mgrs, _clubs, gws = _seed(test_session, mode="redraft")
    _squad(test_session, lg, mgrs, gws)
    _draft_club(test_session, lg, mgrs["A"], 3)
    _login(client, test_session, mgrs["A"])
    body = client.get("/keepers/candidates?fpl_manager_id=1").text
    assert "keeper_team_code" not in body


def test_submitting_the_form_keeps_the_club(client, test_session):
    lg, mgrs, _clubs, gws = _seed(test_session)
    _squad(test_session, lg, mgrs, gws)
    _draft_club(test_session, lg, mgrs["A"], 3)
    _login(client, test_session, mgrs["A"])
    r = client.post("/keepers", data={"fpl_manager_id": "1",
                                      "season_year": str(UPCOMING),
                                      "keeper_team_code": "3"})
    assert r.status_code == 303, r.text
    kept = services.effective_keeper_selections(test_session, lg, UPCOMING)
    assert [s.team_id for s in kept] == [_clubs_by_code(test_session, 3).id]


def _clubs_by_code(session, code):
    return session.query(PlTeam).filter_by(code=code).one()
