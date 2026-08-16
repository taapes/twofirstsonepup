"""A manager typing "Sesko" during the live draft must find Šeško.

`search_players` filtered on `Player.name ILIKE '%q%'`, and 'Sesko' does not match
'Šeško' — š is a different character, so the query returned nothing. In the draft
search that is the worst possible failure shape: "no results" looks exactly like
"not in the pool", so the manager concludes the player is gone rather than that they
typed it without the diacritic. Real names this hits: Šeško, Ødegaard, Kadıoğlu,
Milosavljević.

The fix unaccents BOTH sides, so one condition covers every combination. The tests
below pin all four, plus the ø and ğ cases specifically — those are the ones a
hand-written translate() map gets wrong (NFKD has no decomposition for ø, which is
why scripts/import_projections.py carries its own _TRANSLIT table).

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import services
from models import Gameweek, League, Manager, Player, Standing

UPCOMING = 2026


def _seed(session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="draft")
    session.add(lg)
    session.flush()
    session.add(Gameweek(number=1, league_id=lg.id))
    session.flush()

    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i, total=10 - i,
                             points_for=100))

    session.add_all([
        # š — the one that actually bit, twice: once in the draft search and once
        # when this session searched for him and wrongly concluded he'd left the PL.
        Player(name="Šeško", code=1, fpl_id=1, position="FWD", current_team="MUN"),
        # ø — NFKD does not decompose it, so an ascii-ignore pass yields 'degaard'.
        Player(name="Ødegaard", code=2, fpl_id=2, position="MID", current_team="ARS"),
        # ı and ğ — ğ is the character a hand-rolled translate() map missed first try.
        Player(name="Kadıoğlu", code=3, fpl_id=3, position="DEF", current_team="FUL"),
        # Plain ASCII: the control. Must be completely unaffected.
        Player(name="Haaland", code=4, fpl_id=4, position="FWD", current_team="MCI"),
    ])
    session.commit()
    return lg


def _names(session, lg, q):
    return {r["name"] for r in services.search_players(
        session, lg, q=q, available_year=UPCOMING
    )}


def test_an_ascii_query_finds_an_accented_name(test_session):
    lg = _seed(test_session)
    assert _names(test_session, lg, "Sesko") == {"Šeško"}


def test_the_accented_query_still_works(test_session):
    """The fix must not trade one failure for the other — someone with the keyboard
    to type it, or pasting from elsewhere, still has to find him."""
    lg = _seed(test_session)
    assert _names(test_session, lg, "Šeško") == {"Šeško"}


def test_o_slash_is_handled(test_session):
    """NFKD alone turns 'Ødegaard' into 'degaard', so 'Odegaard' would still miss."""
    lg = _seed(test_session)
    assert _names(test_session, lg, "Odegaard") == {"Ødegaard"}


def test_dotless_i_and_g_breve_are_handled(test_session):
    lg = _seed(test_session)
    assert _names(test_session, lg, "Kadioglu") == {"Kadıoğlu"}


def test_a_partial_ascii_query_matches_mid_name(test_session):
    """Managers type fragments, not full names — the draft box searches on keyup."""
    lg = _seed(test_session)
    assert _names(test_session, lg, "esk") == {"Šeško"}


def test_plain_ascii_names_are_unaffected(test_session):
    lg = _seed(test_session)
    assert _names(test_session, lg, "Haaland") == {"Haaland"}


def test_a_query_matching_nothing_still_returns_nothing(test_session):
    """Guard against the unaccent wrapper accidentally widening the match to
    everything — a filter that never excludes is worse than the original bug."""
    lg = _seed(test_session)
    assert _names(test_session, lg, "Ronaldo") == set()
