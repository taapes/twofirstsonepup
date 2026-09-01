"""Unreadable main-draft picks: the health check and the fix.

A main-draft pick recorded as free text that never resolves to a player costs its
manager `trusted` status in `_drafted_this_season`, so his ENTIRE squad falls back to the
"on the GW1 roster" proxy — which over-grants a draft-length keeper clock to anyone
signed in preseason free agency. Real cost, not bookkeeping.

Added 2026-08-31 alongside narrowing the keeper-seed check, which used to report 105
failures on production (76 drafted this season, 29 waiver pickups — none needing a seed)
and buried this one-line signal underneath.
"""

import pytest

from auth import hash_password
from models import DraftPick, Gameweek, League, Manager, Player, PlayerSeason, Roster
from rules import RuleViolation

import services

SEASON = 2026


@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _player(session, lg, fpl_id, name, *, full_name=None, position="MID"):
    p = Player(fpl_id=fpl_id, code=fpl_id * 1000, name=name, position=position,
               full_name=full_name, current_team="ARS")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id, name=name,
                             position=position, current_team="ARS"))
    return p


@pytest.fixture
def seeded(test_session):
    """One manager with a GW1 roster, plus one label pick that resolves and one that
    doesn't — mirroring the real 26/27 state."""
    lg = League(fpl_league_id="1", name="L", season_year=SEASON, is_current=True,
                sync_locked=False, phase="in_season")
    test_session.add(lg)
    test_session.flush()
    gw1 = Gameweek(number=1, league_id=lg.id)
    test_session.add(gw1)
    test_session.flush()
    m = Manager(league_id=lg.id, fpl_manager_id="1", name="T", display_name="Tucker",
                password_hash=hash_password("pw"))
    test_session.add(m)
    test_session.flush()

    # On the GW1 roster, so his label pick resolves by token subset.
    onroster = _player(test_session, lg, 10, "Dias", full_name="Ruben Santos Dias")
    test_session.add(Roster(manager_id=m.id, player_id=onroster.id, gameweek_id=gw1.id))
    # Exists in the pool but NOT on the roster — dropped before GW1, so unresolvable.
    dropped = _player(test_session, lg, 11, "Braithwaite", full_name="Martin Braithwaite")

    test_session.add(DraftPick(league_id=lg.id, season_year=SEASON, draft_type="main",
                               pick_number=1, round=1, manager_id=m.id,
                               player_label="Ruben Dias"))
    test_session.add(DraftPick(league_id=lg.id, season_year=SEASON, draft_type="main",
                               pick_number=2, round=1, manager_id=m.id,
                               player_label="Braithwaite"))
    test_session.commit()
    return lg, m, dropped


def test_only_the_genuinely_unreadable_pick_is_reported(test_session, seeded):
    """Resolution happens in memory and is never written back to player_id, so a
    `player_id IS NULL` filter alone over-reports: it would flag "Ruben Dias" too, and
    send the commissioner after work already done."""
    lg, _m, _dropped = seeded
    out = services.unresolved_draft_picks(test_session, lg)
    assert [p["label"] for p in out] == ["Braithwaite"]
    assert out[0]["pick_number"] == 2 and out[0]["manager"] == "Tucker"


def test_the_health_check_names_the_pick(test_session, seeded):
    lg, _m, _dropped = seeded
    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    row = checks["draft picks resolved to players"]
    assert row["ok"] is False
    assert "Braithwaite" in row["detail"] and "#2" in row["detail"]


def test_linking_clears_the_check(test_session, seeded):
    """The point of the affordance: the check must be clearable. Before this existed,
    link_discovery_pick was discovery-only and there was no way to act on the report."""
    lg, _m, dropped = seeded
    services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=2,
                             player_fpl_id=dropped.fpl_id)
    assert services.unresolved_draft_picks(test_session, lg) == []
    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["draft picks resolved to players"]["ok"] is True


def test_linking_restores_the_managers_draft_trust(test_session, seeded):
    """WHY it matters. An unreadable pick removes its manager from `trusted`, and the
    honest reason is that the pick we can't read might be the very player being asked
    about — so the derivation keeps the weaker proxy for his whole squad."""
    lg, m, dropped = seeded
    gw = services.current_gameweek(test_session, lg) or 1
    presence, _il = services._roster_presence_and_il_coverage(test_session, lg, gw)
    _d, trusted = services._drafted_this_season(test_session, lg, presence)
    assert m.id not in trusted

    services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=2,
                             player_fpl_id=dropped.fpl_id)
    presence, _il = services._roster_presence_and_il_coverage(test_session, lg, gw)
    _d, trusted = services._drafted_this_season(test_session, lg, presence)
    assert m.id in trusted


def test_a_pick_already_linked_is_refused(test_session, seeded):
    lg, _m, dropped = seeded
    services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=2,
                             player_fpl_id=dropped.fpl_id)
    other = _player(test_session, lg, 12, "Other")
    test_session.commit()
    with pytest.raises(RuleViolation, match="already linked to"):
        services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=2,
                                 player_fpl_id=other.fpl_id)


def test_relinking_the_same_player_is_idempotent(test_session, seeded):
    lg, _m, dropped = seeded
    services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=2,
                             player_fpl_id=dropped.fpl_id)
    out = services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=2,
                                   player_fpl_id=dropped.fpl_id)
    assert out["already"] is True


def test_one_player_cannot_fill_two_picks(test_session, seeded):
    """Otherwise a mislink quietly hands one manager two clocks on the same human."""
    lg, m, dropped = seeded
    services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=2,
                             player_fpl_id=dropped.fpl_id)
    test_session.add(DraftPick(league_id=lg.id, season_year=SEASON, draft_type="main",
                               pick_number=3, round=1, manager_id=m.id, player_label="dup"))
    test_session.commit()
    with pytest.raises(RuleViolation, match="already linked to .* pick"):
        services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=3,
                                 player_fpl_id=dropped.fpl_id)


def test_an_unknown_pick_is_refused(test_session, seeded):
    lg, _m, dropped = seeded
    with pytest.raises(RuleViolation, match="no 2026 main-draft pick"):
        services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=999,
                                 player_fpl_id=dropped.fpl_id)


def test_the_corrections_page_offers_the_link_form(client, test_session, seeded,
                                                   monkeypatch):
    lg, _m, _d = seeded
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    assert client.post("/admin/login", data={"password": "pw"}).status_code == 303
    body = client.get("/admin/corrections").text
    assert "Unreadable draft picks" in body
    # Scope to the new section: the page legitimately shows player_label elsewhere
    # (the picks table renders "as entered" for any pick whose label differs from the
    # linked player), so asserting over the whole body tests the wrong thing.
    start = body.index("Unreadable draft picks")
    section = body[start:body.index("<h2", start + 1)]
    assert "/admin/corrections/draft/link" in section
    assert "Braithwaite" in section
    assert "Ruben Dias" not in section, "the resolvable pick is not listed"


def test_a_manager_cannot_link_a_pick(client, test_session, seeded):
    lg, m, dropped = seeded
    assert client.post("/login", data={"manager_id": "1",
                                       "password": "pw"}).status_code == 303
    r = client.post("/admin/corrections/draft/link",
                    data={"season_year": str(SEASON), "pick_number": "2",
                          "player_name": "Braithwaite · ARS"})
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
    test_session.expire_all()
    assert services.unresolved_draft_picks(test_session, lg) != []


# ---- the narrowed keeper-seed check ------------------------------------------
def test_a_trusted_managers_unseeded_players_are_not_flagged(test_session):
    """The narrowing, pinned.

    Asking every rostered player for a seed is unsatisfiable once a season has a draft
    board: on production it reported 105 failures, of which 76 were drafted this season
    and 29 were waiver pickups — all deriving their clock from real data, none needing a
    seed. A permanently-red check trains the reader to ignore the health page.

    A seed is only required where the derivation has nothing to work from, which is
    exactly `_drafted_this_season`'s `trusted` set.
    """
    lg = League(fpl_league_id="2", name="Clean", season_year=SEASON, is_current=True,
                sync_locked=False, phase="in_season")
    test_session.add(lg)
    test_session.flush()
    gw1 = Gameweek(number=1, league_id=lg.id)
    test_session.add(gw1)
    test_session.flush()
    m = Manager(league_id=lg.id, fpl_manager_id="5", name="T", display_name="Ann")
    test_session.add(m)
    test_session.flush()

    # Rostered, no keeper seed — and drafted this season, so his clock derives.
    p = _player(test_session, lg, 20, "Drafted")
    test_session.add(Roster(manager_id=m.id, player_id=p.id, gameweek_id=gw1.id))
    test_session.add(DraftPick(league_id=lg.id, season_year=SEASON, draft_type="main",
                               pick_number=1, round=1, manager_id=m.id,
                               player_id=p.id))
    test_session.commit()

    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["keeper clocks derivable without a seed"]["ok"] is True, \
        checks["keeper clocks derivable without a seed"]["detail"]


def test_an_untrusted_managers_unseeded_players_ARE_flagged(test_session, seeded):
    """The other side: with an unreadable pick, Tucker's squad genuinely can't be
    derived, and the check says so — then clears when the pick is linked."""
    lg, _m, dropped = seeded
    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["keeper clocks derivable without a seed"]["ok"] is False

    services.link_draft_pick(test_session, lg, season_year=SEASON, pick_number=2,
                             player_fpl_id=dropped.fpl_id)
    checks = {c["check"]: c for c in services.data_health(test_session, lg)}
    assert checks["keeper clocks derivable without a seed"]["ok"] is True, \
        "linking the pick restores trust, so the seed check clears too"
