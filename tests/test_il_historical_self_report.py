"""Self-service historical IL/international placement for a player already dropped.

The gap this closes: a manager drafts a player who is already hurt, drops him for a
same-position replacement in FPL, and only THEN comes to record the injury on the
site -- by which point the synced roster shows the replacement, not him, and
place_on_il's self-service path refused outright (it was tightened in Item 6b
specifically to stop a manager claiming an unrelated player as theirs).

The fix: `_validate_absence_eligibility` falls back from "is he on your CURRENT
roster" to "was he EVER on your roster this season" (via the same `presence` dict
`_derive_keeper_status` already shares), and refuses if a DIFFERENT manager genuinely
holds him now. `dropped_players_for_manager` is the picker's candidate list and the
`start_gw` suggestion's source.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import Gameweek, InjuryList, InternationalList, League, Manager, Player, PlayerSeason, Roster
from rules import RuleViolation

LAST_GW = 38


def _seed(session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                phase="offseason")
    session.add(lg)
    session.flush()
    gws = {}
    for n in range(1, LAST_GW + 1):
        g = Gameweek(number=n, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[n] = g
    a = Manager(league_id=lg.id, fpl_manager_id="1", name="A", display_name="Ann")
    b = Manager(league_id=lg.id, fpl_manager_id="2", name="B", display_name="Bo")
    session.add_all([a, b])
    session.commit()
    return lg, a, b, gws


def _player(session, lg, name, fpl_id, pos="MID"):
    p = Player(name=name, code=fpl_id * 7, fpl_id=fpl_id, position=pos,
               current_team="MUN", price=90, status="a")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id, name=name,
                             position=pos, current_team="MUN"))
    session.commit()
    return p


def _hold(session, mgr, player, gws, numbers):
    for n in numbers:
        session.add(Roster(manager_id=mgr.id, gameweek_id=gws[n].id, player_id=player.id))
    session.commit()


# ---- the motivating scenario: drafted, hurt, dropped, never recorded ------

def test_wright_scenario_self_service_succeeds(test_session):
    """Drafted Wright, held him GW1-13, dropped him for Simms GW14 -- never recorded
    the injury. The manager reports it now, at GW20."""
    lg, a, _b, gws = _seed(test_session)
    wright = _player(test_session, lg, "Wright", 1, pos="FWD")
    simms = _player(test_session, lg, "Simms", 2, pos="FWD")
    _hold(test_session, a, wright, gws, range(1, 14))
    _hold(test_session, a, simms, gws, range(14, LAST_GW + 1))

    out = services.place_on_il(
        test_session, lg, fpl_manager_id="1",
        injured_fpl_id=wright.fpl_id, replacement_fpl_id=simms.fpl_id,
        start_gw=13,
    )
    assert out["player"] == "Wright"

    entry = test_session.query(InjuryList).filter_by(player_id=wright.id).one()
    assert entry.self_reported is True
    assert entry.start_gw == 13
    assert services.effective_owner(test_session, lg)[wright.id] == a.id


def test_ordinary_on_roster_placement_is_not_flagged_self_reported(test_session):
    """The existing, common case -- he's still on the roster -- is unaffected."""
    lg, a, _b, gws = _seed(test_session)
    wright = _player(test_session, lg, "Wright", 1, pos="FWD")
    simms = _player(test_session, lg, "Simms", 2, pos="FWD")
    _hold(test_session, a, wright, gws, range(1, LAST_GW + 1))
    _hold(test_session, a, simms, gws, range(1, LAST_GW + 1))

    services.place_on_il(
        test_session, lg, fpl_manager_id="1",
        injured_fpl_id=wright.fpl_id, replacement_fpl_id=simms.fpl_id,
        start_gw=20,
    )
    entry = test_session.query(InjuryList).filter_by(player_id=wright.id).one()
    assert entry.self_reported is False


def test_the_admin_backfill_path_is_never_flagged_self_reported(test_session):
    """require_roster=False (the admin route) is a different actor entirely --
    self_reported means a MANAGER used the historical path, not admin."""
    lg, a, _b, gws = _seed(test_session)
    wright = _player(test_session, lg, "Wright", 1, pos="FWD")
    simms = _player(test_session, lg, "Simms", 2, pos="FWD")
    _hold(test_session, a, simms, gws, range(1, LAST_GW + 1))

    services.place_on_il(
        test_session, lg, fpl_manager_id="1", require_roster=False,
        injured_fpl_id=wright.fpl_id, replacement_fpl_id=simms.fpl_id,
        start_gw=13,
    )
    entry = test_session.query(InjuryList).filter_by(player_id=wright.id).one()
    assert entry.self_reported is False


# ---- eligibility: must have been on MY roster this season -----------------

def test_a_player_never_held_is_refused(test_session):
    lg, a, _b, gws = _seed(test_session)
    stranger = _player(test_session, lg, "Stranger", 1, pos="FWD")
    simms = _player(test_session, lg, "Simms", 2, pos="FWD")
    _hold(test_session, a, simms, gws, range(1, LAST_GW + 1))

    with pytest.raises(RuleViolation) as exc:
        services.place_on_il(
            test_session, lg, fpl_manager_id="1",
            injured_fpl_id=stranger.fpl_id, replacement_fpl_id=simms.fpl_id,
            start_gw=13,
        )
    assert "isn't on" in str(exc.value)


def test_a_player_currently_held_by_someone_else_is_refused(test_session):
    """Was on A's roster earlier -- but B has since claimed him for real. A cannot
    retroactively grab him back via an IL claim."""
    lg, a, b, gws = _seed(test_session)
    wright = _player(test_session, lg, "Wright", 1, pos="FWD")
    simms = _player(test_session, lg, "Simms", 2, pos="FWD")
    _hold(test_session, a, wright, gws, range(1, 14))
    _hold(test_session, a, simms, gws, range(1, LAST_GW + 1))
    _hold(test_session, b, wright, gws, range(14, LAST_GW + 1))  # B claimed him GW14+

    with pytest.raises(RuleViolation) as exc:
        services.place_on_il(
            test_session, lg, fpl_manager_id="1",
            injured_fpl_id=wright.fpl_id, replacement_fpl_id=simms.fpl_id,
            start_gw=13,
        )
    assert "currently rostered by Bo" in str(exc.value)


def test_intl_shares_the_same_historical_eligibility(test_session):
    """place_on_intl gets the identical fallback -- the AFCON/Asia Cup twin of the
    same gap."""
    lg, a, _b, gws = _seed(test_session)
    away = _player(test_session, lg, "Away", 1, pos="MID")
    rep = _player(test_session, lg, "Rep", 2, pos="MID")
    _hold(test_session, a, away, gws, range(1, 14))
    _hold(test_session, a, rep, gws, range(14, LAST_GW + 1))

    services.place_on_intl(
        test_session, lg, fpl_manager_id="1",
        away_fpl_id=away.fpl_id, replacement_fpl_id=rep.fpl_id, start_gw=13,
    )
    entry = test_session.query(InternationalList).filter_by(player_id=away.id).one()
    assert entry.self_reported is True


def test_intl_also_refuses_a_player_now_held_by_someone_else(test_session):
    lg, a, b, gws = _seed(test_session)
    away = _player(test_session, lg, "Away", 1, pos="MID")
    rep = _player(test_session, lg, "Rep", 2, pos="MID")
    _hold(test_session, a, away, gws, range(1, 14))
    _hold(test_session, a, rep, gws, range(1, LAST_GW + 1))
    _hold(test_session, b, away, gws, range(14, LAST_GW + 1))

    with pytest.raises(RuleViolation) as exc:
        services.place_on_intl(
            test_session, lg, fpl_manager_id="1",
            away_fpl_id=away.fpl_id, replacement_fpl_id=rep.fpl_id, start_gw=13,
        )
    assert "currently rostered by Bo" in str(exc.value)


# ---- the picker: dropped_players_for_manager -------------------------------

def test_dropped_players_offers_the_previously_held_player(test_session):
    lg, a, _b, gws = _seed(test_session)
    wright = _player(test_session, lg, "Wright", 1, pos="FWD")
    simms = _player(test_session, lg, "Simms", 2, pos="FWD")
    _hold(test_session, a, wright, gws, range(1, 14))
    _hold(test_session, a, simms, gws, range(14, LAST_GW + 1))

    rows = services.dropped_players_for_manager(test_session, lg, a)
    assert [r["name"] for r in rows] == ["Wright"]
    assert rows[0]["suggested_start_gw"] == 14


def test_dropped_players_excludes_someone_currently_on_the_roster(test_session):
    lg, a, _b, gws = _seed(test_session)
    wright = _player(test_session, lg, "Wright", 1, pos="FWD")
    _hold(test_session, a, wright, gws, range(1, LAST_GW + 1))

    assert services.dropped_players_for_manager(test_session, lg, a) == []


def test_dropped_players_excludes_someone_claimed_by_another_manager(test_session):
    lg, a, b, gws = _seed(test_session)
    wright = _player(test_session, lg, "Wright", 1, pos="FWD")
    _hold(test_session, a, wright, gws, range(1, 14))
    _hold(test_session, b, wright, gws, range(14, LAST_GW + 1))

    assert services.dropped_players_for_manager(test_session, lg, a) == []


def test_dropped_players_excludes_someone_never_held(test_session):
    lg, a, b, gws = _seed(test_session)
    stranger = _player(test_session, lg, "Stranger", 1, pos="FWD")
    _hold(test_session, b, stranger, gws, range(1, LAST_GW + 1))

    assert services.dropped_players_for_manager(test_session, lg, a) == []


# ---- admin visibility -------------------------------------------------------

def test_health_check_lists_self_reported_placements(test_session):
    lg, a, _b, gws = _seed(test_session)
    wright = _player(test_session, lg, "Wright", 1, pos="FWD")
    simms = _player(test_session, lg, "Simms", 2, pos="FWD")
    _hold(test_session, a, wright, gws, range(1, 14))
    _hold(test_session, a, simms, gws, range(14, LAST_GW + 1))

    services.place_on_il(
        test_session, lg, fpl_manager_id="1",
        injured_fpl_id=wright.fpl_id, replacement_fpl_id=simms.fpl_id, start_gw=13,
    )
    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    row = checks["self-reported IL/international placements"]
    assert not row["ok"]
    assert "Wright" in row["detail"]


def test_health_check_is_ok_with_no_self_reported_placements(test_session):
    lg, a, _b, gws = _seed(test_session)
    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["self-reported IL/international placements"]["ok"]
