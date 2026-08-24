"""The keeper clock belongs to the PLAYER, not the (manager, player) pair.

Before this, a clock lived in exactly two manager-scoped places — this manager's own
KeeperSeed, and a recursive lookup of a trade sender's clock. Nothing recorded that
anyone used to own a dropped player, so A could hold a player with one keeper year
left, drop him, and B would claim him off waivers with a full fresh clock. If the
clock was EXHAUSTED, B could keep a player nobody was allowed to keep any more.

The rule is MID-SEASON only: within a season the clock follows the player through a
drop, capped at KEEPER_FRESH_WAIVER and still labelled 'waiver' (so it still eats one
of the two waiver keeper slots). At the rollover anyone not kept resets, which is what
advance_season already does by iterating KeeperSelection — hence no ledger table and
no rollover change.

Also pinned here: "on the GW1 roster" was only ever a PROXY for "drafted", and a
preseason free-agent signing lands on GW1 too. Where a season's main-draft picks are
actually recorded we consult them; where they are not (every season before 2026) the
proxy stands, or every historical keeper would regress to 'waiver'.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import services
from models import (
    DraftPick,
    Gameweek,
    KeeperSeed,
    League,
    Manager,
    Player,
    PlayerSeason,
    Roster,
    Trade,
)
from rules import KEEPER_FRESH_DRAFT, KEEPER_FRESH_WAIVER

LAST_GW = 38
SEASON = 2025
ALL_GWS = range(1, LAST_GW + 1)


def _seed(session, managers=("A", "B", "C")):
    """A finished season with every gameweek present — a sparse calendar would make
    _dropped see a gap in every tenure and every player would derive as 'waiver'."""
    lg = League(fpl_league_id="1", name="S", season_year=SEASON, is_current=True,
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
        mgrs[name] = m
    session.commit()
    return lg, mgrs, gws


def _player(session, lg, name, fpl_id, pos="MID", full_name=None):
    p = Player(name=name, code=fpl_id * 7, fpl_id=fpl_id, position=pos,
               current_team="ARS", price=50, status="a", full_name=full_name)
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id, name=name,
                             position=pos, current_team="ARS"))
    session.commit()
    return p


def _hold(session, mgr, player, gws, numbers):
    for n in numbers:
        session.add(Roster(manager_id=mgr.id, gameweek_id=gws[n].id, player_id=player.id))
    session.commit()


def _seed_clock(session, lg, mgr, player, years):
    session.add(KeeperSeed(league_id=lg.id, manager_id=mgr.id, player_id=player.id,
                           years_remaining=years, season_year=SEASON))
    session.commit()


def _pick(session, lg, mgr, player=None, *, label=None, number=1):
    session.add(DraftPick(
        league_id=lg.id, season_year=SEASON, draft_type="main", round=1,
        pick_number=number, manager_id=mgr.id,
        player_id=player.id if player else None, player_label=label,
    ))
    session.commit()


def _status(session, lg):
    return services._derive_keeper_status(session, lg, kept_all=True)


# ---- the clock survives a drop ---------------------------------------------

def test_a_claimant_inherits_the_dropped_players_clock(test_session):
    """A kept him with 2 years left and dropped him; B claims him off waivers. B gets
    2, not a fresh 3 — the whole point of the rule."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(1, 11))
    _hold(test_session, m["B"], p, gws, range(11, LAST_GW + 1))
    _seed_clock(test_session, lg, m["A"], p, 2)

    got = _status(test_session, lg)[m["B"].id][p.id]
    assert got["acquisition"] == "waiver", "still a waiver pickup, still eats a slot"
    assert got["years_remaining"] == 2


def test_an_exhausted_clock_cannot_be_kept_by_the_claimant(test_session):
    """The headline case: a player nobody may keep any more stays that way."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(1, 11))
    _hold(test_session, m["B"], p, gws, range(11, LAST_GW + 1))
    _seed_clock(test_session, lg, m["A"], p, 0)

    got = _status(test_session, lg)[m["B"].id][p.id]
    assert got["years_remaining"] == 0
    assert got["eligible"] is False


def test_a_clock_above_the_waiver_cap_is_capped(test_session):
    """A drafted player (4) picked off waivers still arrives at 3 — a waiver
    acquisition is worth a year less, and that hasn't changed."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(1, 11))
    _hold(test_session, m["B"], p, gws, range(11, LAST_GW + 1))
    _seed_clock(test_session, lg, m["A"], p, 4)

    assert _status(test_session, lg)[m["B"].id][p.id]["years_remaining"] == \
        KEEPER_FRESH_WAIVER


def test_a_genuine_free_agent_still_gets_the_fresh_waiver_clock(test_session):
    """Nobody held him earlier, so there is no clock to inherit."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["B"], p, gws, range(11, LAST_GW + 1))

    got = _status(test_session, lg)[m["B"].id][p.id]
    assert got["acquisition"] == "waiver"
    assert got["years_remaining"] == KEEPER_FRESH_WAIVER


def test_the_clock_carries_through_a_chain_of_claims(test_session):
    """A -> dropped -> B -> dropped -> C. C inherits the real number, not a reset."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(1, 11))
    _hold(test_session, m["B"], p, gws, range(11, 21))
    _hold(test_session, m["C"], p, gws, range(21, LAST_GW + 1))
    _seed_clock(test_session, lg, m["A"], p, 1)

    assert _status(test_session, lg)[m["C"].id][p.id]["years_remaining"] == 1


def test_a_prior_holder_who_arrived_by_trade_still_passes_his_clock_on(test_session):
    """The case a seed-only lookup would miss: B never had a seed of his own, he got
    the player (and the clock) by trade, then dropped him."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(1, 11))
    _hold(test_session, m["B"], p, gws, range(11, 21))
    _seed_clock(test_session, lg, m["A"], p, 2)
    test_session.add(Trade(league_id=lg.id, from_manager=m["A"].id,
                           to_manager=m["B"].id, player_id=p.id,
                           fpl_trade_id="1", event_gw=11))
    test_session.commit()
    _hold(test_session, m["C"], p, gws, range(21, LAST_GW + 1))

    assert _status(test_session, lg)[m["C"].id][p.id]["years_remaining"] == 2


def test_a_commissioner_seed_still_wins_over_an_inherited_clock(test_session):
    """The override is the correction of record and outranks everything."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(1, 11))
    _hold(test_session, m["B"], p, gws, range(11, LAST_GW + 1))
    _seed_clock(test_session, lg, m["A"], p, 1)
    _seed_clock(test_session, lg, m["B"], p, 3)

    assert _status(test_session, lg)[m["B"].id][p.id]["years_remaining"] == 3


def test_a_deliberate_zero_seed_is_not_overwritten_by_an_inherited_clock(test_session):
    """0 is falsy but meaningful — 'maxed out'. Testing truthiness instead of `is
    None` would hand the years straight back."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(1, 11))
    _hold(test_session, m["B"], p, gws, range(11, LAST_GW + 1))
    _seed_clock(test_session, lg, m["A"], p, 3)
    _seed_clock(test_session, lg, m["B"], p, 0)

    got = _status(test_session, lg)[m["B"].id][p.id]
    assert got["years_remaining"] == 0
    assert got["eligible"] is False


def test_a_manager_who_drops_and_re_adds_his_own_player_is_unchanged(test_session):
    """Already handled by his own seed plus the `dropped` flag — the new fallback
    must not reach for someone else's clock here."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, [g for g in ALL_GWS if g != 20])
    _seed_clock(test_session, lg, m["A"], p, 2)

    got = _status(test_session, lg)[m["A"].id][p.id]
    assert got["acquisition"] == "waiver", "an unexplained gap is still a drop"
    assert got["years_remaining"] == 2


# ---- drafted vs. on the GW1 roster -----------------------------------------

def test_a_drafted_player_still_gets_the_full_draft_clock(test_session):
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _pick(test_session, lg, m["A"], p, number=1)

    got = _status(test_session, lg)[m["A"].id][p.id]
    assert (got["acquisition"], got["years_remaining"]) == ("draft", KEEPER_FRESH_DRAFT)


def test_a_preseason_free_agent_does_not_get_a_draft_clock(test_session):
    """On the GW1 roster but not in the draft: signed after it, so waiver-length."""
    lg, m, gws = _seed(test_session)
    drafted = _player(test_session, lg, "Drafted", 1)
    signed = _player(test_session, lg, "Signed", 2)
    _hold(test_session, m["A"], drafted, gws, ALL_GWS)
    _hold(test_session, m["A"], signed, gws, ALL_GWS)
    _pick(test_session, lg, m["A"], drafted, number=1)

    st = _status(test_session, lg)[m["A"].id]
    assert st[drafted.id]["acquisition"] == "draft"
    assert st[signed.id]["acquisition"] == "waiver"
    assert st[signed.id]["years_remaining"] == KEEPER_FRESH_WAIVER


def test_a_season_with_no_recorded_picks_keeps_the_gw1_proxy(test_session):
    """Every season before 2026 predates the live draft board. Reading their silence
    as 'undrafted' would regress every historical keeper to waiver."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)

    got = _status(test_session, lg)[m["A"].id][p.id]
    assert (got["acquisition"], got["years_remaining"]) == ("draft", KEEPER_FRESH_DRAFT)


def test_the_guard_is_per_manager_not_per_league(test_session):
    """B has picks recorded, A has none. A keeps the proxy; B doesn't."""
    lg, m, gws = _seed(test_session)
    a_p = _player(test_session, lg, "AP", 1)
    b_p = _player(test_session, lg, "BP", 2)
    _hold(test_session, m["A"], a_p, gws, ALL_GWS)
    _hold(test_session, m["B"], b_p, gws, ALL_GWS)
    _pick(test_session, lg, m["B"], None, label="Somebody Unresolvable", number=2)
    _pick(test_session, lg, m["B"], b_p, number=3)

    st = _status(test_session, lg)
    assert st[m["A"].id][a_p.id]["acquisition"] == "draft", "no picks for A -> proxy"


def test_a_label_only_pick_still_reads_as_drafted(test_session):
    """Three real 26/27 picks carry free text and no player_id, because the player
    had no `players` row when the pick was made. A player_id-keyed lookup would call
    them undrafted and quietly dock each of them a keeper year."""
    lg, m, gws = _seed(test_session)
    dias = _player(test_session, lg, "Dias", 1, full_name="Ruben Dias")
    other = _player(test_session, lg, "Other", 2, full_name="Someone Else")
    _hold(test_session, m["A"], dias, gws, ALL_GWS)
    _hold(test_session, m["A"], other, gws, ALL_GWS)
    _pick(test_session, lg, m["A"], None, label="Ruben Dias", number=1)
    _pick(test_session, lg, m["A"], other, number=2)

    st = _status(test_session, lg)[m["A"].id]
    assert st[dias.id]["acquisition"] == "draft"
    assert st[dias.id]["years_remaining"] == KEEPER_FRESH_DRAFT


def test_an_unresolvable_label_keeps_the_proxy_for_that_manager(test_session):
    """Missing evidence must not read as 'undrafted' — the pick we couldn't parse
    might be the very player being asked about."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1, full_name="Real Player")
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    _pick(test_session, lg, m["A"], None, label="Nobody On This Roster", number=1)

    got = _status(test_session, lg)[m["A"].id][p.id]
    assert got["acquisition"] == "draft", "unreadable pick -> fall back to the proxy"


def test_a_drafted_player_dropped_and_reclaimed_carries_his_clock(test_session):
    """The two halves together: drafted (4), dropped, claimed by B -> capped to 3."""
    lg, m, gws = _seed(test_session)
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, range(1, 11))
    _hold(test_session, m["B"], p, gws, range(11, LAST_GW + 1))
    _pick(test_session, lg, m["A"], p, number=1)

    got = _status(test_session, lg)[m["B"].id][p.id]
    assert (got["acquisition"], got["years_remaining"]) == ("waiver", KEEPER_FRESH_WAIVER)


def test_a_goalie_team_pick_is_not_evidence_about_players(test_session):
    """A club pick names a CLUB. Counting it as "this manager's picks are on record"
    made every outfielder on his GW1 roster read as an undrafted free agent — the
    goalie-team keeper suite caught this."""
    from models import PlTeam

    lg, m, gws = _seed(test_session)
    club = PlTeam(name="Arsenal", short_name="ARS", code=3, is_current_pl=True)
    test_session.add(club)
    test_session.flush()
    p = _player(test_session, lg, "P", 1)
    _hold(test_session, m["A"], p, gws, ALL_GWS)
    test_session.add(DraftPick(
        league_id=lg.id, season_year=SEASON, draft_type="main", round=1,
        pick_number=1, manager_id=m["A"].id, team_id=club.id,
    ))
    test_session.commit()

    got = _status(test_session, lg)[m["A"].id][p.id]
    assert got["acquisition"] == "draft", "a club pick says nothing about players"
