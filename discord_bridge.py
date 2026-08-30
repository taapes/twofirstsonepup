"""Outbound Discord: announce new trades, push commissioner alerts.

WHY THIS IS A MODULE AND NOT A FEW LINES IN services.py
-------------------------------------------------------
This is the first OUTBOUND network call in the codebase. CLAUDE.md's architecture rule
("don't add live FPL calls into request handlers") is written about inbound sync, but
its reasoning — request latency and an external service's availability leaking into
ours — applies identically in this direction, so the same discipline is kept:

  * Nothing here is called from a request handler. The only caller is the post-sync
    hook in `sync.run_sync`.
  * Nothing here runs inside a service transaction, and nothing here is called from
    `record_audit`. Audit is a write-path primitive; hanging an HTTP call off it would
    give every future audited action a network dependency.
  * `post_message` NEVER raises and never rolls anything back. A trade is the real
    work; announcing it is a side effect. A Discord outage, a rotated webhook or a
    404'd channel must leave the trade recorded and the marker unset, so the next
    sweep simply tries again.
  * The timeout is short and absolute. A hung webhook must not stall the sync.

The feature is OFF when its env var is unset — no config UI, no database flag. That
makes a fresh checkout, the test suite and the demo sandbox all silent by default.

SENDER SEAM
-----------
Both announcers take a `send` callable. Today it posts to an incoming webhook, which
needs no bot, no Developer Portal app, no privileged intent and no Manage Server
permission — the URL is the whole credential. The inbound half (a later session) needs
a real bot token anyway, and a bot can do one thing a webhook cannot: reply to a
specific message. When that lands, only `_webhook_sender` is replaced; the sweep logic,
the markers and the rendering below are untouched.
"""

import datetime
import hashlib
import logging
import os

import httpx

log = logging.getLogger(__name__)

# The webhook URL IS the credential — env only, never a database column and never a
# config page, matching SYNC_AUTH_TOKEN's handling in SECURITY.md.
WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
ALERT_WEBHOOK_ENV = "DISCORD_ALERT_WEBHOOK_URL"

# Deliberately short. The sync it hangs off has real work to do, and a slow webhook is
# indistinguishable from a dead one for our purposes: either way we skip and retry.
TIMEOUT_SECONDS = 5.0

# Discord rejects a message over 2000 characters outright, so a long batch has to be
# split rather than truncated — losing the tail of an alert list silently is worse than
# posting twice.
MAX_MESSAGE_CHARS = 1900


def webhook_url(env_name: str = WEBHOOK_ENV) -> str | None:
    """The configured webhook, or None when the feature is off."""
    return (os.getenv(env_name) or "").strip() or None


def post_message(url: str, content: str) -> bool:
    """POST one message. Returns True on success. NEVER raises.

    Every failure mode collapses to False on purpose: the caller's only sane response
    to any of them is identical — leave the marker unset and try again next sweep.
    """
    try:
        r = httpx.post(url, json={"content": content}, timeout=TIMEOUT_SECONDS)
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — see the docstring; this is the point
        log.warning("discord post failed: %s", exc)
        return False


def _webhook_sender(url: str):
    def send(content: str) -> bool:
        return post_message(url, content)

    return send


def _chunks(lines: list[str], header: str = "") -> list[str]:
    """Pack lines into messages under Discord's length cap, header on the first."""
    out: list[str] = []
    buf = header
    for line in lines:
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > MAX_MESSAGE_CHARS and buf:
            out.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


# ---- trades -----------------------------------------------------------------
def render_trade(row: dict) -> str:
    """One trade as a line, from `get_trades`' already-assembled display shape.

    Reuses that shape rather than re-deriving it (`kind`/`what` already handle players,
    picks and goalie-team clubs, and a pick's `what` is its human label). Re-deriving
    would give the channel a second, drifting account of what a trade says.
    """
    what = row.get("what") or "—"
    line = f"**{row.get('from')} → {row.get('to')}**: {what}"
    if row.get("gw"):
        line += f" (GW{row['gw']})"
    if row.get("conditional") and row.get("condition_note"):
        line += f"\n  ↳ _{row['condition_note']}_"
    return line


def announce_new_trades(db, league, send=None) -> dict:
    """Announce every trade with `announced_at IS NULL`, then stamp it.

    Stamps PER SUCCESS, not in a batch: a partial failure must leave the rows it
    couldn't post still queued, which is exactly what a per-row stamp gives.

    Announces trades from EVERY season, not just `league`'s. `league` is used only to
    render and to decide the feature is on — a pick trade is stored on the league row
    where it happened, and a cross-season deal announced nowhere is the bug this
    replaces.
    """
    from models import Trade

    url = webhook_url()
    if send is None:
        if not url:
            return {"sent": 0, "skipped": "not configured"}
        send = _webhook_sender(url)

    pending = (
        db.query(Trade)
        .filter(Trade.announced_at.is_(None))
        .order_by(Trade.created_at, Trade.id)
        .all()
    )
    if not pending:
        return {"sent": 0}

    import services

    # One pass over get_trades, indexed by id — it is cross-season already, and calling
    # it per trade would re-resolve every condition in the league for each row.
    rows = {
        r["id"]: r
        for season in services.get_trades(db)
        for r in season["trades"]
    }

    sent = 0
    for t in pending:
        row = rows.get(str(t.id))
        if row is None:
            # A trade get_trades can't render (an orphaned manager id, say). Stamping it
            # would silence it forever; leaving it queued retries a render that may well
            # succeed once the data is corrected.
            log.warning("trade %s not renderable, leaving unannounced", t.id)
            continue
        if not send(f"🔁 **Trade recorded**\n{render_trade(row)}"):
            # Stop on the first failure. The rest are almost certainly going to fail
            # too, and hammering a rate-limited or dead endpoint is how an IP earns a
            # Cloudflare ban.
            break
        t.announced_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
        sent += 1
    return {"sent": sent, "pending": len(pending) - sent}


# ---- commissioner alerts ------------------------------------------------------
def alert_fingerprint(entry: dict) -> str:
    """Content address for an alert. See DiscordAlert's docstring for the cadence this
    implies — it is a design choice, not an implementation detail."""
    raw = "|".join((
        str(entry.get("category") or ""),
        str(entry.get("manager") or ""),
        str(entry.get("detail") or ""),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def collect_alerts(db, league) -> list[dict]:
    """Everything worth waking the commissioner for: `flagged_actions` plus any FAILED
    `data_health` check, normalized to the same {category, manager, detail} shape."""
    import services

    out = list(services.flagged_actions(db, league))
    for check in services.data_health(db, league):
        if not check.get("ok"):
            out.append({
                "category": "Health check",
                "manager": None,
                "detail": f"{check['check']}: {check.get('detail') or 'failed'}",
            })
    return out


def announce_alerts(db, league, send=None) -> dict:
    """Push not-yet-sent commissioner alerts to the private alert channel.

    The dedupe row is written only AFTER a successful send, so a failed post is retried
    next sweep rather than silently swallowed — the same ordering
    `confirm_discovery_suggestion` uses for the same reason.
    """
    from models import DiscordAlert

    url = webhook_url(ALERT_WEBHOOK_ENV)
    if send is None:
        if not url:
            return {"sent": 0, "skipped": "not configured"}
        send = _webhook_sender(url)

    entries = collect_alerts(db, league)
    if not entries:
        return {"sent": 0}

    seen = {
        f for (f,) in db.query(DiscordAlert.fingerprint).filter(
            DiscordAlert.league_id == league.id
        )
    }
    fresh = []
    for e in entries:
        fp = alert_fingerprint(e)
        # Dedupe within this batch too: flagged_actions can legitimately emit the same
        # line twice (two checks noticing one situation), and the UNIQUE would abort
        # the whole transaction on the second insert.
        if fp in seen:
            continue
        seen.add(fp)
        fresh.append((fp, e))
    if not fresh:
        return {"sent": 0}

    lines = [
        f"• **{e['category']}**"
        + (f" — {e['manager']}" if e.get("manager") else "")
        + f": {e['detail']}"
        for _fp, e in fresh
    ]
    for message in _chunks(lines, header="⚠️ **Commissioner alerts**"):
        if not send(message):
            # Nothing is marked sent, so the whole batch is retried. Re-posting a line
            # that did get through is the lesser evil against losing one that didn't.
            return {"sent": 0, "failed": True}

    for fp, e in fresh:
        db.add(DiscordAlert(
            league_id=league.id, fingerprint=fp,
            summary=f"{e['category']}: {e['detail']}"[:500],
        ))
    db.commit()
    return {"sent": len(fresh)}


def run_outbound(db, league) -> dict:
    """Both sweeps, guarded. Called from the post-sync hook; never raises.

    The frozen-season skip lives HERE rather than at the call site so it is testable
    and travels with the feature: a `sync_locked` league's trades are history and its
    alerts are settled, so announcing either would be posting last season's news.
    """
    if league is None or getattr(league, "sync_locked", False):
        return {"skipped": "frozen" if league is not None else "no league"}
    out: dict = {}
    for name, fn in (("trades", announce_new_trades), ("alerts", announce_alerts)):
        try:
            out[name] = fn(db, league)
        except Exception as exc:  # noqa: BLE001
            # A bug in our own rendering must not fail the sync that carried it.
            log.warning("discord %s sweep failed: %s", name, exc)
            db.rollback()
            out[name] = {"error": str(exc)}
    return out
