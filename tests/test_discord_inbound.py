"""Discord inbound: poll, store, parse, resolve, stage, confirm.

THE INVARIANT THIS FILE DEFENDS: nothing reaches league state without a human click.
A wrong player match moves a keeper clock and nothing downstream would flag it
(models.py:679), and the real messages make that permanent rather than cautious —
an IL announcement does not contain the replacement player `place_on_il` requires.

So most of these tests assert what the pipeline REFUSES to do, and that the questions
it cannot answer survive all the way to the review queue instead of being guessed or
dropped.

No network: `fetch_messages` is monkeypatched. The message fixtures are the same real
August 2026 posts pinned in test_discord_parse.py.
"""

import datetime as dt

import pytest

import discord_bridge
import services
from models import (
    DiscordIngest,
    DiscordMessage,
    Gameweek,
    InjuryList,
    League,
    Manager,
    Player,
    PlayerSeason,
    Roster,
    Trade,
)
from rules import RuleViolation

CHANNEL = "555000111"

TRADE_MSG = """🚨🚨 Trade Alert 🚨

John trades
2026 7th
to Michael for
2026 6th"""

TRADE_INITIALS = """🚨 TRADE ALERT 🚨

KT Trades:

Cunha

KS Trades:

6-9 Discoveries"""

IL_MSG = "Saliba IL 1-4 probably longer"


def _raw(mid, content, *, author_id="900001", author="John", ts="2026-08-16T11:05:00+00:00"):
    return {"id": str(mid), "content": content,
            "author": {"id": author_id, "username": author, "global_name": author},
            "timestamp": ts}


def _seed(session):
    lg = League(fpl_league_id="88", name="L", season_year=2026, is_current=True,
                sync_locked=False, phase="in_season")
    session.add(lg)
    session.flush()
    for n in (1, 2):
        session.add(Gameweek(number=n, league_id=lg.id,
                             start_date=dt.date(2026, 8, 10 + n)))
    people = {
        "John": ("1", "900001"), "Michael": ("2", "900002"),
        "Kevin T": ("3", "900003"), "Kevin S": ("4", "900004"),
    }
    managers = {}
    for display, (fpl, discord) in people.items():
        m = Manager(league_id=lg.id, fpl_manager_id=fpl, name=f"Team{fpl}",
                    display_name=display, discord_user_id=discord)
        session.add(m)
        managers[display] = m
    session.commit()
    return lg, managers


def _player(session, lg, name, fpl_id, *, position="DEF", full_name=None):
    p = Player(fpl_id=fpl_id, code=fpl_id * 1000, name=name, position=position,
               full_name=full_name)
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id,
                             name=name, position=position))
    session.commit()
    return p


def _poll(session, lg, monkeypatch, messages, channel=CHANNEL):
    monkeypatch.setattr(discord_bridge, "fetch_messages",
                        lambda cid, after=None, token=None: list(messages))
    monkeypatch.setenv(discord_bridge.BOT_TOKEN_ENV, "fake-token")
    return discord_bridge.poll_channel(session, lg, channel)


# ---- the feature is off unless configured ------------------------------------
def test_no_bot_token_is_a_clean_no_op(test_session, monkeypatch):
    monkeypatch.delenv(discord_bridge.BOT_TOKEN_ENV, raising=False)
    lg, _m = _seed(test_session)
    assert discord_bridge.run_inbound(test_session, lg) == {"skipped": "not configured"}
    assert test_session.query(DiscordMessage).count() == 0


def test_a_frozen_season_is_not_polled(test_session, monkeypatch):
    monkeypatch.setenv(discord_bridge.BOT_TOKEN_ENV, "fake-token")
    lg, _m = _seed(test_session)
    lg.sync_locked = True
    test_session.commit()
    assert discord_bridge.run_inbound(test_session, lg) == {"skipped": "frozen"}


# ---- store raw first ----------------------------------------------------------
def test_messages_are_stored_before_anything_parses_them(test_session, monkeypatch):
    """What makes the pipeline replayable: a parser bug is fixed by re-running over
    these rows, not by re-asking Discord."""
    lg, _m = _seed(test_session)
    _poll(test_session, lg, monkeypatch, [_raw(1, "just chatter, no announcement")])

    row = test_session.query(DiscordMessage).one()
    assert row.content == "just chatter, no announcement"
    assert row.parse_status == "ignored", "stored even though nothing parsed"
    assert row.author_discord_id == "900001"


def test_a_re_poll_stores_nothing_twice(test_session, monkeypatch):
    lg, _m = _seed(test_session)
    msgs = [_raw(1, IL_MSG)]
    _player(test_session, lg, "Saliba", 500)
    _poll(test_session, lg, monkeypatch, msgs)
    _poll(test_session, lg, monkeypatch, msgs)

    assert test_session.query(DiscordMessage).count() == 1
    assert test_session.query(DiscordIngest).count() == 1


def test_the_cursor_advances_numerically_not_lexically(test_session, monkeypatch):
    """Snowflakes are stored as strings (they exceed 2^53), so a lexical max would
    put "9" above "10" and the cursor would walk backwards forever."""
    lg, _m = _seed(test_session)
    _poll(test_session, lg, monkeypatch, [_raw(9, "a"), _raw(10, "b")])

    seen = {}
    monkeypatch.setattr(discord_bridge, "fetch_messages",
                        lambda cid, after=None, token=None: seen.setdefault("after", after) and [])
    discord_bridge.poll_channel(test_session, lg, CHANNEL)
    assert seen["after"] == "10"


# ---- IL: the structurally incomplete case -------------------------------------
def test_an_il_post_stages_without_a_replacement(test_session, monkeypatch):
    """THE fact of this feature. The announcement has no replacement, `place_on_il`
    requires one, so the proposal is partial by nature and a human finishes it."""
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG)])

    ing = test_session.query(DiscordIngest).one()
    assert ing.kind == "il_place" and ing.status == "pending"
    assert ing.payload["injured_fpl_id"] == 500
    assert ing.payload["start_gw"] == 1
    assert ing.payload["replacement_fpl_id"] is None
    assert ing.resolution["needs"] == ["replacement_fpl_id"]


def test_the_author_identifies_the_manager_with_no_name_matching(test_session, monkeypatch):
    """Why discord_user_id exists. IL posts are self-reports, so the poster IS the
    manager — resolved by id, at certainty 1.0, with no fuzzy matching in the path."""
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG, author_id="900002")])

    ing = test_session.query(DiscordIngest).one()
    assert ing.payload["fpl_manager_id"] == "2"
    assert ing.resolution["manager"]["method"] == "discord_id"


def test_an_unmapped_author_stages_with_the_gap_reported(test_session, monkeypatch):
    """It must fail LOUDLY into the queue, not quietly attach to the wrong manager."""
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    _poll(test_session, lg, monkeypatch,
          [_raw(1, IL_MSG, author_id="999999", author="Sir Hefty Boy")])

    ing = test_session.query(DiscordIngest).one()
    assert ing.payload["fpl_manager_id"] is None
    assert any("unmapped" in u["why"] for u in ing.resolution["unresolved"])
    assert services.unmapped_discord_authors(test_session, lg)[0]["name"] == "Sir Hefty Boy"


def test_the_replacement_is_suggested_for_a_GW1_post(test_session, monkeypatch):
    """EVERY real IL post says "1-4", i.e. start_gw=1 — and at GW1 there is no previous
    snapshot to diff against.

    A diff-only implementation therefore returns nothing for exactly the messages that
    motivated this feature, while passing a test written at GW2. That is what the first
    cut did, and it is the silent-inert shape this repo keeps finding: healthy-looking,
    tested, and inert on real data. The squad tier is what makes it work.
    """
    lg, m = _seed(test_session)
    saliba = _player(test_session, lg, "Saliba", 500)
    gabriel = _player(test_session, lg, "Gabriel", 502)
    haaland = _player(test_session, lg, "Haaland", 503, position="FWD")
    gws = {g.number: g for g in test_session.query(Gameweek)}
    for p in (saliba, gabriel, haaland):
        test_session.add(Roster(manager_id=m["John"].id, player_id=p.id,
                                gameweek_id=gws[1].id))
    test_session.commit()

    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG)])   # the real "1-4" text
    ing = test_session.query(DiscordIngest).one()
    names = [s["name"] for s in ing.resolution["replacement_suggestions"]]
    assert names, "a GW1 post must still offer candidates"
    # Narrowed to the injured player's position, which is all place_on_il accepts.
    assert "Haaland" not in names, "a FWD can't replace a DEF"
    assert "Gabriel" in names


def test_the_replacement_prefers_whoever_was_actually_added(test_session, monkeypatch):
    """The missing field is filled in with a real default: whoever this manager
    actually added that gameweek. This is what makes confirming one click."""
    lg, m = _seed(test_session)
    saliba = _player(test_session, lg, "Saliba", 500)
    # Same position as Saliba: place_on_il only accepts a like-for-like swap,
    # so a FWD here would be an unrealistic fixture.
    wissa = _player(test_session, lg, "Wissa", 501)
    gws = {g.number: g for g in test_session.query(Gameweek)}
    john = m["John"]
    test_session.add(Roster(manager_id=john.id, player_id=saliba.id,
                            gameweek_id=gws[1].id))
    test_session.add(Roster(manager_id=john.id, player_id=saliba.id,
                            gameweek_id=gws[2].id))
    test_session.add(Roster(manager_id=john.id, player_id=wissa.id,
                            gameweek_id=gws[2].id))
    test_session.commit()

    _poll(test_session, lg, monkeypatch, [_raw(1, "Saliba IL 2-5")])
    ing = test_session.query(DiscordIngest).one()
    suggestions = ing.resolution["replacement_suggestions"]
    assert suggestions[0] == {"fpl_id": 501, "name": "Wissa", "position": "DEF",
                              "reason": "added"}
    # And never the injured player himself — he is on his own squad, and place_on_il
    # refuses "replace Saliba with Saliba".
    assert "Saliba" not in [s["name"] for s in suggestions]


# ---- trades -------------------------------------------------------------------
def test_a_trade_post_stages_both_sides(test_session, monkeypatch):
    lg, _m = _seed(test_session)
    _poll(test_session, lg, monkeypatch, [_raw(1, TRADE_MSG)])

    ing = test_session.query(DiscordIngest).one()
    assert ing.kind == "trade"
    assert (ing.payload["a_fpl"], ing.payload["b_fpl"]) == ("1", "2")
    assert ing.payload["a_picks"] == ["2026:main:7:John"]
    assert ing.payload["b_picks"] == ["2026:main:6:Michael"]


def test_the_author_is_only_a_hint_for_a_trade(test_session, monkeypatch):
    """A real sampled message has John announcing a trade between two OTHER managers,
    so neither side may fall back to the poster."""
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Cunha", 600, position="FWD")
    # Posted by John (900001), but the trade is KT <-> KS.
    _poll(test_session, lg, monkeypatch, [_raw(1, TRADE_INITIALS, author_id="900001")])

    ing = test_session.query(DiscordIngest).one()
    assert (ing.payload["a_fpl"], ing.payload["b_fpl"]) == ("3", "4")
    assert ing.resolution["a"]["method"] == "initials"


def test_ambiguous_initials_resolve_to_nobody(test_session, monkeypatch):
    """This league has two K-managers, so the ambiguity check is load-bearing. A
    near-miss on a manager hands a pick to the wrong person."""
    lg, m = _seed(test_session)
    m["Kevin S"].display_name = "Kevin T2"   # both now initial to "KT"
    test_session.commit()
    _player(test_session, lg, "Cunha", 600, position="FWD")
    _poll(test_session, lg, monkeypatch, [_raw(1, TRADE_INITIALS)])

    ing = test_session.query(DiscordIngest).one()
    assert ing.payload["a_fpl"] is None
    assert "matches 2 managers" in ing.resolution["a"]["why"]


def test_an_unresolvable_asset_reaches_the_queue_as_a_question(test_session, monkeypatch):
    """"6-9 Discoveries" is not guessable. Dropping it would silently shrink the deal;
    guessing would reassign someone else's slot."""
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Cunha", 600, position="FWD")
    _poll(test_session, lg, monkeypatch, [_raw(1, TRADE_INITIALS)])

    ing = test_session.query(DiscordIngest).one()
    texts = [u["text"] for u in ing.resolution["unresolved"]]
    assert "6-9 Discoveries" in texts
    assert ing.confidence < 1.0


def test_an_overall_pick_number_is_never_converted_silently(test_session, monkeypatch):
    """"Pick 6" is a position, not a round, and converting needs that season's draft
    order — which may not exist yet. It becomes a question, not an assumption."""
    lg, _m = _seed(test_session)
    _poll(test_session, lg, monkeypatch, [_raw(
        1, "🚨 TRADE ALERT 🚨\nJohn trades\n2026 Pick 6\nto Michael for\n2026 6th")])

    ing = test_session.query(DiscordIngest).one()
    assert ing.payload["a_picks"] == [], "not guessed into a round"
    assert any("position, not a round" in u["why"] for u in ing.resolution["unresolved"])


def test_the_assumed_pick_owner_is_reported_not_hidden(test_session, monkeypatch):
    """Nothing in any sampled message says whose pick a traded pick originally was.
    Assuming the giver is reasonable and is shown as an assumption."""
    lg, _m = _seed(test_session)
    _poll(test_session, lg, monkeypatch, [_raw(1, TRADE_MSG)])
    ing = test_session.query(DiscordIngest).one()
    assert ing.resolution["picks"]["a"][0]["assumed_owner"] == "John"


# ---- the review loop ----------------------------------------------------------
def test_nothing_is_applied_without_a_click(test_session, monkeypatch):
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG), _raw(2, TRADE_MSG)])

    assert test_session.query(InjuryList).count() == 0
    assert test_session.query(Trade).count() == 0
    assert {i.status for i in test_session.query(DiscordIngest)} == {"pending"}


def test_confirming_an_il_proposal_needs_the_replacement_supplied(test_session, monkeypatch):
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG)])
    ing = test_session.query(DiscordIngest).one()

    with pytest.raises(RuleViolation, match="replacement_fpl_id"):
        services.apply_discord_ingest(test_session, lg, str(ing.id))
    test_session.refresh(ing)
    assert ing.status == "failed" and test_session.query(InjuryList).count() == 0


def test_confirming_with_a_replacement_places_the_player(test_session, monkeypatch):
    lg, m = _seed(test_session)
    saliba = _player(test_session, lg, "Saliba", 500)
    wissa = _player(test_session, lg, "Wissa", 501)
    gws = {g.number: g for g in test_session.query(Gameweek)}
    for p in (saliba, wissa):
        test_session.add(Roster(manager_id=m["John"].id, player_id=p.id,
                                gameweek_id=gws[1].id))
    test_session.commit()

    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG)])
    ing = test_session.query(DiscordIngest).one()
    services.apply_discord_ingest(test_session, lg, str(ing.id), replacement_fpl_id=501)

    test_session.refresh(ing)
    assert ing.status == "applied"
    entry = test_session.query(InjuryList).one()
    assert entry.player_id == saliba.id and entry.replacement_id == wissa.id
    assert ing.applied_entity_id == entry.id, "recorded, so an undo is possible"


def test_a_rule_violation_is_captured_on_the_row_not_swallowed(test_session, monkeypatch):
    """It means Discord and the league's actual state disagree, which is worth seeing
    rather than hiding."""
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    _player(test_session, lg, "Wissa", 501)
    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG)])
    ing = test_session.query(DiscordIngest).one()

    # Nobody rosters either player, so place_on_il refuses.
    with pytest.raises(RuleViolation):
        services.apply_discord_ingest(test_session, lg, str(ing.id),
                                      replacement_fpl_id=501)
    test_session.refresh(ing)
    assert ing.status == "failed" and ing.error


def test_a_trade_read_from_discord_is_not_announced_back_to_discord(
        test_session, monkeypatch):
    """The echo. `announced_at IS NULL` is the announce queue, so a trade confirmed from
    a Discord proposal would be posted straight back to the channel that taught us about
    it on the next sync. Discord already knows — that's where it came from.

    Found 2026-09-03 while explaining which channel each webhook points at: the
    commissioner reads #trades AND would have pointed the public webhook at it.
    """
    lg, _m = _seed(test_session)
    _poll(test_session, lg, monkeypatch, [_raw(1, TRADE_MSG)])
    ing = test_session.query(DiscordIngest).one()
    services.apply_discord_ingest(test_session, lg, str(ing.id))

    rows = test_session.query(Trade).all()
    assert rows, "the trade was recorded"
    assert all(t.announced_at is not None for t in rows), \
        "a trade read from Discord must not be queued for announcement"

    sent = []
    out = discord_bridge.announce_new_trades(
        test_session, lg, send=lambda content: sent.append(content) or True)
    assert sent == [] and out["sent"] == 0


def test_an_ordinary_trade_is_still_queued_for_announcement(test_session):
    """The other half: the guard must not switch announcing off for trades the league
    hasn't heard about, which is the feature's whole purpose."""
    lg, m = _seed(test_session)
    saka = _player(test_session, lg, "Saka", 500)
    services.record_trade(
        test_session, lg, a_fpl=m["John"].fpl_manager_id,
        b_fpl=m["Kevin T"].fpl_manager_id,
        a_players=[saka.fpl_id], b_players=[], a_picks=[], b_picks=[])

    rows = test_session.query(Trade).all()
    assert rows and all(t.announced_at is None for t in rows)

    sent = []
    discord_bridge.announce_new_trades(
        test_session, lg, send=lambda content: sent.append(content) or True)
    assert len(sent) == 1


def test_a_dismissed_proposal_is_never_re_proposed(test_session, monkeypatch):
    """Kept, not deleted — the discovery-suggestion rule. Re-parsing must not revive
    a decision the commissioner already made."""
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    msgs = [_raw(1, IL_MSG)]
    _poll(test_session, lg, monkeypatch, msgs)
    ing = test_session.query(DiscordIngest).one()
    services.reject_discord_ingest(test_session, lg, str(ing.id))

    # Re-ingest the same stored message directly (a parser re-run).
    msg = test_session.query(DiscordMessage).one()
    discord_bridge.ingest_message(test_session, lg, msg)
    test_session.commit()

    rows = test_session.query(DiscordIngest).all()
    assert len(rows) == 1 and rows[0].status == "rejected"
    assert services.discord_ingest_queue(test_session, lg) == []


def test_re_ingesting_a_pending_proposal_updates_it_in_place(test_session, monkeypatch):
    lg, _m = _seed(test_session)
    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG)])
    ing = test_session.query(DiscordIngest).one()
    assert ing.payload["injured_fpl_id"] is None, "player not in the pool yet"

    # The player arrives, and a re-parse now resolves him.
    _player(test_session, lg, "Saliba", 500)
    msg = test_session.query(DiscordMessage).one()
    discord_bridge.ingest_message(test_session, lg, msg)
    test_session.commit()

    assert test_session.query(DiscordIngest).count() == 1
    test_session.refresh(ing)
    assert ing.payload["injured_fpl_id"] == 500


# ---- transport ----------------------------------------------------------------
def test_a_bad_token_disables_rather_than_retries(test_session, monkeypatch):
    """401s count toward a Cloudflare ban at 10,000 per 10 minutes, so looping on one
    is how an IP gets blocked."""
    lg, _m = _seed(test_session)

    def boom(cid, after=None, token=None):
        raise discord_bridge.DiscordAuthError("discord rejected the bot token")

    monkeypatch.setattr(discord_bridge, "fetch_messages", boom)
    monkeypatch.setenv(discord_bridge.BOT_TOKEN_ENV, "bad")
    out = discord_bridge.poll_channel(test_session, lg, CHANNEL)
    assert out["disabled"] is True


def test_one_unparseable_message_does_not_lose_the_rest(test_session, monkeypatch):
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    real = discord_bridge.ingest_message

    def flaky(db, league, msg):
        if msg.discord_message_id == "1":
            raise ValueError("boom")
        return real(db, league, msg)

    monkeypatch.setattr(discord_bridge, "ingest_message", flaky)
    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG), _raw(2, IL_MSG)])

    statuses = {m.discord_message_id: m.parse_status
                for m in test_session.query(DiscordMessage)}
    assert statuses == {"1": "failed", "2": "staged"}


def test_probe_tells_a_quiet_channel_from_an_unreadable_one(monkeypatch):
    """Both misconfigurations return 200 with nothing useful, so they are
    indistinguishable from "no new messages" without this."""
    class Resp:
        def __init__(self, payload, code=200):
            self._p, self.status_code = payload, code

        def json(self):
            return self._p

    monkeypatch.setenv(discord_bridge.BOT_TOKEN_ENV, "t")
    monkeypatch.setattr(discord_bridge.httpx, "get",
                        lambda *a, **kw: Resp([{"id": "1", "content": ""}]))
    probe = discord_bridge.probe_channel("123")
    assert probe["ok"] is False and "MESSAGE CONTENT" in probe["detail"]

    monkeypatch.setattr(discord_bridge.httpx, "get", lambda *a, **kw: Resp([]))
    assert "Read Message History" in discord_bridge.probe_channel("123")["detail"]

    monkeypatch.setattr(discord_bridge.httpx, "get", lambda *a, **kw: Resp([], 403))
    assert discord_bridge.probe_channel("123")["ok"] is False


# ---- the identity mapping survives a rollover ---------------------------------
def test_discord_user_id_is_carried_across_the_rollover(test_session):
    """Without this the map silently empties at the next rollover and every proposal
    quietly loses its one certain identity, with nothing reporting it."""
    lg, m = _seed(test_session)
    new = League(fpl_league_id="99", name="L27", season_year=2027, is_current=False,
                 phase="offseason")
    test_session.add(new)
    test_session.flush()
    pairing = {}
    for display, old in m.items():
        nm = Manager(league_id=new.id, fpl_manager_id=f"9{old.fpl_manager_id}",
                     name=f"New{old.fpl_manager_id}")
        test_session.add(nm)
        test_session.flush()
        pairing[nm.id] = old.id
    test_session.commit()

    services.advance_season(test_session, lg, new, pairing=pairing)

    carried = {
        mm.display_name: mm.discord_user_id
        for mm in test_session.query(Manager).filter_by(league_id=new.id)
    }
    assert carried["John"] == "900001" and carried["Kevin S"] == "900004"


# ---- the review UI ------------------------------------------------------------
@pytest.fixture()
def client(test_session):
    """Matches the convention in the other route tests: `test_session` already patches
    db.SessionLocal, so the app resolves to the test database on its own."""
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app, follow_redirects=False)


def _login_admin(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "inbound-test-pw")
    r = client.post("/admin/login", data={"password": "inbound-test-pw"})
    assert r.status_code == 303, r.text


def test_the_queue_renders_with_the_unanswered_questions_visible(
    test_session, client, monkeypatch
):
    """The questions the parser refused to guess must reach a human. A template error
    here strands every proposal silently."""
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Cunha", 600, position="FWD")
    _poll(test_session, lg, monkeypatch, [_raw(1, TRADE_INITIALS)])
    _login_admin(client, monkeypatch)

    r = client.get("/admin/corrections")
    assert r.status_code == 200, r.text
    assert "From Discord" in r.text
    assert "6-9 Discoveries" in r.text, "the unanswerable asset is shown, not dropped"
    assert "/admin/corrections/discord/apply" in r.text


def test_the_il_queue_offers_the_suggested_replacement(test_session, client, monkeypatch):
    lg, m = _seed(test_session)
    saliba = _player(test_session, lg, "Saliba", 500)
    wissa = _player(test_session, lg, "Wissa", 501, position="FWD")
    gws = {g.number: g for g in test_session.query(Gameweek)}
    test_session.add(Roster(manager_id=m["John"].id, player_id=saliba.id,
                            gameweek_id=gws[1].id))
    for p in (saliba, wissa):
        test_session.add(Roster(manager_id=m["John"].id, player_id=p.id,
                                gameweek_id=gws[2].id))
    test_session.commit()
    _poll(test_session, lg, monkeypatch, [_raw(1, "Saliba IL 2-5")])
    _login_admin(client, monkeypatch)

    r = client.get("/admin/corrections")
    assert r.status_code == 200, r.text
    assert 'name="replacement_fpl_id"' in r.text
    assert "Wissa" in r.text, "pre-filled from the roster diff"


def test_a_manager_cannot_apply_a_proposal(test_session, client, monkeypatch):
    lg, _m = _seed(test_session)
    _player(test_session, lg, "Saliba", 500)
    _poll(test_session, lg, monkeypatch, [_raw(1, IL_MSG)])
    ing = test_session.query(DiscordIngest).one()

    r = client.post("/admin/corrections/discord/apply",
                    data={"ingest_id": str(ing.id), "replacement_fpl_id": "501"})
    # The site-wide login gate answers first, before the route's own admin check —
    # either way nothing is applied, which is the part that matters.
    assert r.status_code == 303
    test_session.expire_all()
    assert test_session.query(InjuryList).count() == 0


def test_mapping_a_discord_account_from_the_health_page(test_session, client, monkeypatch):
    lg, m = _seed(test_session)
    m["John"].discord_user_id = None
    test_session.commit()
    _login_admin(client, monkeypatch)

    r = client.post("/admin/discord/map",
                    data={"fpl_manager_id": "1", "discord_user_id": "900001"})
    assert r.status_code == 303
    test_session.expire_all()
    assert test_session.query(Manager).filter_by(fpl_manager_id="1").one().discord_user_id == "900001"


def test_one_discord_account_cannot_be_two_managers(test_session, client, monkeypatch):
    """Otherwise the author lookup becomes ambiguous exactly where it is supposed to
    be certain."""
    lg, _m = _seed(test_session)
    with pytest.raises(RuleViolation, match="already mapped"):
        services.map_discord_author(test_session, lg, fpl_manager_id="1",
                                    discord_user_id="900002")
