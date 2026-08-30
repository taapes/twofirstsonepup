"""Outbound Discord: trade announcements and commissioner alerts.

Two invariants this file exists to defend, both of which have a real incident behind
them in the design notes:

1. **An announcement can never fail, roll back or duplicate a trade.** The write is the
   real work; telling Discord about it is a side effect. Every failure mode here has to
   leave the trade intact and the marker unset, so the next sweep retries.
2. **The marker is persisted, not in-process.** Trades also arrive from the FPL feed,
   and sync is idempotent and re-runs constantly — an in-memory guard would re-announce
   the league's whole history on every run.

No test here touches the network: `send` is injected. `post_message` is the only
function that would, and its own test monkeypatches httpx.
"""

import datetime as dt

import pytest

import discord_bridge
import services
from models import DiscordAlert, Gameweek, League, Manager, Player, Trade

T1 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
T2 = dt.datetime(2026, 1, 2, 12, 0, tzinfo=dt.timezone.utc)


class FakeSender:
    """Records what would have been posted, and can be told to fail."""

    def __init__(self, fail_after=None):
        self.messages = []
        self.fail_after = fail_after

    def __call__(self, content):
        if self.fail_after is not None and len(self.messages) >= self.fail_after:
            return False
        self.messages.append(content)
        return True


def _seed(session, *, sync_locked=False):
    lg = League(fpl_league_id="77", name="L", season_year=2026, is_current=True,
                sync_locked=sync_locked, phase="in_season")
    session.add(lg)
    session.flush()
    session.add(Gameweek(number=1, league_id=lg.id))
    a = Manager(league_id=lg.id, fpl_manager_id="1", name="TeamA", display_name="Ann")
    b = Manager(league_id=lg.id, fpl_manager_id="2", name="TeamB", display_name="Ben")
    session.add_all([a, b])
    session.commit()
    return lg, a, b


def _player_trade(session, lg, a, b, *, name="Saka", fpl_id=101, created_at=T1,
                  announced_at=None):
    p = Player(fpl_id=fpl_id, code=fpl_id * 1000, name=name, position="MID")
    session.add(p)
    session.flush()
    t = Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id, player_id=p.id,
              created_at=created_at, announced_at=announced_at)
    session.add(t)
    session.commit()
    return t


def _fake_sources(monkeypatch, *, flagged=(), health=()):
    """Point the alert collector at fixed data. monkeypatch restores it for us."""
    import services

    monkeypatch.setattr(services, "flagged_actions", lambda db, league: list(flagged))
    monkeypatch.setattr(services, "data_health", lambda db, league: list(health))


# ---- the feature is off unless configured ------------------------------------
def test_no_webhook_configured_is_a_clean_no_op(test_session, monkeypatch):
    """A fresh checkout, the test suite and the demo sandbox must all stay silent."""
    monkeypatch.delenv(discord_bridge.WEBHOOK_ENV, raising=False)
    lg, a, b = _seed(test_session)
    t = _player_trade(test_session, lg, a, b)

    out = discord_bridge.announce_new_trades(test_session, lg)
    assert out == {"sent": 0, "skipped": "not configured"}
    test_session.refresh(t)
    assert t.announced_at is None, "an unconfigured feature must not consume the queue"


def test_alerts_off_without_their_own_webhook(test_session, monkeypatch):
    """The alert channel is separate and separately configured — having the public
    trade webhook set must not start posting commissioner business to it."""
    monkeypatch.setenv(discord_bridge.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.delenv(discord_bridge.ALERT_WEBHOOK_ENV, raising=False)
    lg, _a, _b = _seed(test_session)
    assert discord_bridge.announce_alerts(test_session, lg) == {
        "sent": 0, "skipped": "not configured"}


# ---- trades ------------------------------------------------------------------
def test_a_new_trade_is_announced_once_and_stamped(test_session):
    lg, a, b = _seed(test_session)
    t = _player_trade(test_session, lg, a, b)
    send = FakeSender()

    assert discord_bridge.announce_new_trades(test_session, lg, send=send)["sent"] == 1
    assert "Ann" in send.messages[0] and "Ben" in send.messages[0]
    assert "Saka" in send.messages[0]
    test_session.refresh(t)
    assert t.announced_at is not None

    # The whole point of persisting the marker: sync re-runs constantly.
    assert discord_bridge.announce_new_trades(test_session, lg, send=send)["sent"] == 0
    assert len(send.messages) == 1


def test_an_already_stamped_trade_is_never_announced(test_session):
    """What the migration's back-stamp buys: no first-deploy flood of league history."""
    lg, a, b = _seed(test_session)
    _player_trade(test_session, lg, a, b, announced_at=T1)
    send = FakeSender()
    assert discord_bridge.announce_new_trades(test_session, lg, send=send)["sent"] == 0
    assert send.messages == []


def test_a_failed_post_leaves_the_trade_queued_and_raises_nothing(test_session):
    lg, a, b = _seed(test_session)
    t = _player_trade(test_session, lg, a, b)
    send = FakeSender(fail_after=0)

    out = discord_bridge.announce_new_trades(test_session, lg, send=send)
    assert out["sent"] == 0
    test_session.refresh(t)
    assert t.announced_at is None, "unstamped, so the next sweep retries"

    # And it goes out cleanly once Discord is back.
    good = FakeSender()
    assert discord_bridge.announce_new_trades(test_session, lg, send=good)["sent"] == 1


def test_a_partial_failure_stamps_only_what_actually_posted(test_session):
    """Per-row stamping. A batch stamp would silence the trades it never sent."""
    lg, a, b = _seed(test_session)
    t1 = _player_trade(test_session, lg, a, b, name="Saka", fpl_id=101, created_at=T1)
    t2 = _player_trade(test_session, lg, a, b, name="Guehi", fpl_id=102, created_at=T2)
    send = FakeSender(fail_after=1)

    assert discord_bridge.announce_new_trades(test_session, lg, send=send)["sent"] == 1
    test_session.refresh(t1)
    test_session.refresh(t2)
    assert t1.announced_at is not None and t2.announced_at is None


def test_a_conditional_pick_trade_announces_its_condition(test_session):
    """The condition is most of what a conditional deal MEANS; a line without it
    misreports the trade to the league."""
    from models import TradeConditionTerm

    lg, a, b = _seed(test_session)
    t = Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id,
              pick_round=2, pick_season_year=2027, pick_draft_type="main",
              pick_original_manager=a.id, draft_pick="2027 main R2 (orig TeamA)",
              condition_logic="all", condition_effect="escalate_round",
              pick_round_if_met=1, created_at=T1)
    test_session.add(t)
    test_session.flush()
    test_session.add(TradeConditionTerm(
        trade_id=t.id, metric="league_finish", manager_name="Ann",
        season_year=2027, comparison="<=", threshold=3))
    test_session.commit()

    send = FakeSender()
    discord_bridge.announce_new_trades(test_session, lg, send=send)
    assert "upgrades to R1" in send.messages[0]
    assert "Ann finishes top 3 in 2027" in send.messages[0]


def test_an_unrenderable_trade_is_left_queued_rather_than_silenced(
    test_session, monkeypatch
):
    """Stamping it would hide it forever. Leaving it queued retries a render that may
    well succeed once the underlying data is corrected."""
    import services

    lg, a, b = _seed(test_session)
    t = _player_trade(test_session, lg, a, b)
    send = FakeSender()
    # get_trades sees nothing, so there is no row for this trade id.
    monkeypatch.setattr(services, "get_trades", lambda db: [])

    out = discord_bridge.announce_new_trades(test_session, lg, send=send)
    assert out["sent"] == 0 and send.messages == []
    test_session.refresh(t)
    assert t.announced_at is None


# ---- alerts ------------------------------------------------------------------
def test_alerts_post_once_and_are_deduped_by_content(test_session, monkeypatch):
    """flagged_actions is recomputed from scratch every sync and carries no ids, so
    dedupe has to be content-addressed."""
    import services

    lg, _a, _b = _seed(test_session)
    entries = [{"category": "Injury list", "manager": "Ann", "detail": "return Saka"}]
    monkeypatch.setattr(services, "flagged_actions", lambda db, league: entries)
    monkeypatch.setattr(services, "data_health", lambda db, league: [])

    send = FakeSender()
    assert discord_bridge.announce_alerts(test_session, lg, send=send)["sent"] == 1
    assert "Injury list" in send.messages[0] and "Ann" in send.messages[0]
    assert test_session.query(DiscordAlert).count() == 1

    # Same situation next sync -> silence.
    assert discord_bridge.announce_alerts(test_session, lg, send=send)["sent"] == 0
    assert len(send.messages) == 1

    # Text moves on (a GW passed) -> genuinely new information, so it posts again.
    # This is what sets the re-alert cadence: about once a GW, not once a sync.
    entries[0]["detail"] = "return Saka (2 GWs overdue)"
    assert discord_bridge.announce_alerts(test_session, lg, send=send)["sent"] == 1
    assert len(send.messages) == 2


def test_a_failed_alert_post_records_nothing(test_session, monkeypatch):
    """Recording the dedupe row before a confirmed send would lose the alert forever —
    the same confirm-then-mark ordering confirm_discovery_suggestion uses."""
    lg, _a, _b = _seed(test_session)
    _fake_sources(monkeypatch, flagged=[
        {"category": "Anti-tanking", "manager": "Ben", "detail": "3 blanks"}])

    out = discord_bridge.announce_alerts(test_session, lg, send=FakeSender(fail_after=0))
    assert out == {"sent": 0, "failed": True}
    assert test_session.query(DiscordAlert).count() == 0

    # Retried next sweep, and it lands.
    good = FakeSender()
    assert discord_bridge.announce_alerts(test_session, lg, send=good)["sent"] == 1


def test_a_duplicate_line_within_one_batch_is_collapsed(test_session, monkeypatch):
    """Two checks noticing one situation would otherwise violate the UNIQUE and abort
    the whole transaction on insert."""
    lg, _a, _b = _seed(test_session)
    dup = {"category": "Injury list", "manager": "Ann", "detail": "return Saka"}
    _fake_sources(monkeypatch, flagged=[dict(dup), dict(dup)])

    send = FakeSender()
    assert discord_bridge.announce_alerts(test_session, lg, send=send)["sent"] == 1
    assert test_session.query(DiscordAlert).count() == 1


def test_a_failed_health_check_becomes_an_alert(test_session, monkeypatch):
    lg, _a, _b = _seed(test_session)
    _fake_sources(monkeypatch, health=[
        {"check": "roster sizes", "ok": False, "detail": "Ann has 16"},
        {"check": "standings coverage", "ok": True, "detail": ""},
    ])

    send = FakeSender()
    assert discord_bridge.announce_alerts(test_session, lg, send=send)["sent"] == 1
    assert "roster sizes" in send.messages[0]
    assert "standings coverage" not in send.messages[0], "passing checks stay quiet"


def test_a_long_alert_batch_is_split_not_truncated(test_session, monkeypatch):
    """Discord rejects anything over 2000 chars outright, so losing the tail of an
    alert list would be silent."""
    lg, _a, _b = _seed(test_session)
    _fake_sources(monkeypatch, flagged=[
        {"category": "Anti-tanking", "manager": f"M{i}", "detail": "x" * 120}
        for i in range(40)
    ])

    send = FakeSender()
    assert discord_bridge.announce_alerts(test_session, lg, send=send)["sent"] == 40
    assert len(send.messages) > 1
    assert all(len(msg) <= discord_bridge.MAX_MESSAGE_CHARS for msg in send.messages)
    # Nothing dropped between chunks.
    joined = "\n".join(send.messages)
    assert all(f"M{i}" in joined for i in range(40))


# ---- the transport itself ----------------------------------------------------
@pytest.mark.parametrize("boom", [
    RuntimeError("connection reset"),
    TimeoutError("timed out"),
])
def test_post_message_never_raises(monkeypatch, boom):
    """Every failure collapses to False, because the caller's response to all of them
    is identical: leave the marker unset and retry."""
    def explode(*a, **kw):
        raise boom

    monkeypatch.setattr(discord_bridge.httpx, "post", explode)
    assert discord_bridge.post_message("https://example.invalid/hook", "hi") is False


def test_post_message_reports_an_http_error_as_failure(monkeypatch):
    class Resp:
        def raise_for_status(self):
            raise RuntimeError("404 Not Found")

    monkeypatch.setattr(discord_bridge.httpx, "post", lambda *a, **kw: Resp())
    assert discord_bridge.post_message("https://example.invalid/hook", "hi") is False


def test_post_message_sends_content_and_a_bounded_timeout(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self):
            return None

    def capture(url, json=None, timeout=None):
        seen.update(url=url, json=json, timeout=timeout)
        return Resp()

    monkeypatch.setattr(discord_bridge.httpx, "post", capture)
    assert discord_bridge.post_message("https://example.invalid/hook", "hello") is True
    assert seen["json"] == {"content": "hello"}
    # A hung webhook must never stall the sync it hangs off.
    assert seen["timeout"] == discord_bridge.TIMEOUT_SECONDS


def test_run_outbound_swallows_a_bug_in_our_own_rendering(test_session, monkeypatch):
    """A crash in the announcer must not fail the sync that carried it."""
    monkeypatch.setenv(discord_bridge.WEBHOOK_ENV, "https://example.invalid/hook")
    lg, _a, _b = _seed(test_session)
    monkeypatch.setattr(discord_bridge, "announce_new_trades",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setattr(discord_bridge, "announce_alerts", lambda *a, **kw: {"sent": 0})

    out = discord_bridge.run_outbound(test_session, lg)
    assert "boom" in out["trades"]["error"]
    assert out["alerts"] == {"sent": 0}


# ---- the post-sync entry point -----------------------------------------------
def test_a_frozen_season_announces_nothing(test_session, monkeypatch):
    """Its trades are history and its alerts are settled. Announcing either would be
    posting last season's news — and after a rollover the frozen row is the one the
    league has just stopped caring about."""
    monkeypatch.setenv(discord_bridge.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setenv(discord_bridge.ALERT_WEBHOOK_ENV, "https://example.invalid/hook")
    lg, a, b = _seed(test_session, sync_locked=True)
    t = _player_trade(test_session, lg, a, b)

    assert discord_bridge.run_outbound(test_session, lg) == {"skipped": "frozen"}
    test_session.refresh(t)
    assert t.announced_at is None, "left queued, not consumed"


def test_no_current_league_is_a_clean_no_op(test_session, monkeypatch):
    """A database with no league rows yet — a fresh deploy before the first sync."""
    monkeypatch.setenv(discord_bridge.WEBHOOK_ENV, "https://example.invalid/hook")
    assert discord_bridge.run_outbound(test_session, None) == {"skipped": "no league"}


def test_run_outbound_runs_both_sweeps_through_the_real_wiring(
    test_session, monkeypatch
):
    """End to end from the post-sync entry point, faking only the transport — so this
    covers the env lookup and the two default senders, which the injected-send tests
    above deliberately bypass."""
    posted = []
    monkeypatch.setenv(discord_bridge.WEBHOOK_ENV, "https://example.invalid/trades")
    monkeypatch.setenv(discord_bridge.ALERT_WEBHOOK_ENV, "https://example.invalid/alerts")
    monkeypatch.setattr(discord_bridge, "post_message",
                        lambda url, content: (posted.append((url, content)), True)[1])

    lg, a, b = _seed(test_session)
    t = _player_trade(test_session, lg, a, b)
    _fake_sources(monkeypatch, flagged=[
        {"category": "Injury list", "manager": "Ann", "detail": "return Saka"}])

    out = discord_bridge.run_outbound(test_session, lg)
    assert out["trades"]["sent"] == 1 and out["alerts"]["sent"] == 1

    # Each sweep used ITS OWN webhook — commissioner business must not land in the
    # public channel.
    by_url = {url: content for url, content in posted}
    assert "Saka" in by_url["https://example.invalid/trades"]
    assert "Injury list" in by_url["https://example.invalid/alerts"]

    test_session.refresh(t)
    assert t.announced_at is not None


# ---- the queue must not back up silently --------------------------------------
def test_a_stale_announce_queue_is_reported_on_health(test_session, monkeypatch):
    """A rotated or revoked webhook fails exactly the way a healthy one fails on a
    blip: post returns False, the row stays unstamped, the sweep retries. Correct, and
    indistinguishable from working — the only evidence is a log line nobody reads. So
    the QUEUE is asserted, not the sender."""
    monkeypatch.setenv(discord_bridge.WEBHOOK_ENV, "https://example.invalid/hook")
    lg, a, b = _seed(test_session)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    _player_trade(test_session, lg, a, b, created_at=old)

    check = next(c for c in services.data_health(test_session, lg)
                 if c["check"] == "trades announced to Discord")
    assert check["ok"] is False and "webhook" in check["detail"]


def test_a_freshly_recorded_trade_is_not_yet_a_backlog(test_session, monkeypatch):
    """It just hasn't been swept yet. Flagging it would make the check cry wolf on
    every trade the commissioner enters."""
    monkeypatch.setenv(discord_bridge.WEBHOOK_ENV, "https://example.invalid/hook")
    lg, a, b = _seed(test_session)
    _player_trade(test_session, lg, a, b,
                  created_at=dt.datetime.now(dt.timezone.utc))

    check = next(c for c in services.data_health(test_session, lg)
                 if c["check"] == "trades announced to Discord")
    assert check["ok"] is True


def test_the_check_is_absent_when_the_webhook_is_off(test_session, monkeypatch):
    """A permanent backlog is the CORRECT state for an unconfigured webhook, so
    flagging it would be noise on every install that doesn't use the feature."""
    monkeypatch.delenv(discord_bridge.WEBHOOK_ENV, raising=False)
    lg, a, b = _seed(test_session)
    _player_trade(test_session, lg, a, b)

    names = [c["check"] for c in services.data_health(test_session, lg)]
    assert "trades announced to Discord" not in names
