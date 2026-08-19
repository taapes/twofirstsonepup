"""Suggesting a player for a free-text discovery pick — and never acting on it.

A discovery pick is recorded in September as a NAME, because the player isn't in the
Premier League yet and has no `players` row to point at. He becomes keepable only once
he joins, and only a LINK to his real row gives him the 4-year discovery keeper clock
(`link_discovery_pick`). Finding him by hand in an ~800-row pool, months later, having
forgotten how the name was spelled on draft night, is the part that doesn't happen.

So the daily sync proposes candidates. The hard rule is that it only ever proposes:
`match_discovery_picks` writes `discovery_match_suggestions` rows and nothing else.
`DraftPick.player_id` is written by `link_discovery_pick` alone, on an explicit admin
action. A 1.0 score is not authority — `players.name` is FPL's short web_name, two
people share a name, and a wrong link is indistinguishable from a right one
afterwards while quietly handing a manager another manager's keeper.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DiscoveryMatchSuggestion,
    DraftPick,
    Gameweek,
    League,
    Manager,
    Player,
    PlayerSeason,
    Standing,
)
from rules import RuleViolation

SEASON = 2026
_FPL = [0]


@pytest.fixture(autouse=True)
def _reset_ids():
    _FPL[0] = 900
    yield


def _seed(session, *, season=SEASON):
    lg = League(fpl_league_id=str(season), name=f"S{season}", season_year=season,
                is_current=True, sync_locked=False, phase="offseason")
    session.add(lg)
    session.flush()
    for n in range(1, 4):
        session.add(Gameweek(number=n, league_id=lg.id))
    mgrs = {}
    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i,
                             total=100 - i, points_for=1000 - i))
        mgrs[name] = m
    session.commit()
    return lg, mgrs


def _player(session, lg, web_name, full_name=None, pos="FWD", team="NEW"):
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=web_name, full_name=full_name, code=fid * 7, fpl_id=fid,
               position=pos, current_team=team, price=50, status="a")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fid,
                             name=web_name, position=pos, current_team=team))
    session.commit()
    return p


def _pick(session, lg, mgr, label, *, pick_number=1, season=SEASON):
    dp = DraftPick(league_id=lg.id, season_year=season, draft_type="discovery",
                   round=1, pick_number=pick_number, manager_id=mgr.id,
                   player_id=None, player_label=label, source="discovery")
    session.add(dp)
    session.commit()
    return dp


def _suggestions(session, pick):
    return (
        session.query(DiscoveryMatchSuggestion)
        .filter_by(draft_pick_id=pick.id)
        .all()
    )


# ---- normalisation: the accent traps ----------------------------------------

@pytest.mark.parametrize("typed,web,full", [
    ("Odegaard", "Ødegaard", "Martin Ødegaard"),
    ("Norgaard", "Nørgaard", "Christian Nørgaard"),
    ("F.Kadioglu", "F.Kadıoğlu", "Ferdi Kadıoğlu"),
    ("Hjerto-Dahl", "Hjertø-Dahl", "Isak Hjertø-Dahl"),
])
def test_accented_names_still_match_what_a_manager_typed(typed, web, full, test_session):
    """ø and ı have NO NFKD decomposition, so a bare ascii-ignore pass DELETES them
    and 'Ødegaard' becomes 'degaard'. Same trap scripts/import_projections.py's
    _TRANSLIT exists for; this matcher keeps its own copy of the rule."""
    lg, mgrs = _seed(test_session)
    p = _player(test_session, lg, web, full)
    pick = _pick(test_session, lg, mgrs["A"], typed)

    services.match_discovery_picks(test_session)
    rows = _suggestions(test_session, pick)
    assert [r.player_id for r in rows] == [p.id]
    # `exact`, not merely "found". Without the translation table ø is DELETED rather
    # than folded to o, and 'Ødegaard' -> 'degaard' still scrapes past the 0.85 fuzzy
    # threshold — so asserting only that he was suggested would pass on broken
    # normalisation. The tier is the part that proves translit ran.
    assert rows[0].method == "exact", "accent folding fell through to a fuzzy match"
    assert rows[0].score == 1.0


def test_normalisation_lowercases_before_transliterating():
    """The translation table has lowercase keys only, so an uppercase Ø would slip
    past it and then be deleted by ascii-ignore."""
    assert services._match_norm("ØDEGAARD") == services._match_norm("odegaard")


# ---- the match tiers ---------------------------------------------------------

def test_an_exact_full_name_match_scores_one(test_session):
    lg, mgrs = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")

    services.match_discovery_picks(test_session)
    row = _suggestions(test_session, pick)[0]
    assert (row.player_id, row.score, row.method, row.status) == (
        p.id, 1.0, "exact", "pending")


def test_a_web_name_only_pool_still_matches_a_typed_full_name(test_session):
    """The case that motivated players.full_name: before it, the pool held only
    "Woltemade" and a manager's "Nick Woltemade" matched nothing exactly. The token
    subset carries it either way, so this keeps working on rows sync hasn't
    refreshed yet."""
    lg, mgrs = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", None)
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")

    services.match_discovery_picks(test_session)
    row = _suggestions(test_session, pick)[0]
    assert row.player_id == p.id
    assert row.method == "strong"


def test_a_near_miss_is_suggested_with_a_sub_one_score(test_session):
    """A typo shouldn't cost the match — but it must be visibly less certain."""
    lg, mgrs = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Wolterade")

    services.match_discovery_picks(test_session)
    row = _suggestions(test_session, pick)[0]
    assert row.player_id == p.id
    assert row.method == "close"
    assert 0.85 <= row.score < 1.0


def test_an_unrelated_player_is_not_suggested(test_session):
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Haaland", "Erling Haaland")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")

    services.match_discovery_picks(test_session)
    assert _suggestions(test_session, pick) == []


def test_several_candidates_are_all_offered(test_session):
    """Two plausible people is exactly when a human must choose — so both are
    proposed rather than the matcher picking."""
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Nelson", "Reiss Nelson")
    _player(test_session, lg, "Nelson", "Ben Nelson")
    pick = _pick(test_session, lg, mgrs["A"], "Nelson")

    services.match_discovery_picks(test_session)
    assert len(_suggestions(test_session, pick)) == 2


# ---- the hard rule: never links ---------------------------------------------

def test_the_matcher_never_links_even_on_a_perfect_score(test_session):
    """The load-bearing guarantee of the whole feature."""
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")

    services.match_discovery_picks(test_session)
    test_session.refresh(pick)
    assert pick.player_id is None, "a suggestion must never become a link"
    assert pick.player_label == "Nick Woltemade"


def test_a_linked_pick_is_not_matched_again(test_session):
    lg, mgrs = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.link_discovery_pick(test_session, lg, season_year=SEASON,
                                 pick_number=1, player_fpl_id=p.fpl_id)

    out = services.match_discovery_picks(test_session)
    assert out["picks"] == 0
    assert _suggestions(test_session, pick) == []


# ---- idempotency across daily runs ------------------------------------------

def test_running_twice_does_not_duplicate(test_session):
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")

    services.match_discovery_picks(test_session)
    services.match_discovery_picks(test_session)
    assert len(_suggestions(test_session, pick)) == 1


def test_a_rejected_pair_is_never_raised_again(test_session):
    """The reason rejected rows are kept rather than deleted: a nightly job that
    re-proposed every dismissal would make the dashboard unusable."""
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.match_discovery_picks(test_session)
    row = _suggestions(test_session, pick)[0]
    services.reject_discovery_suggestion(test_session, lg, str(row.id))

    services.match_discovery_picks(test_session)
    rows = _suggestions(test_session, pick)
    assert len(rows) == 1
    assert rows[0].status == "rejected", "a dismissal must survive the next run"


def test_a_rejected_pick_stays_off_the_dashboard_but_the_pick_remains(test_session):
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.match_discovery_picks(test_session)
    services.reject_discovery_suggestion(
        test_session, lg, str(_suggestions(test_session, pick)[0].id))

    board = services.unlinked_discovery_picks(test_session, lg)
    assert len(board) == 1, "the pick still needs linking"
    assert board[0]["suggestions"] == [], "but the rejected candidate is gone"


# ---- confirm ----------------------------------------------------------------

def test_confirming_links_the_pick_and_records_it(test_session):
    from models import AuditLog

    lg, mgrs = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.match_discovery_picks(test_session)
    row = _suggestions(test_session, pick)[0]

    services.confirm_discovery_suggestion(test_session, lg, str(row.id))
    test_session.refresh(pick)
    test_session.refresh(row)
    assert pick.player_id == p.id
    assert pick.player_label == "Nick Woltemade", "the as-entered name is kept"
    assert row.status == "confirmed"
    assert test_session.query(AuditLog).filter_by(
        action="discovery.suggestion.confirm").count() == 1


def test_a_confirmed_pick_shows_the_real_player_on_the_board(test_session):
    """End to end: the point of linking is that the board and the keeper derivation
    can finally see who he is."""
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.match_discovery_picks(test_session)
    services.confirm_discovery_suggestion(
        test_session, lg, str(_suggestions(test_session, pick)[0].id))

    assert services.unlinked_discovery_picks(test_session, lg) == []
    board = services.get_discovery_board(test_session, lg, SEASON)
    assert board[0]["player"] == "Nick Woltemade"


def test_confirming_a_clashing_match_fails_and_leaves_it_pending(test_session):
    """link_discovery_pick's rules run FIRST, so a refused link doesn't record a
    decision that never happened."""
    lg, mgrs = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", "Nick Woltemade")
    first = _pick(test_session, lg, mgrs["A"], "Nick Woltemade", pick_number=1)
    second = _pick(test_session, lg, mgrs["B"], "N. Woltemade", pick_number=2)
    services.match_discovery_picks(test_session)
    services.link_discovery_pick(test_session, lg, season_year=SEASON,
                                 pick_number=1, player_fpl_id=p.fpl_id)

    row = _suggestions(test_session, second)[0]
    with pytest.raises(RuleViolation, match="already linked"):
        services.confirm_discovery_suggestion(test_session, lg, str(row.id))
    test_session.rollback()
    assert test_session.get(DiscoveryMatchSuggestion, row.id).status == "pending"
    assert first.player_id == p.id


def test_confirming_an_unknown_suggestion_is_refused(test_session):
    import uuid as _uuid

    lg, _mgrs = _seed(test_session)
    with pytest.raises(RuleViolation, match="suggestion not found"):
        services.confirm_discovery_suggestion(test_session, lg, str(_uuid.uuid4()))


def test_deleting_a_pick_takes_its_suggestions_with_it(test_session):
    """The one cascade in the schema. Without it delete_draft_pick — written long
    before this table — would start failing on a foreign key."""
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.match_discovery_picks(test_session)
    assert _suggestions(test_session, pick)

    services.delete_draft_pick(test_session, lg, str(pick.id))
    assert test_session.query(DiscoveryMatchSuggestion).count() == 0


# ---- cross-row + health ------------------------------------------------------

def test_a_pick_on_an_older_league_row_is_still_matched(test_session):
    """Picks predate rollovers; the matcher has no league filter for that reason."""
    old, old_mgrs = _seed(test_session, season=SEASON - 1)
    old.is_current, old.sync_locked = False, True
    lg, _mgrs = _seed(test_session, season=SEASON)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, old, old_mgrs["A"], "Nick Woltemade",
                 season=SEASON - 1)

    services.match_discovery_picks(test_session)
    assert len(_suggestions(test_session, pick)) == 1


def test_data_health_reports_unlinked_picks_and_pending_review(test_session):
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    pick = _pick(test_session, lg, mgrs["A"], "Nick Woltemade")

    def line():
        return next(c for c in services.data_health(test_session, lg)
                    if c["check"] == "discovery picks linked to players")

    assert line()["ok"] is True, "unlinked with no candidates is nothing to do"
    assert "1 unlinked" in line()["detail"]

    services.match_discovery_picks(test_session)
    assert line()["ok"] is False, "a candidate is waiting on a human"
    assert "1 suggestion(s) awaiting review" in line()["detail"]

    services.confirm_discovery_suggestion(
        test_session, lg, str(_suggestions(test_session, pick)[0].id))
    assert line()["ok"] is True
    assert line()["detail"] == "ok"


def test_corrections_page_exposes_the_dashboard(test_session):
    lg, mgrs = _seed(test_session)
    _player(test_session, lg, "Woltemade", "Nick Woltemade")
    _pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.match_discovery_picks(test_session)

    data = services.corrections_data(test_session, lg)
    assert len(data["unlinked_discovery"]) == 1
    row = data["unlinked_discovery"][0]
    assert row["label"] == "Nick Woltemade"
    assert row["owner"] == "A"
    assert row["suggestions"][0]["player"] == "Woltemade"
    assert row["suggestions"][0]["score"] == 1.0
