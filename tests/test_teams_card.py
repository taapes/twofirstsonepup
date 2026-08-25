"""The `/teams` roster card: uniform height via a kept-only default.

Cards in a CSS grid stretch to the tallest one in their visual row, so a single long
card made every neighbour tall — and which cards shared a row depended on the viewport,
not on the managers. The fix is `align-items:start` (CSS, untestable here) plus showing
only the KEPT players by default, with the rest behind a <details>.

The behaviour that needs pinning is the FALLBACK. Keeper selections for the upcoming
season don't exist until after GW38, so a literal "kept only" card would render empty
for most of the year. It must fall back to the full roster whenever nothing is kept, and
must never collapse mid-draft, where the arriving picks are the point of the card.

The partition and the label abbreviation are tested by rendering the template directly:
`acquisition` is derived from roster history, and fabricating a real "discovery" one
would test the derivation rather than the card. The route test guards the wiring.
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
    Roster,
    Standing,
)
from templating import templates


# ---- the card template itself ---------------------------------------------
def _render(players, phase_macro="in_season"):
    tpl = templates.env.get_template("_roster_card.html")
    return tpl.render(
        t={"manager": "A", "manager_fpl": "1", "players": players},
        phase={"macro": phase_macro},
    )


def _p(name, *, kept=False, acquisition="draft", years=3):
    return {"player": name, "position": "MID", "acquisition": acquisition,
            "years_remaining": years, "eligible": True, "reason": None,
            "kept": kept, "kept_discovery": False}


def test_with_nothing_kept_the_card_shows_everyone_and_does_not_collapse():
    """The in-season state, and the whole reason for the fallback: kept is False for
    every row until selections are submitted after GW38."""
    html = _render([_p("Alpha"), _p("Bravo"), _p("Charlie")])

    assert "<details" not in html
    for name in ("Alpha", "Bravo", "Charlie"):
        assert name in html


def test_kept_players_show_and_the_rest_go_behind_the_expand():
    html = _render([_p("Keeper One", kept=True), _p("Bravo"), _p("Charlie")])

    assert "<details" in html
    # Position matters, not mere presence: <details> hides its contents visually but
    # keeps them in the DOM, so `"Bravo" in html` would pass even if nothing collapsed.
    summary_at = html.index("<summary")
    assert html.index("Keeper One") < summary_at, "the kept player must be visible"
    assert html.index("Bravo") > summary_at, "the rest must be behind the expand"
    assert html.index("Charlie") > summary_at
    assert "+2 more" in html


def test_the_summary_never_says_keeper():
    """test_keeper_privacy's teams-page test asserts on a 120-char window after a
    player's name; the word "keeper" in this summary can land inside ANOTHER manager's
    window and fail it. Pinned here so the wording can't drift back."""
    html = _render([_p("Keeper One", kept=True), _p("Bravo")])
    summary = html[html.index("<summary"):html.index("</summary>")]

    assert "keeper" not in summary.lower()


def test_a_squad_that_is_all_keepers_does_not_render_an_empty_expand():
    html = _render([_p("Keeper One", kept=True), _p("Keeper Two", kept=True)])

    assert "<details" not in html


@pytest.mark.parametrize("phase_macro", ["draft", "preseason"])
def test_the_card_never_collapses_mid_draft(phase_macro):
    """These are exactly the phases where ui._teams_data swaps in
    get_teams_in_progress, and there the arriving picks ARE the card — hiding them to
    show the static kept players would be backwards."""
    html = _render([_p("Keeper One", kept=True), _p("Fresh Pick")], phase_macro)

    assert "<details" not in html
    assert "Fresh Pick" in html


def test_long_acquisition_labels_are_abbreviated_with_the_full_word_on_hover():
    """Label width was the second cause of uneven heights: `discovery` wraps inside a
    minmax(300px) column and adds a line."""
    html = _render([_p("Alpha", acquisition="discovery"),
                    _p("Bravo", acquisition="waiver"),
                    _p("Charlie", acquisition="draft")])

    assert '<span title="discovery">disc</span>' in html
    assert '<span title="waiver">waiv</span>' in html
    # already short — abbreviating these would only cost readability
    assert '<span title="draft">draft</span>' in html
    assert "acq" in html, "the nowrap class must be on the label cell"


def test_the_max_pill_and_keeper_badge_survive_the_refactor():
    """The row markup moved into a macro; these are the bits other pages' tests and the
    page subtitle refer to."""
    html = _render([_p("Alpha", kept=True, years=0)])

    assert 'class="pill warn">max<' in html
    assert "🔒 keeper" in html


# ---- route wiring ---------------------------------------------------------
@pytest.fixture
def client(test_session):
    """A TestClient sharing the test database (conftest patches db.SessionLocal, which
    get_db resolves at call time) — never the configured one."""
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _auth(monkeypatch):
    """Token bypass for the login gate: this page's content doesn't depend on which
    manager is viewing once keepers are revealed."""
    monkeypatch.setenv("SYNC_AUTH_TOKEN", "test-token-teams-card")
    return {"X-Auth-Token": "test-token-teams-card"}


def _seed(session, *, locked, kept_one):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="offseason", keepers_locked=locked)
    session.add(lg)
    session.flush()
    gw = Gameweek(number=1, league_id=lg.id)
    session.add(gw)
    session.flush()
    m = Manager(league_id=lg.id, fpl_manager_id="1", name="A", display_name="A")
    session.add(m)
    session.flush()
    session.add(Standing(league_id=lg.id, manager_id=m.id, rank=1, total=9,
                         points_for=100))
    for i, name in enumerate(["Alpha", "Bravo", "Charlie"], start=1):
        p = Player(name=name, code=i * 7, fpl_id=i, position="MID", current_team="ARS")
        session.add(p)
        session.flush()
        session.add_all([
            Roster(manager_id=m.id, gameweek_id=gw.id, player_id=p.id),
            PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=p.fpl_id, name=name,
                         position="MID", current_team="ARS"),
        ])
        if kept_one and name == "Alpha":
            session.add(KeeperSelection(league_id=lg.id, manager_id=m.id,
                                        player_id=p.id, season_year=2026))
    session.commit()
    return lg, m


def test_teams_page_shows_the_whole_squad_when_nothing_is_kept(
    client, test_session, monkeypatch
):
    """Production today: no selections for the upcoming season, so the page must look
    exactly as it did before this change."""
    _seed(test_session, locked=False, kept_one=False)

    body = client.get("/teams", headers=_auth(monkeypatch)).content.decode()
    assert "<details" not in body
    for name in ("Alpha", "Bravo", "Charlie"):
        assert name in body


def test_teams_page_collapses_once_selections_are_revealed(
    client, test_session, monkeypatch
):
    _seed(test_session, locked=True, kept_one=True)

    body = client.get("/teams", headers=_auth(monkeypatch)).content.decode()
    assert "<details" in body
    summary_at = body.index("<summary")
    assert body.index("Alpha") < summary_at
    assert body.index("Bravo") > summary_at
