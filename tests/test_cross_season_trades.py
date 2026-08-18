"""Trades, transactions, and future picks must survive a season rollover.

All three pages scoped their reads to the CURRENT league row; after the 26/27
rollover, every trade/transaction/future-pick row from 25/26 lives on the OLD row,
so the pages went blank. get_trades / get_all_transactions / get_future_picks now
read across every league row (transactions still diffs one league's own roster
snapshots internally — GW numbers repeat 1-38 every season).

A trade's season is computed ON READ, not from the storing row: an FPL-synced trade
(event_gw set) can't have crossed a season boundary, so the storing row's own
season_year is right; a commissioner-entered trade (no event_gw) is bucketed by
created_at against the Jan-31 deadline, so a post-GW38 offseason trade lands in the
FOLLOWING season.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime as dt

import services
from models import FuturePick, Gameweek, League, Manager, Player, PlTeam, Roster, Trade

_FPL = [0]


def _next_fpl():
    _FPL[0] += 1
    return _FPL[0]


def _league(session, *, season_year, is_current=False):
    lg = League(
        fpl_league_id=str(_next_fpl()), name=f"S{season_year}", season_year=season_year,
        is_current=is_current, sync_locked=not is_current, phase="offseason",
    )
    session.add(lg)
    session.flush()
    return lg


def _manager(session, lg, fpl_manager_id, name):
    m = Manager(league_id=lg.id, fpl_manager_id=fpl_manager_id, name=name, display_name=name)
    session.add(m)
    session.commit()
    return m


def _player(session, name, pos="MID"):
    fid = _next_fpl()
    p = Player(name=name, code=fid * 7, fpl_id=fid, position=pos, current_team="ARS")
    session.add(p)
    session.commit()
    return p


def _gws(session, lg, n=3):
    gws = {}
    for i in range(1, n + 1):
        g = Gameweek(number=i, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[i] = g
    session.commit()
    return gws


# ---- get_trades: cross-season grouping + season attribution ---------------

def test_trades_group_by_season_newest_first(test_session):
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)
    old_a = _manager(test_session, old, "1", "A")
    old_b = _manager(test_session, old, "2", "B")
    new_a = _manager(test_session, new, "1", "A")
    new_b = _manager(test_session, new, "2", "B")
    p_old = _player(test_session, "Old Player")
    p_new = _player(test_session, "New Player")

    test_session.add(Trade(
        league_id=old.id, from_manager=old_a.id, to_manager=old_b.id,
        player_id=p_old.id, event_gw=10,
    ))
    test_session.add(Trade(
        league_id=new.id, from_manager=new_a.id, to_manager=new_b.id,
        player_id=p_new.id, event_gw=5,
    ))
    test_session.commit()

    out = services.get_trades(test_session)
    years = [s["year"] for s in out]
    assert years == [2026, 2025]
    assert [t["what"] for t in out[0]["trades"]] == ["New Player"]
    assert [t["what"] for t in out[1]["trades"]] == ["Old Player"]


def test_synced_trade_lands_in_its_storing_rows_season(test_session):
    """event_gw set (FPL-synced, mid-season) -> the STORING row's season_year,
    regardless of when created_at happens to be."""
    old = _league(test_session, season_year=2025)
    a = _manager(test_session, old, "1", "A")
    b = _manager(test_session, old, "2", "B")
    p = _player(test_session, "Mid-Season Trade")
    test_session.add(Trade(
        league_id=old.id, from_manager=a.id, to_manager=b.id, player_id=p.id,
        event_gw=20, created_at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
    ))
    test_session.commit()

    out = services.get_trades(test_session)
    assert [s["year"] for s in out] == [2025]


def test_offseason_commissioner_trade_lands_in_the_following_season(test_session):
    """No event_gw -> bucket by created_at. An August (post-GW38) trade belongs to
    the season STARTING that year, i.e. the one about to be drafted."""
    old = _league(test_session, season_year=2025)
    a = _manager(test_session, old, "1", "A")
    b = _manager(test_session, old, "2", "B")
    p = _player(test_session, "Offseason Trade")
    test_session.add(Trade(
        league_id=old.id, from_manager=a.id, to_manager=b.id, player_id=p.id,
        event_gw=None, created_at=dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc),
    ))
    test_session.commit()

    out = services.get_trades(test_session)
    assert [s["year"] for s in out] == [2026]


def test_january_commissioner_trade_lands_in_the_prior_calendar_years_season(test_session):
    """A January trade belongs to the season that started the PREVIOUS calendar
    year (the trade deadline is Jan 31 of that season)."""
    old = _league(test_session, season_year=2025)
    a = _manager(test_session, old, "1", "A")
    b = _manager(test_session, old, "2", "B")
    p = _player(test_session, "January Trade")
    test_session.add(Trade(
        league_id=old.id, from_manager=a.id, to_manager=b.id, player_id=p.id,
        event_gw=None, created_at=dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc),
    ))
    test_session.commit()

    out = services.get_trades(test_session)
    assert [s["year"] for s in out] == [2025]


def test_cross_row_manager_names_resolve_for_both_rows(test_session):
    """The names map must span every league row — a per-league map would render
    a trade's from/to as None whenever its managers belong to a different row."""
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)
    a = _manager(test_session, old, "1", "A")
    b = _manager(test_session, old, "2", "B")
    _manager(test_session, new, "3", "C")  # unrelated manager on the other row
    p = _player(test_session, "P")
    test_session.add(Trade(league_id=old.id, from_manager=a.id, to_manager=b.id,
                           player_id=p.id, event_gw=1))
    test_session.commit()

    out = services.get_trades(test_session)
    row = out[0]["trades"][0]
    assert row["from"] == "A"
    assert row["to"] == "B"


def test_club_trade_renders_as_a_club_not_a_blank_player(test_session):
    """Pre-existing bug: a goalie-team trade (team_id set, pick_round and
    player_id both NULL) used to render kind='player', what='—'."""
    lg = _league(test_session, season_year=2025)
    a = _manager(test_session, lg, "1", "A")
    b = _manager(test_session, lg, "2", "B")
    team = PlTeam(code=3, fpl_id=3, short_name="ARS", name="Arsenal", is_current_pl=True)
    test_session.add(team)
    test_session.flush()
    test_session.add(Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id,
                           team_id=team.id, event_gw=1))
    test_session.commit()

    out = services.get_trades(test_session)
    row = out[0]["trades"][0]
    assert row["kind"] == "club"
    assert row["what"] == "Arsenal"


def test_corrections_data_still_yields_a_flat_trade_list(test_session):
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)
    old_a = _manager(test_session, old, "1", "A")
    old_b = _manager(test_session, old, "2", "B")
    _manager(test_session, new, "1", "A")
    _manager(test_session, new, "2", "B")
    p1 = _player(test_session, "Old Trade Player")
    test_session.add(Trade(league_id=old.id, from_manager=old_a.id, to_manager=old_b.id,
                           player_id=p1.id, event_gw=1))
    test_session.commit()

    data = services.corrections_data(test_session, new)
    assert isinstance(data["trades"], list)
    assert data["trades"], "expected at least one trade row"
    assert "year" not in data["trades"][0], "must be flat rows, not season groups"
    assert data["trades"][0]["what"] == "Old Trade Player"


# ---- get_all_transactions ----------------------------------------------------

def test_transactions_grouped_by_season_newest_first_no_cross_season_collision(test_session):
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)
    old_gws = _gws(test_session, old)
    new_gws = _gws(test_session, new)
    old_m = _manager(test_session, old, "1", "A")
    new_m = _manager(test_session, new, "1", "A")
    p_old_1 = _player(test_session, "Old GW1 Player")
    p_old_2 = _player(test_session, "Old GW2 Player")
    p_new_1 = _player(test_session, "New GW1 Player")
    p_new_2 = _player(test_session, "New GW2 Player")

    test_session.add(Roster(manager_id=old_m.id, player_id=p_old_1.id, gameweek_id=old_gws[1].id))
    test_session.add(Roster(manager_id=old_m.id, player_id=p_old_2.id, gameweek_id=old_gws[2].id))
    test_session.add(Roster(manager_id=new_m.id, player_id=p_new_1.id, gameweek_id=new_gws[1].id))
    test_session.add(Roster(manager_id=new_m.id, player_id=p_new_2.id, gameweek_id=new_gws[2].id))
    test_session.commit()

    out = services.get_all_transactions(test_session)
    years = [s["year"] for s in out]
    assert years == [2026, 2025]
    # each season's own diff, not a cross-season comparison of GW-numbered snapshots
    new_moves = out[0]["weeks"][0]["moves"]
    old_moves = out[1]["weeks"][0]["moves"]
    assert {m["player"] for m in new_moves} == {"New GW1 Player", "New GW2 Player"}
    assert {m["player"] for m in old_moves} == {"Old GW1 Player", "Old GW2 Player"}


def test_transactions_skips_seasons_with_no_moves(test_session):
    """A league row with no gameweeks/rosters at all (e.g. a brand-new season
    before FPL's first sync) contributes nothing — not an empty season entry."""
    _league(test_session, season_year=2024)
    out = services.get_all_transactions(test_session)
    assert not any(s["year"] == 2024 for s in out)


# ---- get_future_picks: cross-season, forward-looking only -------------------

def test_future_picks_same_year_on_two_rows_newer_row_wins(test_session):
    old = _league(test_session, season_year=2025)
    new = _league(test_session, season_year=2026, is_current=True)
    _manager(test_session, old, "1", "A")
    _manager(test_session, old, "2", "B")
    _manager(test_session, new, "1", "A")
    _manager(test_session, new, "3", "C")

    test_session.add(FuturePick(
        league_id=old.id, season_year=2027, draft_type="main", round=1,
        original_owner="A", owner="B",
    ))
    test_session.commit()
    test_session.add(FuturePick(
        league_id=new.id, season_year=2027, draft_type="main", round=1,
        original_owner="A", owner="C",
    ))
    test_session.commit()

    out = services.get_future_picks(test_session, new)
    season = next(s for s in out if s["year"] == 2027)
    assert season["main"] == [{"round": 1, "original_owner": "A", "owner": "C"}]


def test_future_picks_excludes_years_before_the_current_season(test_session):
    lg = _league(test_session, season_year=2026, is_current=True)
    _manager(test_session, lg, "1", "A")
    _manager(test_session, lg, "2", "B")
    test_session.add(FuturePick(
        league_id=lg.id, season_year=2025, draft_type="main", round=1,
        original_owner="A", owner="B",
    ))
    test_session.commit()

    out = services.get_future_picks(test_session, lg)
    assert not any(s["year"] == 2025 for s in out)


def test_future_picks_stay_forward_looking_from_an_old_row_too(test_session):
    """Future picks are season-agnostic and never migrated — a year at or after
    an OLDER row's own season_year still shows, even from that row's perspective."""
    old = _league(test_session, season_year=2025)
    _manager(test_session, old, "1", "A")
    _manager(test_session, old, "2", "B")
    test_session.add(FuturePick(
        league_id=old.id, season_year=2027, draft_type="main", round=2,
        original_owner="A", owner="B",
    ))
    test_session.commit()

    out = services.get_future_picks(test_session, old)
    assert any(s["year"] == 2027 for s in out)
