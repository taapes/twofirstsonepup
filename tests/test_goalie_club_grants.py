"""The 2026-only house rule: two clubs per manager, roster shape untouched.

Each manager holds TWO Premier League clubs' worth of goalkeeper rights, not two
individual keepers. FPL's own roster rules are unchanged (still 15 players, 2 GKP),
so the draft used ordinary individual GKP picks and `goalie_team_mode` correctly stays
'off' — this is a NEW, independent fact (`GoalieClubGrant`), not a fourth mode value.

The concrete problem this closes: a real-world transfer at an owned club forces a
manual FPL trade to keep a manager's roster in sync (FPL has no concept of "own a
club"), and that trade is indistinguishable from a real one until ownership is an
explicit fact. The worked example is production: Gaby held Aston Villa, Mark held
Chelsea; when Emiliano Martinez transferred Villa -> Chelsea, an FPL trade moved
Martinez to Mark and Sanchez to Gaby. Nothing was decided between them — the same
underlying asset (club rights) just manifested as a different individual each side.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime as dt

import pytest

import services
from models import (
    Gameweek,
    GoalieClubGrant,
    League,
    Manager,
    Player,
    PlayerSeason,
    PlTeam,
    Roster,
    Trade,
)
from rules import GOALIE_CLUBS_PER_MANAGER, RuleViolation

SEASON = 2026
_FPL = [0]


def _seed(session, *, season=SEASON):
    lg = League(fpl_league_id="1", name=f"S{season}", season_year=season,
                is_current=True, sync_locked=False, phase="in_season",
                goalie_team_mode="off")
    session.add(lg)
    session.flush()
    gw = Gameweek(number=3, league_id=lg.id)
    session.add(gw)
    session.flush()
    mgrs = {}
    for i, name in enumerate(["Gaby", "Mark", "Scott"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=f"Team{i}",
                    display_name=name)
        session.add(m)
        mgrs[name] = m
    session.commit()
    return lg, mgrs, gw


def _club(session, short, code, name="Club"):
    t = PlTeam(code=code, fpl_id=code, short_name=short, name=f"{name} {short}",
              is_current_pl=True)
    session.add(t)
    session.commit()
    return t


def _keeper(session, lg, name, short, *, roster_of=None, gw=None):
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=name, code=fid * 7, fpl_id=fid, position="GKP", current_team=short)
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fid, name=name,
                             position="GKP", current_team=short))
    if roster_of is not None:
        session.add(Roster(manager_id=roster_of.id, gameweek_id=gw.id, player_id=p.id))
    session.commit()
    return p


def _grant(session, lg, manager, team, *, season=SEASON):
    session.add(GoalieClubGrant(league_id=lg.id, season_year=season,
                                manager_id=manager.id, team_id=team.id))
    session.commit()


def _fpl_trade(session, lg, tid, *, gw=3):
    """A pair of Trade rows sharing one fpl_trade_id — the shape sync_trades writes
    for an FPL-sourced trade."""
    def leg(frm, to, player):
        session.add(Trade(league_id=lg.id, from_manager=frm.id, to_manager=to.id,
                          player_id=player.id, fpl_trade_id=tid, event_gw=gw))
    return leg


# ---- the base fact: _goalie_team_history / goalie_team_owner ------------------
def test_a_grant_needs_no_draft_pick_to_be_read(test_session):
    """The whole point: nothing about the 2026 draft recorded club ownership, so
    goalie_team_owner has to work from the grant alone."""
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    _grant(test_session, lg, m["Mark"], che)

    owner = services.goalie_team_owner(test_session, lg, season_year=SEASON)
    assert owner[che.id] == m["Mark"].fpl_manager_id


def test_two_grants_for_one_manager_both_read(test_session):
    """The dict key is already (season, team_id), so two clubs per manager needs no
    structural change — this pins that it actually works, not just compiles."""
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    avl = _club(test_session, "AVL", 7)
    _grant(test_session, lg, m["Mark"], che)
    _grant(test_session, lg, m["Mark"], avl)

    owner = services.goalie_team_owner(test_session, lg, season_year=SEASON)
    assert owner[che.id] == owner[avl.id] == m["Mark"].fpl_manager_id


def test_a_draft_pick_still_wins_over_a_grant_for_the_same_club(test_session):
    """Weakest source first: a grant is what stands in for a real club draft when
    none happened. If the league later moves to `keeper` mode and actually drafts a
    club, that real pick must win over a stale grant for the same (season, team)."""
    from models import DraftPick

    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    _grant(test_session, lg, m["Mark"], che)
    test_session.add(DraftPick(league_id=lg.id, season_year=SEASON, draft_type="main",
                               round=1, pick_number=1, manager_id=m["Scott"].id,
                               team_id=che.id, source="draft"))
    test_session.commit()

    owner = services.goalie_team_owner(test_session, lg, season_year=SEASON)
    assert owner[che.id] == m["Scott"].fpl_manager_id


# ---- the classifier: is this an FPL trade, or club continuity? ----------------
def test_the_martinez_shape_is_classified_as_continuity_not_a_trade(test_session):
    """The worked example, reproduced structurally: Mark holds Chelsea, Gaby holds
    Villa. Martinez (now CHE) moves to Mark; Sanchez (now AVL) moves to Gaby. Neither
    manager decided anything — stamp announced_at so it never reaches Discord."""
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    avl = _club(test_session, "AVL", 7)
    _grant(test_session, lg, m["Mark"], che)
    _grant(test_session, lg, m["Gaby"], avl)
    martinez = _keeper(test_session, lg, "Martinez", "CHE")
    sanchez = _keeper(test_session, lg, "Sanchez", "AVL")

    leg = _fpl_trade(test_session, lg, "134240")
    leg(m["Gaby"], m["Mark"], martinez)
    leg(m["Mark"], m["Gaby"], sanchez)
    test_session.commit()

    n = services.classify_goalkeeper_continuity_trades(test_session, lg)
    assert n == 1
    rows = test_session.query(Trade).filter_by(fpl_trade_id="134240").all()
    assert all(t.announced_at is not None for t in rows)


def test_a_real_trade_of_two_keepers_is_left_alone(test_session):
    """The 25/26 counterexample: Jorgensen<->Alisson, structurally identical (one leg
    each way, both GKP) but no grant exists for that season, so it can't match. The
    grant check — not the position check alone — is what decides it."""
    lg, m, gw = _seed(test_session, season=2025)
    che = _club(test_session, "CHE", 8)
    liv = _club(test_session, "LIV", 11)
    jorgensen = _keeper(test_session, lg, "Jorgensen", "CHE")
    alisson = _keeper(test_session, lg, "Alisson", "LIV")
    # No grants at all for 2025 — the rule is 2026-only.

    leg = _fpl_trade(test_session, lg, "695349", gw=22)
    leg(m["Scott"], m["Mark"], jorgensen)
    leg(m["Mark"], m["Scott"], alisson)
    test_session.commit()

    n = services.classify_goalkeeper_continuity_trades(test_session, lg)
    assert n == 0
    rows = test_session.query(Trade).filter_by(fpl_trade_id="695349").all()
    assert all(t.announced_at is None for t in rows)


def test_only_one_side_owning_the_club_is_not_enough(test_session):
    """Both receivers must already hold the grant for what they're receiving — one
    side matching and the other not is a real trade (or a data problem), not
    continuity, and must not be swept up."""
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    avl = _club(test_session, "AVL", 7)
    _grant(test_session, lg, m["Mark"], che)
    # Villa is NOT granted to Gaby (e.g. unconfirmed, or genuinely someone else's).
    martinez = _keeper(test_session, lg, "Martinez", "CHE")
    sanchez = _keeper(test_session, lg, "Sanchez", "AVL")

    leg = _fpl_trade(test_session, lg, "999999")
    leg(m["Gaby"], m["Mark"], martinez)
    leg(m["Mark"], m["Gaby"], sanchez)
    test_session.commit()

    assert services.classify_goalkeeper_continuity_trades(test_session, lg) == 0


def test_a_non_goalkeeper_pair_is_never_classified(test_session):
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    avl = _club(test_session, "AVL", 7)
    _grant(test_session, lg, m["Mark"], che)
    _grant(test_session, lg, m["Gaby"], avl)
    _FPL[0] += 1
    striker_a = Player(name="A", code=_FPL[0] * 7, fpl_id=_FPL[0], position="FWD",
                       current_team="CHE")
    _FPL[0] += 1
    striker_b = Player(name="B", code=_FPL[0] * 7, fpl_id=_FPL[0], position="FWD",
                       current_team="AVL")
    test_session.add_all([striker_a, striker_b])
    test_session.commit()

    leg = _fpl_trade(test_session, lg, "555555")
    leg(m["Gaby"], m["Mark"], striker_a)
    leg(m["Mark"], m["Gaby"], striker_b)
    test_session.commit()

    assert services.classify_goalkeeper_continuity_trades(test_session, lg) == 0


def test_an_already_announced_trade_is_never_revisited(test_session):
    """`announced_at IS NULL` is the only thing this reads — a row already decided
    (announced, or already classified) is left exactly as it is."""
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    avl = _club(test_session, "AVL", 7)
    _grant(test_session, lg, m["Mark"], che)
    _grant(test_session, lg, m["Gaby"], avl)
    martinez = _keeper(test_session, lg, "Martinez", "CHE")
    sanchez = _keeper(test_session, lg, "Sanchez", "AVL")

    leg = _fpl_trade(test_session, lg, "134240")
    leg(m["Gaby"], m["Mark"], martinez)
    leg(m["Mark"], m["Gaby"], sanchez)
    test_session.commit()
    for t in test_session.query(Trade).filter_by(fpl_trade_id="134240"):
        t.announced_at = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    test_session.commit()

    assert services.classify_goalkeeper_continuity_trades(test_session, lg) == 0


def test_a_three_way_or_lopsided_fpl_trade_id_is_never_classified(test_session):
    """Only a clean two-manager, two-leg swap is even considered."""
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    _grant(test_session, lg, m["Mark"], che)
    k1 = _keeper(test_session, lg, "K1", "CHE")
    k2 = _keeper(test_session, lg, "K2", "CHE")
    k3 = _keeper(test_session, lg, "K3", "CHE")

    tid = "777777"
    test_session.add_all([
        Trade(league_id=lg.id, from_manager=m["Gaby"].id, to_manager=m["Mark"].id,
             player_id=k1.id, fpl_trade_id=tid, event_gw=3),
        Trade(league_id=lg.id, from_manager=m["Scott"].id, to_manager=m["Mark"].id,
             player_id=k2.id, fpl_trade_id=tid, event_gw=3),
        Trade(league_id=lg.id, from_manager=m["Mark"].id, to_manager=m["Gaby"].id,
             player_id=k3.id, fpl_trade_id=tid, event_gw=3),
    ])
    test_session.commit()

    assert services.classify_goalkeeper_continuity_trades(test_session, lg) == 0


# ---- entry: commissioner confirms, never applies blind ------------------------
def test_a_grant_can_be_set_and_is_idempotent(test_session):
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)

    r = services.set_goalie_club_grant(test_session, lg, fpl_manager_id="2",
                                       team_code=8, season_year=SEASON)
    assert r == {"manager": "Mark", "team": che.name, "season_year": SEASON}
    assert test_session.query(GoalieClubGrant).count() == 1

    # Re-entering the same grant is a no-op, not a duplicate.
    services.set_goalie_club_grant(test_session, lg, fpl_manager_id="2",
                                   team_code=8, season_year=SEASON)
    assert test_session.query(GoalieClubGrant).count() == 1


def test_a_club_cannot_be_double_granted(test_session):
    lg, m, gw = _seed(test_session)
    _club(test_session, "CHE", 8)
    services.set_goalie_club_grant(test_session, lg, fpl_manager_id="2",
                                   team_code=8, season_year=SEASON)
    with pytest.raises(RuleViolation, match="already granted"):
        services.set_goalie_club_grant(test_session, lg, fpl_manager_id="3",
                                       team_code=8, season_year=SEASON)


def test_a_manager_is_capped_at_the_club_limit(test_session):
    lg, m, gw = _seed(test_session)
    codes = [8, 7, 11]
    for c in codes:
        _club(test_session, f"C{c}", c)
    for c in codes[:GOALIE_CLUBS_PER_MANAGER]:
        services.set_goalie_club_grant(test_session, lg, fpl_manager_id="2",
                                       team_code=c, season_year=SEASON)
    with pytest.raises(RuleViolation, match="already holds"):
        services.set_goalie_club_grant(test_session, lg, fpl_manager_id="2",
                                       team_code=codes[-1], season_year=SEASON)


# ---- the inference: presented, never applied -----------------------------------
def test_the_inferred_map_partitions_clubs_from_current_rosters(test_session):
    lg, m, gw = _seed(test_session)
    che = _club(test_session, "CHE", 8)
    avl = _club(test_session, "AVL", 7)
    _keeper(test_session, lg, "Martinez", "CHE", roster_of=m["Mark"], gw=gw)
    _keeper(test_session, lg, "Suzuki", "AVL", roster_of=m["Gaby"], gw=gw)

    rows = services.inferred_goalie_club_grants(test_session, lg)
    by_team = {r["team_id"]: r["manager_name"] for r in rows}
    assert by_team[che.id] == "Mark"
    assert by_team[avl.id] == "Gaby"


def test_the_inference_never_writes(test_session):
    lg, m, gw = _seed(test_session)
    _club(test_session, "CHE", 8)
    _keeper(test_session, lg, "Martinez", "CHE", roster_of=m["Mark"], gw=gw)

    services.inferred_goalie_club_grants(test_session, lg)
    assert test_session.query(GoalieClubGrant).count() == 0
