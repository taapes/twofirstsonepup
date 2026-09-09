"""Outbound Discord for the discovery draft: a pick made, the clock moving, a
deadline approaching.

Two different persistence shapes, matching what each fact actually is:

  - A PICK is a true one-time completion event on a real row — `discovery_announced_at`
    mirrors `Trade.announced_at` exactly (see test_discord_outbound.py's own docstring
    for why that invariant matters: never fail/roll back/duplicate, and the marker is
    persisted, not in-process).
  - The CLOCK MOVING and a DEADLINE APPROACHING are both derived facts with no row of
    their own, so both dedupe through the existing `discord_alerts` fingerprint table
    — identical text never re-posts, changed text does.

No test here touches the network: `send` is injected, same as test_discord_outbound.py.
"""

import datetime as dt

import services
from models import DiscordAlert, DraftPick, Gameweek, League, Manager, Standing
from rules import DISCOVERY_PICK_CLOCK_HOURS
from tests.test_discord_outbound import FakeSender

import discord_bridge

SEASON = 2026
CLOCK = dt.timedelta(hours=DISCOVERY_PICK_CLOCK_HOURS)


def _seed(session, *, anchor_at=None, anchor_pick=1):
    """Three managers, reverse standings C,B,A -> R1 C,B,A / R2 A,B,C (6 picks)."""
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
                    display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i,
                             total=30 - i, points_for=300 - i))
        mgrs[name] = m
    session.commit()
    return lg, mgrs


# ---- announce_discovery_picks: a real row, mirrors the trade sweep -------------
def test_no_webhook_configured_is_a_clean_no_op(test_session, monkeypatch):
    monkeypatch.delenv(discord_bridge.DISCOVERY_WEBHOOK_ENV, raising=False)
    lg, m = _seed(test_session)
    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Kid A", round=1)

    out = discord_bridge.announce_discovery_picks(test_session, lg)
    assert out == {"sent": 0, "skipped": "not configured"}
    pick = test_session.query(DraftPick).one()
    assert pick.discovery_announced_at is None


def test_a_pending_pick_is_announced_and_stamped(test_session):
    lg, m = _seed(test_session)
    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Kid A", round=1)

    send = FakeSender()
    out = discord_bridge.announce_discovery_picks(test_session, lg, send=send)
    assert out == {"sent": 1, "pending": 0}
    assert "C" in send.messages[0] and "Kid A" in send.messages[0]
    pick = test_session.query(DraftPick).one()
    assert pick.discovery_announced_at is not None


def test_an_already_announced_pick_is_never_resent(test_session):
    lg, m = _seed(test_session)
    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Kid A", round=1)
    send = FakeSender()
    discord_bridge.announce_discovery_picks(test_session, lg, send=send)

    again = FakeSender()
    out = discord_bridge.announce_discovery_picks(test_session, lg, send=again)
    assert out == {"sent": 0}
    assert again.messages == []


def test_an_overwrite_correction_is_not_reannounced(test_session):
    """The pick's CONTENT can be corrected after the fact; the announcement that it
    happened already went out and must not repeat just because the name changed."""
    lg, m = _seed(test_session)
    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Kid A", round=1)
    discord_bridge.announce_discovery_picks(test_session, lg, send=FakeSender())

    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Corrected Kid", round=1,
        overwrite=True)

    again = FakeSender()
    out = discord_bridge.announce_discovery_picks(test_session, lg, send=again)
    assert out == {"sent": 0}
    assert again.messages == []


def test_a_failed_post_leaves_the_pick_queued_for_retry(test_session):
    lg, m = _seed(test_session)
    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Kid A", round=1)

    out = discord_bridge.announce_discovery_picks(
        test_session, lg, send=FakeSender(fail_after=0))
    assert out == {"sent": 0, "pending": 1}
    assert test_session.query(DraftPick).one().discovery_announced_at is None

    retried = discord_bridge.announce_discovery_picks(
        test_session, lg, send=FakeSender())
    assert retried == {"sent": 1, "pending": 0}


# ---- announce_discovery_clock: derived facts, dedupe via DiscordAlert ----------
def test_no_webhook_is_a_clean_no_op_for_the_clock_sweep(test_session, monkeypatch):
    monkeypatch.delenv(discord_bridge.DISCOVERY_WEBHOOK_ENV, raising=False)
    lg, _m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    assert discord_bridge.announce_discovery_clock(test_session, lg, SEASON) == \
        {"sent": 0, "skipped": "not configured"}


def test_no_active_clock_is_skipped(test_session):
    lg, _m = _seed(test_session, anchor_at=None)
    out = discord_bridge.announce_discovery_clock(
        test_session, lg, SEASON, send=FakeSender())
    assert out == {"sent": 0, "skipped": "no active clock"}


def test_on_the_clock_fires_once_per_pick_not_every_heartbeat(test_session):
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    send = FakeSender()
    first = discord_bridge.announce_discovery_clock(test_session, lg, SEASON, send=send)
    assert first["sent"] == 1
    assert "C" in send.messages[0]  # C picks first (reverse standings)

    again = discord_bridge.announce_discovery_clock(test_session, lg, SEASON, send=send)
    assert again["sent"] == 0
    assert len(send.messages) == 1, "the heartbeat re-checking must not re-post"


def test_no_deadline_warning_far_from_expiry(test_session):
    lg, _m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    send = FakeSender()
    discord_bridge.announce_discovery_clock(test_session, lg, SEASON, send=send)
    assert not any("left to pick" in msg for msg in send.messages)


def test_a_deadline_warning_fires_within_the_threshold(test_session):
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc)
                                          - (CLOCK - dt.timedelta(hours=1)))
    send = FakeSender()
    out = discord_bridge.announce_discovery_clock(
        test_session, lg, SEASON, warn_within_hours=3, send=send)
    assert out["sent"] == 2  # on-clock (already fired conceptually, but first call) + warning
    assert any("left to pick" in msg for msg in send.messages)


def test_the_deadline_warning_fires_exactly_once_as_the_countdown_ticks_down(
        test_session):
    """The bug this pins: the warning's DEDUP key must not include the rounded
    hours-remaining figure shown in the text, or it would refire at every hour
    boundary as the deadline approaches instead of firing once."""
    anchor = dt.datetime.now(dt.timezone.utc) - (CLOCK - dt.timedelta(hours=2, minutes=50))
    lg, m = _seed(test_session, anchor_at=anchor)
    send = FakeSender()
    discord_bridge.announce_discovery_clock(
        test_session, lg, SEASON, warn_within_hours=3, send=send)
    warnings_so_far = sum("left to pick" in msg for msg in send.messages)
    assert warnings_so_far == 1

    # Simulate 20 minutes passing (the rounded "hours left" figure would change from
    # 2 to 1 partway through this) by moving the anchor back further, which is
    # equivalent to "checking later" for this pure-function-of-now derivation.
    lg.discovery_clock_anchor_at = anchor - dt.timedelta(minutes=90)
    test_session.commit()
    discord_bridge.announce_discovery_clock(
        test_session, lg, SEASON, warn_within_hours=3, send=send)
    assert sum("left to pick" in msg for msg in send.messages) == 1, \
        "must not have refired just because the displayed hour count changed"


def test_both_discovery_sweeps_are_wired_into_run_outbound(test_session, monkeypatch):
    monkeypatch.setenv(discord_bridge.DISCOVERY_WEBHOOK_ENV, "https://example.invalid/disc")
    monkeypatch.setattr(discord_bridge, "post_message",
                        lambda url, content: True)
    lg, m = _seed(test_session, anchor_at=dt.datetime.now(dt.timezone.utc))
    services.record_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1,
        owner_fpl=m["C"].fpl_manager_id, player_name="Kid A", round=1)

    out = discord_bridge.run_outbound(test_session, lg)
    assert "discovery_picks" in out and "discovery_clock" in out
    assert out["discovery_picks"]["sent"] == 1
