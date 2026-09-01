"""The AI gameweek review: generation, the spend guards, and the Discord gate.

THE INVARIANT THIS FILE DEFENDS: nothing generated reaches Discord without a human
click. Generation is automatic; posting is not. A model cannot tell when a joke lands
badly on a particular person in a particular week, and a chat message cannot be unsent.
That property is asserted twice — the post-sync hook has no sending path, and `status`
only becomes 'posted' via the admin route.

No test touches the network: `generate=` is injected, the same seam
tests/test_discord_outbound.py uses for `send=`. `call_model` is the only function that
would reach out, and it is never called here.
"""

import ast
import datetime as dt
import pathlib

import pytest

import ai_content
import services
from auth import hash_password
from models import (
    AiGeneratedContent,
    Fixture,
    Gameweek,
    GameweekPoints,
    League,
    Manager,
    ManagerNote,
    Match,
    Player,
    PlayerSeason,
    Standing,
)
from rules import AI_MIN_REGENERATE_SECONDS, MAX_AI_CALLS_PER_GW, RuleViolation

GW = 2


class FakeGenerator:
    """Records prompts and returns canned output — or fails on demand."""

    def __init__(self, *, fail=False, refuse=False, headline="Chaos", body="One.\n\nTwo."):
        self.prompts = []
        self.fail, self.refuse = fail, refuse
        self.headline, self.body = headline, body

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.refuse:
            raise RuleViolation("the model declined to write this one (cyber)")
        if self.fail:
            return None
        return {"headline": self.headline, "body": self.body, "model": "fake-model"}


@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


def _seed(test_session, *, finished=2, in_progress=0):
    """A league whose GW2 fixtures are all finished, with two managers and a match."""
    lg = League(fpl_league_id="1", name="L", season_year=2026, is_current=True,
                sync_locked=False, phase="in_season")
    test_session.add(lg)
    test_session.flush()
    for n in (1, GW):
        test_session.add(Gameweek(number=n, league_id=lg.id,
                                 start_date=dt.date.today() - dt.timedelta(days=8 - n)))
    test_session.flush()
    gw = test_session.query(Gameweek).filter_by(league_id=lg.id, number=GW).one()
    for i in range(finished):
        test_session.add(Fixture(league_id=lg.id, fpl_fixture_id=100 + i, event=GW,
                                 home_team=f"F{i}", away_team="ZZZ", finished=True))
    for i in range(in_progress):
        test_session.add(Fixture(league_id=lg.id, fpl_fixture_id=200 + i, event=GW,
                                 home_team=f"P{i}", away_team="ZZZ", finished=False,
                                 started=True))
    john = Manager(league_id=lg.id, fpl_manager_id="1", name="T1", display_name="John",
                   password_hash=hash_password("pw"))
    ann = Manager(league_id=lg.id, fpl_manager_id="2", name="T2", display_name="Ann")
    test_session.add_all([john, ann])
    test_session.flush()
    p = Player(fpl_id=500, code=500000, name="Saka", position="MID", current_team="ARS")
    test_session.add(p)
    test_session.flush()
    test_session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=500,
                                  name="Saka", position="MID", current_team="ARS"))
    for m, pts in ((john, 63), (ann, 41)):
        test_session.add(GameweekPoints(
            manager_id=m.id, gameweek_id=gw.id, total_points=pts,
            player_points=[{"fpl_id": 500, "position": 1, "is_starting": True,
                            "minutes": 90, "points": pts}]))
    test_session.add(Match(league_id=lg.id, gameweek_id=gw.id,
                           home_manager_id=john.id, away_manager_id=ann.id))
    test_session.commit()
    return lg, john, ann


# ---- the feature is off unless configured ------------------------------------
def test_no_api_key_is_a_clean_no_op(test_session, monkeypatch):
    """A fresh checkout, the test suite and the demo sandbox all cost nothing."""
    monkeypatch.delenv(ai_content.API_KEY_ENV, raising=False)
    lg, _j, _a = _seed(test_session)
    assert ai_content.ensure_review(test_session, lg, GW) == {"skipped": "not configured"}
    assert test_session.query(AiGeneratedContent).count() == 0


def test_a_frozen_season_generates_nothing(test_session):
    lg, _j, _a = _seed(test_session)
    lg.sync_locked = True
    test_session.commit()
    assert ai_content.run_after_sync(test_session, lg) == {"skipped": "frozen"}


# ---- the gameweek-over gate --------------------------------------------------
def test_a_gameweek_still_in_play_generates_nothing(test_session):
    """Gated on real PL fixtures, not Match.finished — that is FPL's H2H scoring-lock
    and lags by hours. Same test the Discord summary uses, so the two can't disagree."""
    lg, _j, _a = _seed(test_session, finished=1, in_progress=1)
    gen = FakeGenerator()
    out = ai_content.ensure_review(test_session, lg, GW, generate=gen)
    assert out == {"skipped": "gameweek not finished"}
    assert gen.prompts == [], "no API call for an unfinished gameweek"


def test_a_gameweek_with_no_fixtures_generates_nothing(test_session):
    """total == 0 would satisfy finished == total. An unsynced gameweek is not a
    finished one — the same missing-data-isn't-emptiness guard as elsewhere."""
    lg, _j, _a = _seed(test_session, finished=0)
    gen = FakeGenerator()
    assert ai_content.ensure_review(test_session, lg, GW, generate=gen)["skipped"] == \
        "gameweek not finished"
    assert gen.prompts == []


# ---- generation and idempotency ----------------------------------------------
def test_a_finished_gameweek_generates_once(test_session):
    lg, _j, _a = _seed(test_session)
    gen = FakeGenerator()

    out = ai_content.ensure_review(test_session, lg, GW, generate=gen)
    assert out["generated"] is True and out["status"] == "ready"
    row = test_session.query(AiGeneratedContent).one()
    assert (row.headline, row.model, row.attempts) == ("Chaos", "fake-model", 1)
    assert row.content == "One.\n\nTwo."

    # The idempotency that makes the automatic path cost one call per gameweek.
    again = ai_content.ensure_review(test_session, lg, GW, generate=gen)
    assert again["skipped"] == "already generated"
    assert len(gen.prompts) == 1, "the second sync must not pay for a second call"


def test_a_failed_generation_is_recorded_and_does_not_raise(test_session):
    lg, _j, _a = _seed(test_session)
    out = ai_content.ensure_review(test_session, lg, GW,
                                   generate=FakeGenerator(fail=True))
    assert out["generated"] is False and out["status"] == "failed"
    row = test_session.query(AiGeneratedContent).one()
    assert row.content is None and row.error and row.attempts == 1


def test_a_refusal_is_recorded_with_its_reason(test_session):
    """Deliberately teasing copy about named people can trip a safety classifier. The
    reason is kept rather than swallowed: a refusal and a timeout want different
    responses from the commissioner."""
    lg, _j, _a = _seed(test_session)
    out = ai_content.ensure_review(test_session, lg, GW,
                                   generate=FakeGenerator(refuse=True))
    assert out["status"] == "failed"
    assert "declined" in test_session.query(AiGeneratedContent).one().error


def test_the_post_sync_hook_never_raises(test_session, monkeypatch):
    """A bug in our own prompt building must not fail the sync that carried it."""
    lg, _j, _a = _seed(test_session)
    monkeypatch.setattr(ai_content, "build_prompt",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setenv(ai_content.API_KEY_ENV, "fake")
    out = ai_content.run_after_sync(test_session, lg)
    assert "boom" in out.get("error", "")


# ---- the spend guards --------------------------------------------------------
def test_regenerate_updates_in_place(test_session):
    """No second row. A superseded review has no value — the deliberate divergence from
    discovery_match_suggestions' keep-every-candidate rule."""
    lg, _j, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator())
    row = test_session.query(AiGeneratedContent).one()
    row.generated_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    test_session.commit()

    ai_content.ensure_review(test_session, lg, GW, force=True,
                             generate=FakeGenerator(headline="Second", body="New."))
    assert test_session.query(AiGeneratedContent).count() == 1
    test_session.refresh(row)
    assert (row.headline, row.attempts) == ("Second", 2)


def test_regenerating_too_soon_is_refused(test_session):
    """A regenerate button is a thing people double-click."""
    lg, _j, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator())
    with pytest.raises(RuleViolation, match="just regenerated"):
        ai_content.ensure_review(test_session, lg, GW, force=True,
                                 generate=FakeGenerator())


def test_the_per_gameweek_cap_refuses(test_session):
    """Counts FAILURES too: a cap that only counted successes would be no cap at all
    against a persistently failing key, which is exactly when a runaway goes unnoticed
    because nothing is being produced to look at."""
    lg, _j, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator(fail=True))
    row = test_session.query(AiGeneratedContent).one()
    row.attempts = MAX_AI_CALLS_PER_GW
    row.generated_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    test_session.commit()

    with pytest.raises(RuleViolation, match="generation attempts"):
        ai_content.ensure_review(test_session, lg, GW, force=True,
                                 generate=FakeGenerator())


# ---- the prompt --------------------------------------------------------------
def test_the_prompt_carries_the_facts_and_not_the_arithmetic(test_session):
    """A content-shape assertion, not an assertion about model output. The deterministic
    `analysis` sentence is included so the model paraphrases a correct result rather
    than interpreting a scoreline."""
    lg, _j, _a = _seed(test_session)
    prompt = ai_content.build_prompt(test_session, lg, GW)
    assert "GAMEWEEK 2" in prompt
    assert "John 63 – 41 Ann" in prompt
    assert "result: John wins 63–41." in prompt
    assert "STANDINGS" in prompt
    assert "Saka (MID, ARS)" in prompt, "per-player rows, so it can find its own angles"


def _two_gameweeks_of_matches(test_session, lg, john, ann):
    """A GW1 fixture and match alongside the GW2 ones _seed already made."""
    gw1 = test_session.query(Gameweek).filter_by(league_id=lg.id, number=1).one()
    test_session.add(Fixture(league_id=lg.id, fpl_fixture_id=300, event=1,
                             home_team="A", away_team="B", finished=True))
    test_session.add(Match(league_id=lg.id, gameweek_id=gw1.id,
                           home_manager_id=ann.id, away_manager_id=john.id))
    test_session.commit()


def _standings(test_session, lg, john, ann, *, played):
    for i, m in enumerate((john, ann)):
        test_session.add(Standing(
            league_id=lg.id, manager_id=m.id, rank=i + 1, total=3 * played,
            points_for=100, points_against=90,
            matches_won=played, matches_drawn=0, matches_lost=0))
    test_session.commit()


def test_standings_that_predate_the_gameweek_are_labelled_stale(test_session):
    """FPL finalises its H2H table up to a day after the final whistle, so the stored
    standings can legitimately predate the gameweek being reviewed. Labelling them
    "after this gameweek" hands the model a false fact it is told to trust — the real
    GW2 shipped with a manager top of the table on 81 points-for having just scored 23.
    """
    lg, john, ann = _seed(test_session)
    _two_gameweeks_of_matches(test_session, lg, john, ann)
    _standings(test_session, lg, john, ann, played=1)

    prompt = ai_content.build_prompt(test_session, lg, GW)
    assert "STANDINGS — STALE. These do NOT include GW2." in prompt
    assert "covers through GW1" in prompt
    assert "STANDINGS after this gameweek" not in prompt
    # The restriction has to be an instruction, not a footnote.
    assert "do NOT say who leads the league" in prompt


def test_standings_that_include_the_gameweek_are_labelled_current(test_session):
    lg, john, ann = _seed(test_session)
    _two_gameweeks_of_matches(test_session, lg, john, ann)
    _standings(test_session, lg, john, ann, played=2)

    prompt = ai_content.build_prompt(test_session, lg, GW)
    assert "STANDINGS after this gameweek" in prompt
    assert "STALE" not in prompt


def test_coverage_counts_absorbed_results_not_synced_gameweeks(test_session):
    """`matches_won + drawn + lost` is the measure — it counts results the table has
    absorbed whatever the sync did, so a draw counts and a manager with no row doesn't
    drag the answer down."""
    lg, john, ann = _seed(test_session)
    _two_gameweeks_of_matches(test_session, lg, john, ann)
    test_session.add(Standing(league_id=lg.id, manager_id=john.id, rank=1, total=4,
                              points_for=100, points_against=90,
                              matches_won=1, matches_drawn=1, matches_lost=0))
    test_session.commit()

    st = services.get_standings(test_session, lg)
    cov = ai_content.standings_coverage(test_session, lg, GW, st)
    assert cov == {"played": 2, "expected": 2, "current": True}


def test_coverage_on_an_empty_table_does_not_crash(test_session):
    """Preseason, or a league whose first sync hasn't landed."""
    lg, john, ann = _seed(test_session)
    cov = ai_content.standings_coverage(test_session, lg, GW, [])
    assert cov["played"] == 0 and cov["current"] is False
    assert "STANDINGS — STALE" in ai_content.build_prompt(test_session, lg, GW)


def test_a_manager_note_reaches_the_prompt(test_session):
    lg, _j, _a = _seed(test_session)
    test_session.add(ManagerNote(person="John",
                                 note="the favourite and insufferable — never flatter him"))
    test_session.commit()
    prompt = ai_content.build_prompt(test_session, lg, GW)
    assert "WHAT YOU KNOW ABOUT THESE PEOPLE" in prompt
    assert "John: the favourite and insufferable" in prompt


def test_the_persona_states_the_no_arithmetic_rule(test_session):
    """The mitigation for handing over raw per-player rows. If this instruction is ever
    edited out, the model is free to compute a number in front of the people who played
    the match."""
    assert "do not" in ai_content.PERSONA.lower() or "Do NOT" in ai_content.PERSONA
    assert "calculate" in ai_content.PERSONA


def test_manager_notes_survive_a_rollover(test_session):
    """Keyed on the PERSON, not a managers.id FK — managers has one row per season, so
    an FK would need re-entering at every rollover. The discord_user_id trap, avoided."""
    lg, john, _a = _seed(test_session)
    services.set_manager_note(test_session, lg, "John", "never flatter him")

    new = League(fpl_league_id="99", name="L27", season_year=2027, is_current=False,
                 phase="offseason")
    test_session.add(new)
    test_session.flush()
    test_session.add(Manager(league_id=new.id, fpl_manager_id="900",
                             name="NewTeam", display_name="John"))
    test_session.commit()

    assert ai_content._manager_notes(test_session)["John"] == "never flatter him"


# ---- THE DISCORD GATE --------------------------------------------------------
def test_generation_never_posts(test_session, monkeypatch):
    """The property this feature is built around. Asserted at the module level: nothing
    in ai_content reaches Discord, so no amount of generating can put something in front
    of the league."""
    posted = []
    import discord_bridge

    monkeypatch.setattr(discord_bridge, "post_message",
                        lambda url, content: posted.append(content) or True)
    # BOTH webhooks are armed. `webhook_url()` defaults to DISCORD_WEBHOOK_URL while the
    # admin route passes ALERT_WEBHOOK_ENV, so arming only one leaves the other a live
    # unguarded send path — a mutation adding `post_message(webhook_url(), ...)` went
    # undetected until this test set both.
    monkeypatch.setenv(discord_bridge.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setenv(discord_bridge.ALERT_WEBHOOK_ENV, "https://example.invalid/alert")
    lg, _j, _a = _seed(test_session)

    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator())
    ai_content.run_after_sync(test_session, lg)
    assert posted == [], "the post-sync path must have no sending code at all"
    assert test_session.query(AiGeneratedContent).one().status == "ready"

    # Belt and braces, structurally: ai_content imports no Discord module and calls no
    # sender. Checked over the AST rather than the text, because the module's own
    # docstring says in prose that it never posts — a substring scan would trip on the
    # very comment documenting the rule.
    tree = ast.parse((pathlib.Path(__file__).resolve().parent.parent
                      / "ai_content.py").read_text())
    imported, called = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            f = node.func
            called.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    assert "discord_bridge" not in imported, "ai_content must not import discord_bridge"
    assert not called & {"post_message", "webhook_url"}, called & {"post_message",
                                                                  "webhook_url"}


def test_posting_requires_the_admin_route(test_session, client, monkeypatch):
    posted = []
    import discord_bridge

    monkeypatch.setattr(discord_bridge, "post_message",
                        lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_ALERT_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    lg, _j, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator())

    assert client.post("/admin/login", data={"password": "pw"}).status_code == 303
    r = client.post(f"/admin/ai/gw-review/{GW}/post")
    assert r.status_code == 303, r.text
    assert len(posted) == 1 and "Chaos" in posted[0]
    test_session.expire_all()
    assert test_session.query(AiGeneratedContent).one().status == "posted"


def test_a_manager_cannot_post_a_review(test_session, client, monkeypatch):
    posted = []
    import discord_bridge

    monkeypatch.setattr(discord_bridge, "post_message",
                        lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_ALERT_WEBHOOK_URL", "https://example.invalid/hook")
    lg, john, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator())

    assert client.post("/login", data={"manager_id": "1",
                                       "password": "pw"}).status_code == 303
    r = client.post(f"/admin/ai/gw-review/{GW}/post")
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
    assert posted == []


def test_posting_without_a_webhook_says_so(test_session, client, monkeypatch):
    """Rather than silently marking it posted."""
    monkeypatch.delenv("DISCORD_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    lg, _j, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator())
    client.post("/admin/login", data={"password": "pw"})

    r = client.post(f"/admin/ai/gw-review/{GW}/post")
    assert r.status_code == 400 and "nowhere to post" in r.text
    test_session.expire_all()
    assert test_session.query(AiGeneratedContent).one().status == "ready"


# ---- the read surfaces -------------------------------------------------------
def test_the_homepage_shows_a_stored_review_and_makes_no_call(test_session, client,
                                                              monkeypatch):
    """A page view reads the row. Generation in a request handler would make a page
    load cost money — CLAUDE.md's architecture rule."""
    called = []
    monkeypatch.setattr(ai_content, "call_model",
                        lambda *a, **k: called.append(1) or None)
    lg, john, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator())

    assert client.post("/login", data={"manager_id": "1",
                                       "password": "pw"}).status_code == 303
    body = client.get("/").text
    assert "Chaos" in body and "One." in body
    assert called == [], "a page view must never generate"


def test_a_discarded_review_leaves_the_homepage_but_stays_on_reviews(test_session,
                                                                     client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    lg, _j, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator())
    client.post("/admin/login", data={"password": "pw"})

    assert client.post(f"/admin/ai/gw-review/{GW}/discard").status_code == 303
    assert "Chaos" not in client.get("/").text
    assert "Chaos" in client.get("/reviews").text, "a decision, not a deletion"


def test_a_failed_review_is_not_shown_as_content(test_session):
    lg, _j, _a = _seed(test_session)
    ai_content.ensure_review(test_session, lg, GW, generate=FakeGenerator(fail=True))
    assert services.latest_gw_review(test_session, lg) is None
    assert len(services.gw_reviews(test_session, lg)) == 1, "still on /reviews"
