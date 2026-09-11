"""Inbound discovery-pick parsing: managers posting picks in the Discord channel
instead of using /discovery/{year}/pick.

THE SAFETY PROPERTY THIS FILE DEFENDS: parse_discovery_pick has no keyword or digit
pattern to anchor on -- a real post is just a bare player name -- so the only thing
that keeps it from mis-staging ordinary channel chatter is
services.manager_discovery_open_slots: a message is only ever staged when its
(discord_user_id-mapped) author currently has an open discovery-draft slot. Nothing
here ever applies without a commissioner clicking Apply, same as the existing
trade/IL bridge.

No network: fetch_messages is monkeypatched, matching tests/test_discord_inbound.py.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime as dt

import pytest

import discord_bridge
import services
from models import DiscordIngest, DiscordMessage, DraftPick, Gameweek, League, Manager, Standing

CHANNEL = "777000111"
SEASON = 2026


def _seed(session, *, anchor_pick=1, anchor_at=None):
    """Three managers. Reverse standings puts C first: R1 C,B,A / R2 A,B,C -- 6 picks.
    Mirrors tests/test_discovery_pick_clock.py's _seed, plus discord_user_id."""
    lg = League(fpl_league_id="1", name="S", season_year=SEASON, is_current=True,
                sync_locked=False, phase="in_season", discovery_open=True,
                discovery_clock_anchor_pick=anchor_pick,
                discovery_clock_anchor_at=anchor_at)
    session.add(lg)
    session.flush()
    session.add(Gameweek(number=1, league_id=lg.id))
    session.flush()
    mgrs = {}
    for i, name in enumerate(["A", "B", "C"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=f"Team{name}",
                    display_name=name, discord_user_id=f"90000{i}")
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i,
                             total=30 - i, points_for=300 - i))
        mgrs[name] = m
    session.commit()
    return lg, mgrs


def _raw(mid, content, *, author_id, author="Someone", ts="2026-09-11T11:05:00+00:00"):
    return {"id": str(mid), "content": content,
            "author": {"id": author_id, "username": author, "global_name": author},
            "timestamp": ts}


def _poll(session, lg, monkeypatch, messages, channel=CHANNEL):
    monkeypatch.setattr(discord_bridge, "fetch_messages",
                        lambda cid, after=None, token=None: list(messages))
    monkeypatch.setenv(discord_bridge.BOT_TOKEN_ENV, "fake-token")
    return discord_bridge.poll_channel(
        session, lg, channel, ingest_fn=discord_bridge.ingest_discovery_pick_message,
    )


def _ingests(session):
    return session.query(DiscordIngest).filter_by(kind="discovery_pick").all()


# ---- the core safety property: no open slot -> ignored, never staged ---------
def test_a_manager_with_no_open_slot_is_ignored_not_staged(test_session, monkeypatch):
    """C's both picks are already made -- no open slot left for C at all, even
    though A and B still have open slots. A bare word from C must not be staged."""
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    now = dt.datetime.now(dt.timezone.utc)
    for pick_number, rnd in ((1, 1), (6, 2)):
        test_session.add(DraftPick(league_id=lg.id, season_year=SEASON,
                                   draft_type="discovery", round=rnd,
                                   pick_number=pick_number, manager_id=m["C"].id,
                                   player_label="Already Picked", source="discovery",
                                   picked_at=now))
    test_session.commit()

    _poll(test_session, lg, monkeypatch, [_raw(1, "Some Random Kid", author_id="900003")])

    row = test_session.query(DiscordMessage).one()
    assert row.parse_status == "ignored"
    assert _ingests(test_session) == []


def test_ordinary_chatter_from_an_unmapped_poster_with_no_manager_match_is_still_ignored_if_multiline(
        test_session, monkeypatch):
    lg, _m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    _poll(test_session, lg, monkeypatch,
          [_raw(1, "good luck everyone\nmay the best team win", author_id="900999")])
    assert test_session.query(DiscordMessage).one().parse_status == "ignored"
    assert _ingests(test_session) == []


# ---- exactly one open slot: staged with the pick filled in --------------------
def test_a_manager_with_one_open_slot_stages_the_pick_with_the_slot_filled_in(
        test_session, monkeypatch):
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    _poll(test_session, lg, monkeypatch,
          [_raw(1, "Wolfsburg's new signing", author_id="900003")])  # C, pick 1

    row = test_session.query(DiscordMessage).one()
    assert row.parse_status == "staged"
    ingest = _ingests(test_session)[0]
    assert ingest.payload["pick_number"] == 1
    assert ingest.payload["owner_fpl"] == "3"
    assert ingest.payload["player_name"] == "Wolfsburg's new signing"
    assert ingest.confidence == 1.0
    assert ingest.resolution["unresolved"] == []


# ---- two open slots at once (rule 5): staged without guessing which one ------
def test_a_manager_with_two_open_slots_stages_without_guessing_which_pick(
        test_session, monkeypatch):
    """Expire picks 1-3 (nobody home); the clock lands on pick 4 -- ALSO A's, since
    R1's last pick and R2's first pick belong to the same manager in a snake. A now
    holds pick 3 (missed) and pick 4 (current) at once."""
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc)
                                          - 3 * dt.timedelta(hours=24) - dt.timedelta(hours=1))
    _poll(test_session, lg, monkeypatch,
          [_raw(1, "Some Prospect", author_id="900001")])  # A

    ingest = _ingests(test_session)[0]
    assert "pick_number" not in ingest.payload
    assert {s["pick"] for s in ingest.resolution["open_slots"]} == {3, 4}
    assert ingest.confidence == 0.5
    assert any("which pick" in u["text"] for u in ingest.resolution["unresolved"])


# ---- an unmapped author: staged at low confidence, nothing guessed -----------
def test_an_unmapped_author_is_staged_at_low_confidence_with_nothing_guessed(
        test_session, monkeypatch):
    lg, _m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    _poll(test_session, lg, monkeypatch,
          [_raw(1, "Some Prospect", author_id="900999", author="RandomUser")])

    ingest = _ingests(test_session)[0]
    assert "owner_fpl" not in ingest.payload
    assert "pick_number" not in ingest.payload
    assert ingest.payload["player_name"] == "Some Prospect"
    assert ingest.confidence == 0.3
    assert ingest.resolution["unresolved"][0]["text"] == "RandomUser"


# ---- confirm -> a real DraftPick lands ----------------------------------------
def test_applying_a_staged_discovery_pick_creates_a_real_draft_pick(test_session, monkeypatch):
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    _poll(test_session, lg, monkeypatch,
          [_raw(1, "Some Prospect", author_id="900003")])  # C, pick 1
    ingest = _ingests(test_session)[0]

    result = services.apply_discord_ingest(test_session, lg, str(ingest.id))
    assert result["applied"] == "discovery_pick"

    pick = test_session.query(DraftPick).filter_by(
        league_id=lg.id, season_year=SEASON, draft_type="discovery", pick_number=1,
    ).one()
    assert pick.player_label == "Some Prospect"
    assert pick.manager_id == m["C"].id

    test_session.refresh(ingest)
    assert ingest.status == "applied"
    assert ingest.applied_entity_id == pick.id


def test_applying_a_two_open_slot_pick_requires_the_pick_number_override(test_session, monkeypatch):
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc)
                                          - 3 * dt.timedelta(hours=24) - dt.timedelta(hours=1))
    _poll(test_session, lg, monkeypatch, [_raw(1, "Some Prospect", author_id="900001")])
    ingest = _ingests(test_session)[0]

    from rules import RuleViolation
    with pytest.raises(RuleViolation, match="pick_number"):
        services.apply_discord_ingest(test_session, lg, str(ingest.id))

    result = services.apply_discord_ingest(test_session, lg, str(ingest.id), pick_number=4)
    assert result["applied"] == "discovery_pick"
    pick = test_session.query(DraftPick).filter_by(
        league_id=lg.id, season_year=SEASON, draft_type="discovery", pick_number=4,
    ).one()
    assert pick.player_label == "Some Prospect"
