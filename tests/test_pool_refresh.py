"""The global player pool refreshes between seasons — without disturbing history.

`players` used to be frozen whenever every season was `sync_locked`, because sync
keyed on `fpl_id` and refreshing rewrote each row's identity. That guard is gone now
that sync matches on the permanent `code` and each finished season's identity/stats
are frozen in `player_season`. It had to go: between seasons the live feed is the only
source of promoted clubs and new signings, and a stale pool is what people would
otherwise draft from.

The load-bearing property, asserted here: a refresh must never touch a frozen
season's `player_season` rows.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import asyncio

import pytest

import services
import sync
from models import League, Player, PlayerSeason

GKP, DEF, MID, FWD = 1, 2, 3, 4


def _feed(elements, teams=("ARS", "LIV")):
    return {
        "elements": elements,
        "element_types": [
            {"id": i, "singular_name_short": s}
            for i, s in {GKP: "GKP", DEF: "DEF", MID: "MID", FWD: "FWD"}.items()
        ],
        "teams": [{"id": i + 1, "short_name": t} for i, t in enumerate(teams)],
    }


def _el(fpl_id, code, name, pos=DEF, team=1):
    return {"id": fpl_id, "code": code, "web_name": name, "element_type": pos,
            "team": team, "status": "a", "total_points": 0}


def _run(payload, monkeypatch):
    async def _get_json(client, url):
        return payload

    monkeypatch.setattr(sync, "_get_json", _get_json)
    asyncio.run(sync.sync_players())


def _frozen_league(session, fpl_id="1", year=2025):
    lg = League(fpl_league_id=fpl_id, name=f"S{year}", season_year=year,
                is_current=True, sync_locked=True, phase="offseason")
    session.add(lg)
    session.flush()
    return lg


# --------------------------------------------------------------------------
def test_pool_refreshes_even_though_every_season_is_frozen(test_session, monkeypatch):
    """The regression this whole change is about: before, this returned early and the
    pool stayed months stale — no promoted clubs, no new signings."""
    _frozen_league(test_session)
    test_session.commit()

    _run(_feed([_el(1, 111, "Newcomer", FWD, team=2)]), monkeypatch)
    test_session.expire_all()

    p = test_session.query(Player).filter_by(code=111).one()
    assert p.name == "Newcomer" and p.current_team == "LIV"


def test_refresh_does_not_touch_a_frozen_season_snapshot(test_session, monkeypatch):
    """The load-bearing assertion. `players` may move on; a finished season's frozen
    identity and stats must not."""
    lg = _frozen_league(test_session)
    p = Player(name="Gabriel", code=111, fpl_id=5, current_team="ARS",
               position="DEF", price=50, status="a")
    test_session.add(p)
    test_session.flush()
    test_session.add(PlayerSeason(
        league_id=lg.id, player_id=p.id, fpl_id=5, name="Gabriel", position="DEF",
        current_team="ARS", price=50, status="a", total_points=209, minutes=2750,
    ))
    test_session.commit()

    def snapshot():
        return {
            (r.name, r.current_team, r.fpl_id, r.total_points, r.minutes, r.price)
            for r in test_session.query(PlayerSeason).filter_by(league_id=lg.id)
        }

    before = snapshot()
    # a new season: same human, new element id, new club, and the stats reset
    _run(_feed([_el(12, 111, "Gabriel", DEF, team=2)]), monkeypatch)
    test_session.expire_all()

    assert snapshot() == before, "a frozen season's snapshot was rewritten"
    # ...while the live pool did move on
    live = test_session.query(Player).filter_by(code=111).one()
    assert (live.fpl_id, live.current_team) == (12, "LIV")
    assert live.id == p.id, "same human, same row"


def test_refresh_adds_players_from_newly_promoted_clubs(test_session, monkeypatch):
    """The concrete complaint: whole clubs were missing from the draft pool."""
    _frozen_league(test_session)
    existing = Player(name="Gabriel", code=111, fpl_id=5, current_team="ARS",
                      position="DEF", price=50, status="a")
    test_session.add(existing)
    test_session.commit()

    _run(_feed([_el(5, 111, "Gabriel", DEF, team=1),
                _el(6, 999, "Promoted", FWD, team=2)]), monkeypatch)
    test_session.expire_all()

    names = {p.name for p in test_session.query(Player)}
    assert names == {"Gabriel", "Promoted"}


def test_departed_player_is_kept_for_history_but_leaves_the_live_pool(
    test_session, monkeypatch
):
    """They must stay visible to historical pages, but stop being draftable."""
    lg = _frozen_league(test_session)
    gone = Player(name="Departed", code=111, fpl_id=5, current_team="BUR",
                  position="FWD", price=50, status="a")
    test_session.add(gone)
    test_session.flush()
    test_session.add(PlayerSeason(
        league_id=lg.id, player_id=gone.id, fpl_id=5, name="Departed",
        position="FWD", current_team="BUR", total_points=90,
    ))
    test_session.commit()

    _run(_feed([_el(5, 222, "SomeoneElse", FWD)]), monkeypatch)
    test_session.expire_all()

    row = test_session.query(Player).filter_by(code=111).one()
    assert row.name == "Departed", "history would lose the name"
    assert row.fpl_id is None, "should have released its element id"
    fresh = services.player_pool_freshness(test_session)
    assert fresh["live"] == 1 and fresh["historical"] == 1


def test_keeper_candidate_without_an_element_id_is_marked_unavailable(test_session):
    """A departed player has no fpl_id, and the keeper form submits by fpl_id — so
    they must be surfaced as ineligible rather than silently failing on submit."""
    from models import Gameweek, KeeperSelection, Manager, Roster

    lg = _frozen_league(test_session)
    mgr = Manager(league_id=lg.id, fpl_manager_id="1", name="T", display_name="T")
    gw = Gameweek(number=1, league_id=lg.id)
    gone = Player(name="Departed", code=111, fpl_id=None, current_team="BUR",
                  position="FWD")
    # A SECOND departed player who IS a submitted keeper. Both have fpl_id None, so
    # keying "selected" on fpl_id puts a None key in the map and makes the first one
    # read as selected too. Without this row the bug is invisible.
    other = Player(name="AlsoDeparted", code=222, fpl_id=None, current_team="WHU",
                   position="MID")
    test_session.add_all([mgr, gw, gone, other])
    test_session.flush()
    test_session.add_all([
        Roster(manager_id=mgr.id, gameweek_id=gw.id, player_id=gone.id),
        Roster(manager_id=mgr.id, gameweek_id=gw.id, player_id=other.id),
        KeeperSelection(league_id=lg.id, manager_id=mgr.id, player_id=other.id,
                        season_year=(lg.season_year or 0) + 1),
    ])
    test_session.commit()

    out = services.keeper_candidates(test_session, lg, "1")
    rows = {r["player"]: r for r in out["players"]}
    assert "Departed" in rows, f"expected the roster player, got {list(rows)}"
    row = rows["Departed"]
    assert row["fpl_id"] is None
    assert row["eligible"] is False
    assert "Premier League" in (row.get("reason") or "")
    assert row["selected"] is False, (
        "a departed player has fpl_id None; keying 'selected' on fpl_id would make "
        "every one of them look selected"
    )
