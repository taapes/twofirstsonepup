"""Keeper selections are private until they lock.

Everyone submits keepers during the same offseason window, so publishing them on
submit lets a manager read the league's picks and then choose their own. A selection
is private to the manager who made it (and the commissioner) for exactly as long as
it can still be changed, and public the moment it can't.

The redaction lives in `services`, not in templates, because `/v1` is exempt from the
login gate (main.py) and therefore has no viewer to branch on. Consequently the
DEFAULTS are the security boundary: a caller that passes no viewer must disclose
nothing, so a future caller leaks nothing by forgetting to think about it.

The pure cases need no database; the rest use TEST_DATABASE_URL (see conftest).
"""

import inspect

import pytest

import services
from models import (
    DraftPick,
    DraftQueue,
    Gameweek,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerSeason,
    Roster,
    Standing,
)
from rules import PHASES, keepers_revealed, phase_features


# ---- the predicate --------------------------------------------------------
def test_selections_are_private_only_while_they_are_editable():
    assert keepers_revealed("offseason", False) is False, "the private window"
    assert keepers_revealed("offseason", True) is True, "commissioner locked early"
    assert keepers_revealed("draft", False) is True, "the draft started"
    assert keepers_revealed("preseason", False) is True
    assert keepers_revealed("in_season", False) is True


@pytest.mark.parametrize("macro", PHASES)
def test_reveal_is_exactly_the_negation_of_editability(macro):
    """Defined off keepers_editable so the two can't drift when a phase is added."""
    assert keepers_revealed(macro, False) is not phase_features(macro)["keepers_editable"]


@pytest.mark.parametrize("macro", PHASES)
def test_keepers_editable_does_not_depend_on_the_sub_state_flags(macro):
    """keepers_revealed calls phase_features with no kwargs. That shortcut is only
    safe while keepers_editable is a per-phase constant — pin it."""
    assert (
        phase_features(macro)["keepers_editable"]
        == phase_features(macro, trades_off=True, cups_available=True,
                          discovery_open=True, gw_logic=True)["keepers_editable"]
    )


# ---- fixtures -------------------------------------------------------------
UPCOMING = 2026


def _seed(session, phase="offseason", locked=False):
    """A league in the offseason with two managers, each holding one player, each
    having submitted that player as a keeper for the upcoming season."""
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase=phase, keepers_locked=locked)
    session.add(lg)
    session.flush()
    gw = Gameweek(number=1, league_id=lg.id)
    session.add(gw)
    session.flush()

    out = {}
    for i, (mgr, player) in enumerate([("A", "Haaland"), ("B", "Palmer")], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=mgr, display_name=mgr)
        p = Player(name=player, code=i * 7, fpl_id=i, position="MID", current_team="ARS")
        session.add_all([m, p])
        session.flush()
        session.add_all([
            Roster(manager_id=m.id, gameweek_id=gw.id, player_id=p.id),
            Standing(league_id=lg.id, manager_id=m.id, rank=i, total=10 - i,
                     points_for=100),
            KeeperSelection(league_id=lg.id, manager_id=m.id, player_id=p.id,
                            season_year=UPCOMING),
            # get_keeper_selections resolves names through the season snapshot, so
            # without this it returns [] for reasons that have nothing to do with
            # privacy — and every redaction assertion below would pass vacuously.
            PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=p.fpl_id,
                         name=p.name, position=p.position,
                         current_team=p.current_team),
        ])
        out[mgr] = (m, p)
    session.commit()
    return lg, out


def _kept(rows, manager):
    """{player name: kept flag} for one manager out of a get_keepers payload."""
    team = next(t for t in rows if t["manager"] == manager)
    return {p["player"]: p["kept"] for p in team["players"]}


# ---- the defaults ARE the security boundary -------------------------------
def test_derive_hides_every_selection_when_asked_for_no_viewer(test_session):
    lg, mgrs = _seed(test_session)
    status = services._derive_keeper_status(test_session, lg)
    assert all(v["kept"] is False for m in status.values() for v in m.values())

    # ...and the same call CAN show them, so the assertion above isn't vacuous
    shown = services._derive_keeper_status(test_session, lg, kept_all=True)
    assert all(v["kept"] is True for m in shown.values() for v in m.values())


def test_get_keepers_hides_everything_without_a_viewer(test_session):
    lg, _ = _seed(test_session)
    rows = services.get_keepers(test_session, lg)
    assert _kept(rows, "A") == {"Haaland": False}
    assert _kept(rows, "B") == {"Palmer": False}


def test_keeper_selections_are_empty_without_a_viewer(test_session):
    lg, _ = _seed(test_session)
    assert services.get_keeper_selections(test_session, lg, UPCOMING) == []
    # and the same fixture DOES yield rows once revealed, so [] above means redacted
    # rather than "the fixture never had any"
    lg.keepers_locked = True
    assert len(services.get_keeper_selections(test_session, lg, UPCOMING)) == 2


def test_search_does_not_mark_a_hidden_keeper_as_taken(test_session):
    """Not even anonymously: a bare 'taken' pill still tells you the player is off
    the board, which is the half that matters."""
    lg, _ = _seed(test_session)
    rows = {r["name"]: r for r in services.search_players(
        test_session, lg, available_year=UPCOMING, include_taken=True)}
    assert rows["Palmer"]["taken"] is False
    assert rows["Palmer"]["taken_by"] is None


def test_the_redacting_defaults_are_declared_not_incidental(test_session):
    """Blunt, but it kills a default flip even if the body is rewritten."""
    for fn in (services._derive_keeper_status, services.search_players):
        params = inspect.signature(fn).parameters
        assert params["kept_all"].default is False
        assert params["kept_for"].default is None
    for fn in (services.get_keepers, services.get_keeper_selections):
        params = inspect.signature(fn).parameters
        assert params["viewer_fpl"].default is None
        assert params["viewer_is_admin"].default is False


# ---- viewer scoping -------------------------------------------------------
def test_a_manager_sees_their_own_selection_and_nobody_elses(test_session):
    lg, mgrs = _seed(test_session)
    rows = services.get_keepers(test_session, lg, viewer_fpl="1")
    assert _kept(rows, "A") == {"Haaland": True}
    assert _kept(rows, "B") == {"Palmer": False}


def test_the_commissioner_sees_everyones(test_session):
    lg, _ = _seed(test_session)
    rows = services.get_keepers(test_session, lg, viewer_is_admin=True)
    assert _kept(rows, "A") == {"Haaland": True}
    assert _kept(rows, "B") == {"Palmer": True}


def test_an_int_viewer_id_matches_the_string_column(test_session):
    """fpl_manager_id is a String and the session value may not be. A mismatch fails
    closed, which is safe but reads like a bug report."""
    lg, _ = _seed(test_session)
    rows = services.get_keepers(test_session, lg, viewer_fpl=1)
    assert _kept(rows, "A") == {"Haaland": True}


def test_a_manager_sees_only_their_own_submitted_selection(test_session):
    lg, _ = _seed(test_session)
    out = services.get_keeper_selections(test_session, lg, UPCOMING, viewer_fpl="1")
    assert [g["manager"] for g in out] == ["A"]


def test_search_marks_your_own_keeper_taken(test_session):
    lg, mgrs = _seed(test_session)
    rows = {r["name"]: r for r in services.search_players(
        test_session, lg, available_year=UPCOMING, include_taken=True,
        kept_for={mgrs["A"][0].id})}
    assert rows["Haaland"]["taken_by"] == "kept: A"
    assert rows["Palmer"]["taken"] is False


# ---- the reveal -----------------------------------------------------------
def test_locking_keepers_reveals_them_to_everyone(test_session):
    lg, _ = _seed(test_session, locked=True)
    rows = services.get_keepers(test_session, lg)   # no viewer at all
    assert _kept(rows, "B") == {"Palmer": True}
    assert len(services.get_keeper_selections(test_session, lg, UPCOMING)) == 2


def test_starting_the_draft_reveals_them_without_the_lock(test_session):
    lg, _ = _seed(test_session, phase="draft", locked=False)
    assert _kept(services.get_keepers(test_session, lg), "B") == {"Palmer": True}


def test_enter_draft_phase_reveals_them(test_session):
    """The transition the league actually uses — it sets both legs."""
    lg, _ = _seed(test_session)
    assert services.keepers_revealed(lg) is False
    services.enter_draft_phase(test_session, lg)
    assert services.keepers_revealed(lg) is True
    assert _kept(services.get_keepers(test_session, lg), "B") == {"Palmer": True}


def test_search_labels_keepers_once_revealed(test_session):
    lg, _ = _seed(test_session, locked=True)
    rows = {r["name"]: r for r in services.search_players(
        test_session, lg, available_year=UPCOMING, include_taken=True,
        kept_all=services.keepers_revealed(lg))}
    assert rows["Palmer"]["taken_by"] == "kept: B"


# ---- demo mode inverts the predicate if you let it ------------------------
def test_demo_reveals_rather_than_hiding_forever(test_session, monkeypatch):
    """phase_context forces every feature flag True in demo, so keepers_editable is
    True there — the raw predicate would hide keepers PERMANENTLY on the demo site."""
    lg, _ = _seed(test_session)
    assert services.keepers_revealed(lg) is False
    monkeypatch.setenv("APP_ENV", "demo")
    assert services.keepers_revealed(lg) is True
    assert _kept(services.get_keepers(test_session, lg), "B") == {"Palmer": True}


@pytest.mark.parametrize("env", ["prod", "demo"])
@pytest.mark.parametrize("locked", [False, True])
def test_the_template_flag_agrees_with_the_redaction(test_session, monkeypatch, env,
                                                     locked):
    """The page and the data must make the same call, in every combination. That means
    keepers_public cannot be one of phase_features' flags: the demo blanket rewrites
    those wholesale, by a different route than keepers_revealed.

    The locked case is what makes this test able to fail — unlocked-and-not-demo, both
    answers are False for unrelated reasons and any wiring looks correct.
    """
    lg, _ = _seed(test_session, locked=locked)
    monkeypatch.setenv("APP_ENV", env)
    ctx = services.phase_context(test_session, lg)
    assert ctx["keepers_public"] is services.keepers_revealed(lg)


# ---- redaction must not break anything that needs the truth ---------------
def test_the_commissioner_override_page_still_shows_what_is_kept(test_session):
    lg, _ = _seed(test_session)
    ctx = services.keeper_overrides_context(test_session, lg)
    kept = {p["player"]: p["kept"] for m in ctx["managers"] for p in m["players"]}
    assert kept == {"Haaland": True, "Palmer": True}


def test_submitting_keepers_still_enforces_the_cap(test_session):
    """The caps read eligibility, not `kept` — but prove it under the redacted
    default rather than assuming."""
    lg, mgrs = _seed(test_session)
    with pytest.raises(services.RuleViolation):
        services.submit_keepers(
            test_session, lg, fpl_manager_id="1",
            keeper_fpl_ids=[1, 2, 3, 4, 5, 6, 7], season_year=UPCOMING,
        )


def test_search_still_reports_drafted_players(test_session):
    """Draft picks are public; over-redacting would blank them too."""
    lg, mgrs = _seed(test_session, locked=True)
    # a player nobody kept — record_pick now refuses to draft someone else's keeper
    free = Player(name="Saka", code=99, fpl_id=9, position="MID", current_team="ARS")
    test_session.add(free)
    test_session.commit()
    services.record_pick(
        test_session, lg, season_year=UPCOMING, pick_number=1,
        owner_fpl="1", player_fpl_id=9,
    )
    rows = {r["name"]: r for r in services.search_players(
        test_session, lg, available_year=UPCOMING, include_taken=True)}
    assert rows["Saka"]["taken_by"] == "drafted: A"


def test_the_autodraft_queue_never_hands_out_another_managers_keeper(test_session):
    """approve_queued_pick uses search_players as a CORRECTNESS filter, not a
    disclosure. Under the redacted default it would draft B's keeper for A, and
    record_pick has no availability guard to catch it."""
    lg, mgrs = _seed(test_session, phase="draft")
    # Reverse standings put B on the clock at pick 1 — queue for THEM, or the run
    # fails with "no queued picks" and proves nothing about keepers.
    on_clock = services.next_open_pick(services.get_draft_board(test_session, lg, UPCOMING))
    assert on_clock["owner"] == "B", on_clock
    test_session.add(DraftQueue(
        league_id=lg.id, manager_id=mgrs["B"][0].id, season_year=UPCOMING,
        draft_type="main", player_id=mgrs["A"][1].id, rank=1,   # A's keeper
    ))
    test_session.commit()

    with pytest.raises(services.RuleViolation, match="unavailable|no queued"):
        services.approve_queued_pick(test_session, lg, season_year=UPCOMING)
    assert not test_session.query(DraftPick).filter_by(league_id=lg.id).count(), \
        "the queue drafted a player another manager had already kept"


# ---- routes: what a service-only fix leaves behind ------------------------
@pytest.fixture
def client(test_session):
    """A TestClient sharing the test database (conftest patches db.SessionLocal,
    which get_db resolves at call time) — never the configured one."""
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _login(client, session, manager, password="pw"):
    from auth import hash_password

    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text
    return client


def test_you_cannot_read_another_managers_keeper_options(client, test_session):
    """The sharpest hole this closes: keeper_candidates builds `selected` and the
    off-roster discovery pick from its OWN query, so the service-layer redaction
    never reaches this route — the ownership check is the entire protection."""
    lg, mgrs = _seed(test_session)
    _login(client, test_session, mgrs["A"][0])

    mine = client.get("/keepers/candidates?fpl_manager_id=1")
    assert mine.status_code == 200
    assert b"Haaland" in mine.content

    theirs = client.get("/keepers/candidates?fpl_manager_id=2")
    assert theirs.status_code == 403
    assert b"Palmer" not in theirs.content


def test_the_commissioner_can_read_anyones_keeper_options(client, test_session,
                                                          monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "privacy-test-pw")
    lg, mgrs = _seed(test_session)
    assert client.post("/admin/login", data={"password": "privacy-test-pw"}).status_code == 303
    r = client.get("/keepers/candidates?fpl_manager_id=2")
    assert r.status_code == 200 and b"Palmer" in r.content


def test_the_teams_page_shows_your_lock_and_not_theirs(client, test_session):
    """Catches the route forgetting to splat _viewer(request) — the failure a
    service-layer-only change leaves behind."""
    lg, mgrs = _seed(test_session)
    _login(client, test_session, mgrs["A"][0])

    body = client.get("/teams").content.decode()
    a_row = body.split("Haaland")[1][:120]
    b_row = body.split("Palmer")[1][:120]
    assert "keeper" in a_row, "your own pick should still be marked"
    assert "keeper" not in b_row, "another manager's pick leaked on /teams"

    lg.keepers_locked = True
    test_session.commit()
    revealed = client.get("/teams").content.decode()
    assert "keeper" in revealed.split("Palmer")[1][:120]


def test_the_draft_search_does_not_name_a_hidden_keeper(client, test_session):
    """This page is reachable during the offseason, so the 'kept: X' label would
    otherwise publish the whole league's picks to anyone who searched."""
    lg, mgrs = _seed(test_session)
    _login(client, test_session, mgrs["A"][0])

    hidden = client.get(f"/draft/{UPCOMING}/search?q=Palmer").content.decode()
    assert "kept:" not in hidden
    assert "Palmer" in hidden, "the player should look draftable, not vanish"

    lg.keepers_locked = True
    test_session.commit()
    shown = client.get(f"/draft/{UPCOMING}/search?q=Palmer").content.decode()
    assert "kept: B" in shown


def test_the_unauthenticated_api_discloses_nothing_until_the_reveal(client,
                                                                   test_session):
    """/v1 is exempt from the login gate, so these have no viewer to scope to."""
    lg, _ = _seed(test_session)
    key = lg.fpl_league_id

    assert client.get(f"/v1/leagues/{key}/keeper-selections/{UPCOMING}").json() == []
    keepers = client.get(f"/v1/leagues/{key}/keepers").json()
    assert not any(p["kept"] for t in keepers for p in t["players"])
    pool = client.get(f"/v1/leagues/{key}/players?available_year={UPCOMING}").json()
    assert "Palmer" in {p["name"] for p in pool}, (
        "filtering kept players out lets anyone enumerate them by diffing"
    )

    lg.keepers_locked = True
    test_session.commit()
    assert len(client.get(f"/v1/leagues/{key}/keeper-selections/{UPCOMING}").json()) == 2
    after = client.get(f"/v1/leagues/{key}/players?available_year={UPCOMING}").json()
    assert "Palmer" not in {p["name"] for p in after}
