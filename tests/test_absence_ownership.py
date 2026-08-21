"""Absence ownership: an IL'd or internationally-absent player is still HELD.

The bug this closes: FPL's roster shows the replacement, so exactly one reader
(`_derive_keeper_status`, via its IL-coverage union) knew the absentee was still the
manager's. Every other reader of ownership disagreed, and each past incident was patched
by bolting a boolean onto whichever reader broke first — three carve-outs, each found
live. The fix is one predicate (`_absence_held`) folded into `_owner_maps`, so every
reader that already consumes the trade overlay gets absences for free.

Structured like tests/test_trade_overlay.py — a test per reader — because that shape is
what would have caught all three carve-outs before they shipped.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime

import pytest

import services
from models import (
    Gameweek,
    InjuryList,
    InternationalList,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerSeason,
    Roster,
    Trade,
)
from rules import MIN_IL_STAY_GWS, RuleViolation

LAST_GW = 38


def _seed(session, n_gws=LAST_GW):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                phase="offseason")
    session.add(lg)
    session.flush()
    gws = {}
    for n in range(1, n_gws + 1):
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


def _il(session, mgr, player, replacement, *, start=30, end=None, status="active",
        released=None):
    e = InjuryList(player_id=player.id, manager_id=mgr.id, start_gw=start, end_gw=end,
                   replacement_id=replacement.id if replacement else None, status=status,
                   released_player_id=released.id if released else None)
    session.add(e)
    session.commit()
    return e


def _intl(session, mgr, player, replacement=None, *, start=25, status="active",
          tournament="AFCON"):
    e = InternationalList(player_id=player.id, manager_id=mgr.id, start_gw=start,
                          replacement_id=replacement.id if replacement else None,
                          status=status, tournament=tournament)
    session.add(e)
    session.commit()
    return e


def _start_gw(session, gws, n):
    """Mark GW `n` as the current one: current_gameweek() takes max(started), so a
    past start_date on n (and none later) is enough."""
    gws[n].start_date = datetime.date.today() - datetime.timedelta(days=1)
    session.commit()


def _live_stats(fpl_id, minutes):
    return {str(fpl_id): {"stats": {"minutes": minutes}}}


# ---- the fold itself -------------------------------------------------------

def test_an_absent_player_is_still_owned(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, out, gws, range(1, 30))
    _hold(test_session, a, rep, gws, range(30, LAST_GW + 1))
    _il(test_session, a, out, rep)

    assert services.effective_owner(test_session, lg)[out.id] == a.id


def test_the_replacement_is_owned_too(test_session):
    """Additive, not a swap: the manager holds 16 while someone is away."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, out, gws, range(1, 30))
    _hold(test_session, a, rep, gws, range(30, LAST_GW + 1))
    _il(test_session, a, out, rep)

    owner = services.effective_owner(test_session, lg)
    assert owner[out.id] == a.id and owner[rep.id] == a.id


def test_an_absence_cannot_steal_a_rostered_player(test_session):
    """The fold is guarded 'only if unowned' — a mis-entered absence must fail closed."""
    lg, a, b, gws = _seed(test_session)
    star = _player(test_session, lg, "Star", 1)
    _hold(test_session, b, star, gws, range(1, LAST_GW + 1))
    _il(test_session, a, star, None)  # A claims B's player

    assert services.effective_owner(test_session, lg)[star.id] == b.id


def test_an_uncapped_international_list_holds_every_absentee(test_session):
    """AFCON: several away at once, each with a replacement. 15 + N held."""
    lg, a, _b, gws = _seed(test_session)
    away = [_player(test_session, lg, f"Away{i}", 10 + i) for i in range(3)]
    for p in away:
        _hold(test_session, a, p, gws, range(1, 25))
        test_session.add(InternationalList(
            player_id=p.id, manager_id=a.id, start_gw=25, replacement_id=None,
            status="active", tournament="AFCON"))
    test_session.commit()

    owner = services.effective_owner(test_session, lg)
    assert all(owner[p.id] == a.id for p in away)


# ---- fold ORDER: absences before trades ------------------------------------

def test_an_absent_player_can_still_be_traded(test_session):
    """Fold absences AFTER trades and this breaks permanently: the trade guard only
    fires when the map already says the sender holds him, and an absentee is off the
    roster, so the trade could never apply."""
    lg, a, b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)
    test_session.add(Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id,
                           player_id=out.id))
    test_session.commit()

    assert services.effective_owner(test_session, lg)[out.id] == b.id


def test_the_health_check_does_not_report_that_trade_as_unapplied(test_session):
    lg, a, b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)
    test_session.add(Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id,
                           player_id=out.id))
    test_session.commit()

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["site trades applied"]["ok"], checks["site trades applied"]["detail"]


# ---- the predicate: candidacy and ownership must not fork ------------------

def test_a_return_at_season_end_keeps_him_a_candidate(test_session):
    """`status == 'active'` alone would drop him here while _absence_cover still
    covers him — the fork that costs a manager a draft slot."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None, end=LAST_GW, status="returned")

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    assert out.id in status.get(a.id, {})
    assert services.effective_owner(test_session, lg)[out.id] == a.id


def test_a_return_before_season_end_does_not(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None, end=32, status="returned")

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    assert out.id not in status.get(a.id, {})
    assert out.id not in services.effective_owner(test_session, lg)


def test_a_waived_absentee_belongs_to_nobody(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None, end=LAST_GW, status="waived")

    assert out.id not in services.effective_owner(test_session, lg)


def test_a_player_claimed_by_someone_else_is_one_managers_candidate_only(test_session):
    """Live bug before this change: reconcile_absences keys on (manager, player), so
    A's entry never auto-closed and _absence_cover kept granting A candidacy while B
    actually rosters him — he showed up on BOTH keeper boards."""
    lg, a, b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 20))
    _hold(test_session, b, out, gws, range(20, LAST_GW + 1))
    _il(test_session, a, out, None, start=20)

    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    holders = [m for m in (a.id, b.id) if out.id in status.get(m, {})]
    assert holders == [b.id]


# ---- season-end resolution -------------------------------------------------

def test_returning_at_season_end_requires_naming_who_leaves(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, out, gws, range(1, 30))
    _hold(test_session, a, rep, gws, range(30, LAST_GW + 1))
    e = _il(test_session, a, out, rep)

    with pytest.raises(RuleViolation) as exc:
        services.return_from_il(test_session, lg, str(e.id), LAST_GW)
    assert "make room" in str(exc.value)


def test_the_named_player_stops_being_owned(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, out, gws, range(1, 30))
    _hold(test_session, a, rep, gws, range(30, LAST_GW + 1))
    e = _il(test_session, a, out, rep)

    services.return_from_il(test_session, lg, str(e.id), LAST_GW,
                            released_fpl_id=rep.fpl_id)
    owner = services.effective_owner(test_session, lg)
    assert owner[out.id] == a.id
    assert rep.id not in owner


def test_the_named_player_stops_being_a_keeper_candidate(test_session):
    """He is still on the frozen GW38 snapshot, so without the subtraction reaching
    final_candidates he would stay keepable by the manager who released him."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, out, gws, range(1, 30))
    _hold(test_session, a, rep, gws, range(30, LAST_GW + 1))
    e = _il(test_session, a, out, rep)

    services.return_from_il(test_session, lg, str(e.id), LAST_GW,
                            released_fpl_id=rep.fpl_id)
    status = services._derive_keeper_status(test_session, lg, kept_all=True)
    assert out.id in status.get(a.id, {})
    assert rep.id not in status.get(a.id, {})
    # ...and not parked under a phantom owner either. Without the candidate-side
    # subtraction he lands in a None bucket, which the assertion above would miss.
    assert None not in status
    assert not any(rep.id in held for held in status.values())


def test_releasing_instead_needs_nobody_to_leave(test_session):
    """The waiver branch already resolved to 15 — the absentee is what leaves."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, out, gws, range(1, 30))
    _hold(test_session, a, rep, gws, range(30, LAST_GW + 1))
    e = _il(test_session, a, out, rep)

    services.return_from_il(test_session, lg, str(e.id), LAST_GW, via="waiver")
    owner = services.effective_owner(test_session, lg)
    assert out.id not in owner
    assert owner[rep.id] == a.id


def test_naming_someone_mid_season_is_refused(test_session):
    """Mid-season the manager swaps in FPL and the sync sees it."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, out, gws, range(1, 20))
    _hold(test_session, a, rep, gws, range(20, LAST_GW + 1))
    e = _il(test_session, a, out, rep, start=10)

    with pytest.raises(RuleViolation) as exc:
        services.return_from_il(test_session, lg, str(e.id), 20,
                                released_fpl_id=rep.fpl_id)
    assert "season end" in str(exc.value)


def test_cannot_release_a_player_you_do_not_hold(test_session):
    lg, a, b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    theirs = _player(test_session, lg, "Theirs", 3)
    _hold(test_session, a, out, gws, range(1, 30))
    _hold(test_session, b, theirs, gws, range(1, LAST_GW + 1))
    e = _il(test_session, a, out, None)

    with pytest.raises(RuleViolation) as exc:
        services.return_from_il(test_session, lg, str(e.id), LAST_GW,
                                released_fpl_id=theirs.fpl_id)
    assert "isn't on your roster" in str(exc.value)


# ---- enforcement -----------------------------------------------------------

def test_an_unresolved_absence_blocks_that_managers_keepers(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)

    with pytest.raises(RuleViolation) as exc:
        services.submit_keepers(test_session, lg, fpl_manager_id="1",
                                keeper_fpl_ids=[], season_year=2026)
    assert "absence list" in str(exc.value)


def test_it_does_not_block_a_manager_who_owes_nothing(test_session):
    """Manager-scoped, not a league-wide lock."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)

    services.submit_keepers(test_session, lg, fpl_manager_id="2",
                            keeper_fpl_ids=[], season_year=2026)


def test_the_rollover_refuses_while_an_absence_is_open(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)
    new = League(fpl_league_id="2", name="S27", season_year=2026)
    test_session.add(new)
    test_session.flush()
    nm = Manager(league_id=new.id, fpl_manager_id="1", name="A", display_name="Ann")
    nm2 = Manager(league_id=new.id, fpl_manager_id="2", name="B", display_name="Bo")
    test_session.add_all([nm, nm2])
    test_session.commit()

    with pytest.raises(RuleViolation) as exc:
        services.advance_season(test_session, lg, new,
                                pairing={nm.id: a.id, nm2.id: _b.id})
    assert "absence list" in str(exc.value)


def test_health_check_flags_a_player_on_two_absence_lists(test_session):
    lg, a, b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)
    _il(test_session, b, out, None)

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert not checks["no player on two absence lists"]["ok"]


# ---- write-path guards -----------------------------------------------------

def test_you_cannot_il_someone_elses_player(test_session):
    lg, _a, b, gws = _seed(test_session)
    star = _player(test_session, lg, "Star", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, b, star, gws, range(1, LAST_GW + 1))
    _hold(test_session, b, rep, gws, range(1, LAST_GW + 1))

    with pytest.raises(RuleViolation) as exc:
        services.place_on_il(test_session, lg, fpl_manager_id="1",
                             injured_fpl_id=star.fpl_id,
                             replacement_fpl_id=rep.fpl_id, start_gw=30)
    assert "roster" in str(exc.value)


def test_the_historical_backfill_is_exempt(test_session):
    """The backfill exists BECAUSE the roster shows the replacement, not the injured
    player — guarding it would refuse every case it was built for."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, rep, gws, range(1, LAST_GW + 1))

    services.place_on_il(test_session, lg, fpl_manager_id="1", require_roster=False,
                         injured_fpl_id=out.fpl_id, replacement_fpl_id=rep.fpl_id,
                         start_gw=30)
    assert services.effective_owner(test_session, lg)[out.id] == a.id


def test_a_second_international_absence_is_allowed(test_session):
    """The cap that used to be here contradicted the league rule."""
    lg, a, _b, gws = _seed(test_session)
    p1 = _player(test_session, lg, "One", 1)
    p2 = _player(test_session, lg, "Two", 2)
    r1 = _player(test_session, lg, "R1", 3)
    r2 = _player(test_session, lg, "R2", 4)
    for p in (p1, p2, r1, r2):
        _hold(test_session, a, p, gws, range(1, LAST_GW + 1))

    services.place_on_intl(test_session, lg, fpl_manager_id="1", away_fpl_id=p1.fpl_id,
                           replacement_fpl_id=r1.fpl_id, start_gw=25)
    services.place_on_intl(test_session, lg, fpl_manager_id="1", away_fpl_id=p2.fpl_id,
                           replacement_fpl_id=r2.fpl_id, start_gw=25)

    assert test_session.query(InternationalList).filter_by(
        manager_id=a.id, status="active").count() == 2


def test_the_same_player_cannot_go_away_twice(test_session):
    lg, a, _b, gws = _seed(test_session)
    p1 = _player(test_session, lg, "One", 1)
    r1 = _player(test_session, lg, "R1", 3)
    for p in (p1, r1):
        _hold(test_session, a, p, gws, range(1, LAST_GW + 1))

    services.place_on_intl(test_session, lg, fpl_manager_id="1", away_fpl_id=p1.fpl_id,
                           replacement_fpl_id=r1.fpl_id, start_gw=25)
    with pytest.raises(RuleViolation):
        services.place_on_intl(test_session, lg, fpl_manager_id="1",
                               away_fpl_id=p1.fpl_id, replacement_fpl_id=r1.fpl_id,
                               start_gw=26)


# ---- readers that must NOT change ------------------------------------------

def test_my_team_shows_the_absent_player(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, out, gws, range(1, 30))
    _hold(test_session, a, rep, gws, range(30, LAST_GW + 1))
    _il(test_session, a, out, rep)

    team = services.get_my_team(test_session, lg, "1")
    assert {p["name"] for p in team["players"]} == {"Out", "Rep"}


def test_the_players_tab_names_the_owner(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)

    row = next(r for r in services.player_portal(test_session, lg) if r["name"] == "Out")
    assert row["owner"] == "Ann" and row["rostered"] and row["on_il"]


def test_the_trade_form_offers_the_absent_player(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)

    assets = services.manager_assets(test_session, lg, "1")
    assert "Out" in {p["name"] for p in assets["players"]}


def test_the_fifteen_man_check_still_reads_the_raw_snapshot(test_session):
    """Deliberately raw: it validates the SYNC, so the overlay must not touch it."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, 30))
    _il(test_session, a, out, None)

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    detail = next(c["detail"] for k, c in checks.items() if k.startswith("15-man"))
    assert "Ann=0" in detail  # the absentee is NOT counted into the raw snapshot


def test_an_absence_invents_no_transaction(test_session):
    """/transactions diffs raw snapshots — the overlay has no gameweek to attribute."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _hold(test_session, a, out, gws, range(1, LAST_GW + 1))
    _il(test_session, a, out, None)

    rows = services.get_transactions(test_session, lg)
    assert not [r for r in rows if r.get("player") == "Out"]


# ---- Item 6c: record_absentee_minutes ---------------------------------------

def test_record_absentee_minutes_sets_last_played_gw(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    rep = _player(test_session, lg, "Rep", 2)
    _hold(test_session, a, rep, gws, range(1, LAST_GW + 1))
    e = _il(test_session, a, out, rep, start=20)

    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 63), 25)

    test_session.refresh(e)
    assert e.last_played_gw == 25


def test_record_absentee_minutes_ignores_zero_minutes(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    e = _il(test_session, a, out, None, start=20)

    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 0), 25)

    test_session.refresh(e)
    assert e.last_played_gw is None


def test_record_absentee_minutes_ignores_closed_entries(test_session):
    """Only 'active' rows are candidates — a returned/waived entry is not still
    someone's absence to report on."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    e = _il(test_session, a, out, None, start=20, end=24, status="returned")

    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 90), 25)

    test_session.refresh(e)
    assert e.last_played_gw is None


def test_record_absentee_minutes_updates_every_call(test_session):
    """The field tracks the MOST RECENT gameweek he played, not the first."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    e = _il(test_session, a, out, None, start=20)

    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 90), 24)
    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 45), 25)

    test_session.refresh(e)
    assert e.last_played_gw == 25


def test_record_absentee_minutes_covers_the_international_list_too(test_session):
    lg, a, _b, gws = _seed(test_session)
    away = _player(test_session, lg, "Away", 1)
    e = _intl(test_session, a, away, start=25)

    services.record_absentee_minutes(test_session, lg, _live_stats(away.fpl_id, 90), 26)

    test_session.refresh(e)
    assert e.last_played_gw == 26


# ---- Item 6c: the must-return alert -----------------------------------------

def test_the_alert_fires_once_the_minimum_stay_has_passed(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    e = _il(test_session, a, out, None, start=20)
    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 90),
                                     20 + MIN_IL_STAY_GWS + 1)
    _start_gw(test_session, gws, 20 + MIN_IL_STAY_GWS + 1)

    entries = services._return_required_entries(test_session, lg)
    assert [x["player"] for x in entries] == ["Out"]


def test_the_alert_is_suppressed_inside_the_minimum_stay(test_session):
    """The 4-GW minimum stay holds even if he recovers sooner — logging minutes early
    is not yet a violation."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _il(test_session, a, out, None, start=20)
    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 90), 21)
    _start_gw(test_session, gws, 21)  # only 1 GW in, eligible at GW24

    assert services._return_required_entries(test_session, lg) == []


def test_the_alert_fires_exactly_at_the_eligible_gameweek(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    eligible = services.il_return_eligible_gw(20)
    _il(test_session, a, out, None, start=20)
    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 90),
                                     eligible)
    _start_gw(test_session, gws, eligible)

    assert [x["player"] for x in services._return_required_entries(test_session, lg)] \
        == ["Out"]


def test_the_international_alert_has_no_minimum_stay(test_session):
    """Unlike the IL, an international absentee alerts as soon as he plays."""
    lg, a, _b, gws = _seed(test_session)
    away = _player(test_session, lg, "Away", 1)
    _intl(test_session, a, away, start=25)
    services.record_absentee_minutes(test_session, lg, _live_stats(away.fpl_id, 90), 25)
    _start_gw(test_session, gws, 25)

    entries = services._return_required_entries(test_session, lg)
    assert [x["player"] for x in entries] == ["Away"]


def test_several_international_absences_alert_independently(test_session):
    """The international list is uncapped (Item 6b) — each entry is its own alert."""
    lg, a, _b, gws = _seed(test_session)
    p1 = _player(test_session, lg, "One", 1)
    p2 = _player(test_session, lg, "Two", 2)
    _intl(test_session, a, p1, start=25)
    _intl(test_session, a, p2, start=25)
    services.record_absentee_minutes(
        test_session, lg,
        {**_live_stats(p1.fpl_id, 90), **_live_stats(p2.fpl_id, 45)}, 25,
    )
    _start_gw(test_session, gws, 25)

    entries = services._return_required_entries(test_session, lg)
    assert {x["player"] for x in entries} == {"One", "Two"}


def test_no_alert_without_logged_minutes(test_session):
    """On the absence list but hasn't actually played yet — nothing to alert on."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    _il(test_session, a, out, None, start=20)
    _start_gw(test_session, gws, 30)

    assert services._return_required_entries(test_session, lg) == []


def test_the_alert_appears_on_the_homepage_nag(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    eligible = services.il_return_eligible_gw(20)
    _il(test_session, a, out, None, start=20)
    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 90),
                                     eligible)
    _start_gw(test_session, gws, eligible)

    detail = next(x["detail"] for x in services.flagged_actions(test_session, lg)
                  if x["category"] == "Injury list")
    assert "Out" in detail and "playing again" in detail


def test_the_alert_appears_on_the_health_check(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    eligible = services.il_return_eligible_gw(20)
    _il(test_session, a, out, None, start=20)
    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 90),
                                     eligible)
    _start_gw(test_session, gws, eligible)

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert not checks["no absentee playing while still parked"]["ok"]


def test_the_health_check_is_ok_with_nobody_overdue(test_session):
    lg, a, _b, gws = _seed(test_session)
    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["no absentee playing while still parked"]["ok"]


# ---- Item 6c: the alert clears on reconciliation ----------------------------

def test_reconcile_absences_closes_the_entry_when_hes_re_added(test_session):
    """No test existed for this function before Item 6c."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    e = _il(test_session, a, out, None, start=20)
    _hold(test_session, a, out, gws, [LAST_GW])  # re-added on the latest roster

    closed = services.reconcile_absences(test_session, lg)

    test_session.refresh(e)
    assert closed == 1
    assert e.status == "returned" and e.end_gw == LAST_GW


def test_reconcile_absences_leaves_an_untouched_absence_alone(test_session):
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    e = _il(test_session, a, out, None, start=20)

    closed = services.reconcile_absences(test_session, lg)

    test_session.refresh(e)
    assert closed == 0
    assert e.status == "active"


def test_the_alert_disappears_once_reconciled(test_session):
    """He plays, triggers the alert; the manager re-adds him; the same sync's
    reconcile_absences closes the entry and the alert is gone."""
    lg, a, _b, gws = _seed(test_session)
    out = _player(test_session, lg, "Out", 1)
    eligible = services.il_return_eligible_gw(20)
    _il(test_session, a, out, None, start=20)
    services.record_absentee_minutes(test_session, lg, _live_stats(out.fpl_id, 90),
                                     eligible)
    _start_gw(test_session, gws, eligible)
    assert services._return_required_entries(test_session, lg)  # fires first

    _hold(test_session, a, out, gws, [LAST_GW])
    services.reconcile_absences(test_session, lg)

    assert services._return_required_entries(test_session, lg) == []
