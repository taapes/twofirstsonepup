"""Projections on the Players tab: the read path and the rendered table.

Two things are easy to get wrong and expensive when wrong. First, projected and actual
stats have near-identical names (both have G, A, CS, Min), so a crossed assignment
reads perfectly plausible. Second, the tab is OWNER-ONLY by deliberate choice — the
draft board every manager sees must never leak these numbers.

The template tests render Jinja directly (no DB, no TestClient) because the hazard
they guard is structural: a grouped header whose colspans no longer match the columns
beneath it.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

from html.parser import HTMLParser

import pytest

import services
from models import League, Player, PlayerProjection, PlayerSeason
from templating import templates


def _player(session, name, code, fpl_id, team="MCI", pos="FWD"):
    p = Player(name=name, code=code, fpl_id=fpl_id, current_team=team, position=pos,
               price=50, status="a")
    session.add(p)
    session.flush()
    return p


def _league(session, fpl_id, year, *, current=True, locked=False, phase="offseason"):
    lg = League(fpl_league_id=fpl_id, name=f"S{year}", season_year=year,
                is_current=current, sync_locked=locked, phase=phase)
    session.add(lg)
    session.flush()
    return lg


def _proj(session, player, year=2026, **over):
    vals = {"raw_name": player.name, "raw_team": player.current_team,
            "raw_position": player.position, "price": 15.5, "minutes": 3120.0,
            "goals_scored": 27.5, "assists": 5.8, "clean_sheets": 1.5, "bonus": 34.7,
            "defensive_contributions": 0.4, "yellow_cards": 3.2, "points": 231.0}
    vals.update(over)
    session.add(PlayerProjection(season_year=year, player_id=player.id, **vals))
    session.flush()


# ---- the read path --------------------------------------------------------
def test_portal_exposes_every_projected_stat(test_session):
    """Nine distinct values, so a right-label/wrong-field wiring bug fails here."""
    lg = _league(test_session, "1", 2025, locked=True)
    p = _player(test_session, "Haaland", 111, 5)
    _proj(test_session, p)
    test_session.commit()

    (row,) = services.player_portal(test_session, lg)
    assert row["proj_price"] == 15.5
    assert row["proj_points"] == 231.0
    assert row["proj_minutes"] == 3120.0
    assert row["proj_goals"] == 27.5
    assert row["proj_assists"] == 5.8
    assert row["proj_clean_sheets"] == 1.5
    assert row["proj_bonus"] == 34.7
    assert row["proj_defensive_contributions"] == 0.4
    assert row["proj_yellow_cards"] == 3.2


def test_projected_price_is_not_divided_like_the_live_one(test_session):
    """players.price is tenths; the sheet's price is already £m. Dividing it would
    show Haaland at £1.55 and make Val ten times too big."""
    lg = _league(test_session, "1", 2025, locked=True)
    p = _player(test_session, "Haaland", 111, 5)   # price=50 -> £5.0 live
    _proj(test_session, p, price=15.5)
    test_session.commit()

    (row,) = services.player_portal(test_session, lg)
    assert row["price"] == 5.0, "live price is tenths and must still be divided"
    assert row["proj_price"] == 15.5


def test_value_is_points_over_price(test_session):
    lg = _league(test_session, "1", 2025, locked=True)
    p = _player(test_session, "Haaland", 111, 5)
    _proj(test_session, p, points=231.0, price=15.5)
    test_session.commit()

    (row,) = services.player_portal(test_session, lg)
    assert row["proj_value"] == pytest.approx(14.903, abs=1e-3)


def test_value_is_computed_on_read_not_stored(test_session):
    """Change the points and the value must move with them — a stored column would
    quietly disagree with the two numbers either side of it."""
    lg = _league(test_session, "1", 2025, locked=True)
    p = _player(test_session, "Haaland", 111, 5)
    _proj(test_session, p, points=100.0, price=10.0)
    test_session.commit()
    assert services.player_portal(test_session, lg)[0]["proj_value"] == 10.0

    test_session.query(PlayerProjection).one().points = 50.0
    test_session.commit()
    assert services.player_portal(test_session, lg)[0]["proj_value"] == 5.0


@pytest.mark.parametrize("price", [0.0, None])
def test_value_is_blank_rather_than_a_crash_when_price_is_missing(test_session, price):
    lg = _league(test_session, "1", 2025, locked=True)
    p = _player(test_session, "Haaland", 111, 5)
    _proj(test_session, p, price=price)
    test_session.commit()

    (row,) = services.player_portal(test_session, lg)
    assert row["proj_value"] is None
    assert row["proj_points"] == 231.0, "the projection itself is still real"


def test_a_zero_projection_is_not_the_same_as_no_projection(test_session):
    """The source says 46 players won't play. That is a real forecast of 0, and it
    must not render identically to 'we have no data on this player'."""
    lg = _league(test_session, "1", 2025, locked=True)
    zero = _player(test_session, "Benched", 111, 5)
    absent = _player(test_session, "Uncovered", 222, 6)
    _proj(test_session, zero, points=0.0)
    test_session.commit()

    rows = {r["name"]: r for r in services.player_portal(test_session, lg)}
    assert rows["Benched"]["proj_points"] == 0.0
    assert rows["Uncovered"]["proj_points"] is None


def test_a_player_without_a_projection_is_still_listed(test_session):
    """Historical players will never have one; a few live ones are missing from the
    file. Both stay in the list, blank."""
    lg = _league(test_session, "1", 2025, locked=True)
    _player(test_session, "Departed", None, None, team="WOL")
    test_session.commit()

    (row,) = services.player_portal(test_session, lg)
    assert row["name"] == "Departed"
    assert all(row[k] is None for k in row if k.startswith("proj_"))


def test_portal_ignores_another_seasons_projection(test_session):
    lg = _league(test_session, "1", 2025, locked=True)
    p = _player(test_session, "Haaland", 111, 5)
    _proj(test_session, p, year=2025, points=1.0)
    _proj(test_session, p, year=2026, points=231.0)
    test_session.commit()

    (row,) = services.player_portal(test_session, lg)
    assert row["proj_points"] == 231.0


def test_actual_and_projected_stats_do_not_cross(test_session):
    """Both blocks carry a G, an A, a CS and a Min. A crossed assignment would pass
    every other test in this file."""
    lg = _league(test_session, "1", 2025, locked=True)
    p = _player(test_session, "Haaland", 111, 5)
    test_session.add(PlayerSeason(
        league_id=lg.id, player_id=p.id, fpl_id=5, name=p.name, position=p.position,
        current_team=p.current_team, total_points=209, goals_scored=3, assists=5,
        clean_sheets=12, minutes=2750,
    ))
    _proj(test_session, p, points=231.0, goals_scored=27.5, assists=5.8,
          clean_sheets=1.5, minutes=3120.0)
    test_session.commit()

    (row,) = services.player_portal(test_session, lg)
    assert (row["total_points"], row["goals_scored"], row["minutes"]) == (209, 3, 2750)
    assert (row["proj_points"], row["proj_goals"], row["proj_minutes"]) == \
        (231.0, 27.5, 3120.0)


def test_the_draft_board_search_never_exposes_projections(test_session):
    """The Players tab is owner-only by deliberate choice (fa243f2); the draft board
    is what all ten managers see. Encoded as a rule, not a comment."""
    lg = _league(test_session, "1", 2025, locked=True)
    p = _player(test_session, "Haaland", 111, 5)
    _proj(test_session, p)
    test_session.commit()

    (row,) = services.search_players(test_session, lg)
    assert not [k for k in row if k.startswith("proj")], row


# ---- the rendered table ---------------------------------------------------
class _Table(HTMLParser):
    """Counts the header cells of #phead, the group row's colspans, and the <td>s of
    the first body row."""

    def __init__(self):
        super().__init__()
        self.group_span = 0
        self.head_cols = 0
        self.body_cols = 0
        self._in = None
        self._body_row = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            if "grouphead" in (a.get("class") or ""):
                self._in = "group"
            elif a.get("id") == "phead":
                self._in = "head"
            else:
                self._in = "body"
                self._body_row += 1
        elif tag == "th" and self._in == "group":
            self.group_span += int(a.get("colspan", 1))
        elif tag == "th" and self._in == "head":
            self.head_cols += 1
        elif tag == "td" and self._in == "body" and self._body_row == 1:
            self.body_cols += 1


def _render(**over):
    ctx = {
        "hide_nav": True,
        "stats_season_label": "25/26",
        "projection_season_label": "26/27",
        "projection_count": 1,
        "pool": {"synced_at": None, "live": 577, "historical": 387},
        "players": [{
            "fpl_id": 5, "name": "Haaland", "position": "FWD", "team": "MCI",
            "status": "a", "news": None, "price": 5.0, "total_points": 209,
            "points_per_game": 6.5, "goals_scored": 3, "assists": 5,
            "clean_sheets": 12, "bonus": 18, "minutes": 2750, "ict_index": 200.0,
            "proj_price": 15.5, "proj_points": 231.0, "proj_value": 14.903,
            "proj_minutes": 3120.0, "proj_goals": 27.5, "proj_assists": 5.8,
            "proj_clean_sheets": 1.5, "proj_bonus": 34.7,
            "proj_defensive_contributions": 0.4, "proj_yellow_cards": 3.2,
            "owner": None, "rostered": False, "on_il": False, "ineligible": False,
            "acquisition": None, "keeper_years": None, "keeper_eligible": None,
        }],
    }
    ctx.update(over)
    return templates.env.get_template("admin_players.html").render(**ctx)


def test_header_group_and_body_columns_all_line_up():
    """The hazard of a grouped header: add a <th> and forget the <td>, or edit a
    colspan, and every cell after it renders under the wrong label."""
    t = _Table()
    t.feed(_render())
    assert t.head_cols == 29
    assert t.body_cols == 29
    assert t.group_span == 29


def test_the_projected_group_disappears_cleanly_before_any_import():
    t = _Table()
    html = _render(projection_season_label=None)
    t.feed(html)
    assert "PROJECTED" not in html
    assert 'id="f-proj"' not in html
    assert t.head_cols == t.body_cols == t.group_span == 19


def test_sorting_binds_to_the_label_row_not_the_group_row():
    """tHead.rows[0] is now the decorative group row, whose 4 colspan cells don't
    align with 29 <td>s — the one guaranteed breakage of this layout."""
    html = _render()
    assert "getElementById('phead')" in html
    # the binding itself, not the prose — the comment above it names the trap
    assert ".rows[0].cells" not in html


def test_projected_numbers_are_formatted_not_dumped():
    """Floats printed bare read as 27.500000000000004."""
    html = _render()
    assert ">27.5<" in html and ">3120<" in html
    assert "0000000" not in html


def test_a_missing_projection_renders_as_a_dash():
    html = _render(players=[{
        "fpl_id": 9, "name": "Uncovered", "position": "MID", "team": "BHA",
        "status": "a", "news": None, "price": 5.0, "total_points": None,
        "points_per_game": None, "goals_scored": None, "assists": None,
        "clean_sheets": None, "bonus": None, "minutes": None, "ict_index": None,
        "proj_price": None, "proj_points": None, "proj_value": None,
        "proj_minutes": None, "proj_goals": None, "proj_assists": None,
        "proj_clean_sheets": None, "proj_bonus": None,
        "proj_defensive_contributions": None, "proj_yellow_cards": None,
        "owner": None, "rostered": False, "on_il": False, "ineligible": False,
        "acquisition": None, "keeper_years": None, "keeper_eligible": None,
    }], projection_count=0)
    t = _Table()
    t.feed(html)
    assert t.body_cols == 29
    assert html.count("—") >= 10
