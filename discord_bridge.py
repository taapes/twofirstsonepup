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
from sqlalchemy import BigInteger
from sqlalchemy import cast as sa_cast

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


def announce_gameweek_summary(db, league, send=None) -> dict:
    """Post the final projected scoreboard once a gameweek's fixtures are all done.

    Fires ONCE per gameweek, deduped through the existing `discord_alerts` fingerprint
    rather than a new marker column — the question ("have I already said this?") and the
    answer (a content hash) are identical to the alert sweep's, and a second mechanism
    for one message would be two things to keep in step.

    Gated on every PL fixture in the gameweek being finished, not on `Match.finished`:
    the latter is FPL's H2H scoring-lock and can lag by hours, and a summary posted
    while a match is in play would be wrong in the one way that matters.
    """
    import services
    from models import DiscordAlert

    url = webhook_url(ALERT_WEBHOOK_ENV)
    if send is None:
        if not url:
            return {"sent": 0, "skipped": "not configured"}
        send = _webhook_sender(url)

    gw = services.current_gameweek(db, league)
    if not gw:
        return {"sent": 0, "skipped": "no gameweek"}
    counts = (services.gw_fixture_progress(db, league, gw) or {}).get("counts") or {}
    if not counts.get("total") or counts.get("finished") != counts.get("total"):
        return {"sent": 0, "skipped": "gameweek still in play"}

    fingerprint = hashlib.sha256(
        f"gw-summary|{league.id}|{gw}".encode("utf-8")
    ).hexdigest()
    already = (
        db.query(DiscordAlert)
        .filter(DiscordAlert.league_id == league.id,
                DiscordAlert.fingerprint == fingerprint)
        .first()
    )
    if already:
        return {"sent": 0}

    board = services.get_scoreboard(db, league, gw)
    if not board.get("matches"):
        return {"sent": 0, "skipped": "no matches"}
    lines = []
    for m in board["matches"]:
        lines.append(f"**{m['home']} {m['home_score']} – {m['away_score']} {m['away']}**")
        if m.get("analysis"):
            lines.append(f"  _{m['analysis']}_")
    header = f"🏁 **GW{gw} final** _(bench substitutions applied)_"
    for message in _chunks(lines, header=header):
        if not send(message):
            return {"sent": 0, "failed": True}

    db.add(DiscordAlert(league_id=league.id, fingerprint=fingerprint,
                        summary=f"GW{gw} scoreboard summary"))
    db.commit()
    return {"sent": 1, "gameweek": gw}


def run_outbound(db, league) -> dict:
    """Both sweeps, guarded. Called from the post-sync hook; never raises.

    The frozen-season skip lives HERE rather than at the call site so it is testable
    and travels with the feature: a `sync_locked` league's trades are history and its
    alerts are settled, so announcing either would be posting last season's news.
    """
    if league is None or getattr(league, "sync_locked", False):
        return {"skipped": "frozen" if league is not None else "no league"}
    out: dict = {}
    for name, fn in (("trades", announce_new_trades), ("alerts", announce_alerts),
                     ("gw_summary", announce_gameweek_summary)):
        try:
            out[name] = fn(db, league)
        except Exception as exc:  # noqa: BLE001
            # A bug in our own rendering must not fail the sync that carried it.
            log.warning("discord %s sweep failed: %s", name, exc)
            db.rollback()
            out[name] = {"error": str(exc)}
    return out


# =============================================================================
# INBOUND: poll a channel, store raw, parse, resolve, stage for review.
#
# NOTHING BELOW EVER WRITES LEAGUE STATE. Every parsed announcement becomes a
# `DiscordIngest` row with status='pending' that a human confirms — the same rule, and
# for the same reason, as `discovery_match_suggestions`: a wrong player match moves a
# keeper clock and nothing downstream would flag it (models.py:679).
#
# The five real messages that shaped this settle that the human stays permanently, not
# just for a cautious v1:
#   * An IL post ("ekitike IL 1-4") names NO replacement, and `place_on_il` requires
#     one. The write is structurally incomplete however confident the parse.
#   * A trade post is often written by someone who is not a party to it, never says
#     whose pick a traded pick originally was, and uses two pick notations.
# So the goal is not to remove the human. It is to make confirming an announcement ONE
# CLICK instead of a form.
# =============================================================================

BOT_TOKEN_ENV = "DISCORD_BOT_TOKEN"
TRADE_CHANNEL_ENV = "DISCORD_TRADE_CHANNEL_ID"
IL_CHANNEL_ENV = "DISCORD_IL_CHANNEL_ID"

API_BASE = "https://discord.com/api/v10"
# Discord's own cap on this endpoint. Also the loop's "is there more?" signal.
FETCH_LIMIT = 100
# A cap on how many pages one sweep will pull, so a first run against years of history
# can't hold the sync open indefinitely. The cursor persists, so the next sweep resumes.
MAX_PAGES = 10


def bot_token() -> str | None:
    return (os.getenv(BOT_TOKEN_ENV) or "").strip() or None


class DiscordAuthError(RuntimeError):
    """A 401. Raised rather than returned because the caller must STOP, not retry.

    Discord counts 401/403/429 toward a Cloudflare ban at 10,000 per 10 minutes, so a
    bad or rotated token must disable the feature for this sweep instead of looping.
    """


def fetch_messages(channel_id: str, after: str | None = None, token: str | None = None):
    """Newest-first page of messages, oldest page first. Returns [] when unreadable.

    Two Discord behaviours drive the shape of this:

    * **`MESSAGE_CONTENT` gates REST, not just the gateway.** Without the privileged
      intent enabled, `content` comes back as an empty string and the request still
      succeeds — so an unconfigured app looks exactly like a quiet channel.
    * **A missing `READ_MESSAGE_HISTORY` permission returns an empty array, not a 403.**
      Same silent failure. `probe_channel` below exists to tell the two apart, because
      neither is distinguishable from "nothing new" at this level.

    The docs do not specify which end `after=` returns when more than `limit` messages
    are pending, so results are sorted client-side by snowflake and paged until a
    response comes back short.
    """
    token = token or bot_token()
    if not token or not channel_id:
        return []
    headers = {"Authorization": f"Bot {token}"}
    collected: list[dict] = []
    cursor = after
    for _page in range(MAX_PAGES):
        params = {"limit": FETCH_LIMIT}
        if cursor:
            params["after"] = cursor
        try:
            r = httpx.get(
                f"{API_BASE}/channels/{channel_id}/messages",
                headers=headers, params=params, timeout=TIMEOUT_SECONDS,
            )
            if r.status_code == 401:
                raise DiscordAuthError("discord rejected the bot token")
            r.raise_for_status()
            batch = r.json()
        except DiscordAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("discord fetch failed: %s", exc)
            break
        if not batch:
            break
        # Snowflakes are monotonic, so sorting by id is sorting by time.
        batch.sort(key=lambda m: int(m["id"]))
        collected.extend(batch)
        cursor = batch[-1]["id"]
        if len(batch) < FETCH_LIMIT:
            break
    return collected


def probe_channel(channel_id: str, token: str | None = None) -> dict:
    """Distinguish "quiet channel" from "we cannot actually read this one".

    Worth a dedicated call because BOTH misconfigurations — a missing
    `READ_MESSAGE_HISTORY` overwrite on a private channel, and a disabled
    `MESSAGE_CONTENT` intent — present as success with nothing useful in it. Surfaced
    on /admin/health so a silent bridge is diagnosable rather than mysterious.
    """
    token = token or bot_token()
    if not token:
        return {"ok": False, "detail": f"{BOT_TOKEN_ENV} is not set"}
    if not channel_id:
        return {"ok": False, "detail": "no channel id configured"}
    try:
        r = httpx.get(
            f"{API_BASE}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            params={"limit": 1}, timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"unreachable: {exc}"}
    if r.status_code == 401:
        return {"ok": False, "detail": "bot token rejected (401)"}
    if r.status_code == 403:
        return {"ok": False, "detail": "bot cannot view this channel (403)"}
    if r.status_code >= 400:
        return {"ok": False, "detail": f"HTTP {r.status_code}"}
    batch = r.json()
    if not batch:
        return {"ok": True, "detail": "readable, but empty — check Read Message "
                                      "History if you expect messages"}
    if batch[0].get("content") == "":
        return {"ok": False,
                "detail": "messages readable but content is blank — enable the "
                          "MESSAGE CONTENT intent in the Developer Portal"}
    return {"ok": True, "detail": "readable"}


# ---- resolving names to rows --------------------------------------------------
# Reuse, never rebuild: services._score_match already does exact/strong/close tiers
# with difflib and token sets, and it is the same matcher the discovery queue trusts.
# Rebuilding it here would give the league two matchers that disagree.

def resolve_manager(db, league, *, discord_user_id=None, name=None) -> dict:
    """Who is this? Returns {"manager": Manager|None, "method": str, "why": str}.

    Tried in strict order of certainty, and the ORDER is the safety property:

      1. `discord_user_id` — exact, no matching involved. This is why the mapping
         exists, and it covers the author of every IL post for free.
      2. Exact `Manager.display` (case-insensitive).
      3. INITIALS of display_name — "KT" is "Kevin T", which the league really writes.
         Only when UNAMBIGUOUS: this league has two managers whose initials start with
         K, so the ambiguity check is load-bearing rather than theoretical.

    There is deliberately NO fuzzy tier. A near-miss on a manager name hands a pick to
    the wrong person, and unlike a player name there are only ten candidates — if none
    of the three exact routes hit, asking is cheap and guessing is not.
    """
    from models import Manager

    rows = db.query(Manager).filter(Manager.league_id == league.id).all()
    if discord_user_id:
        for m in rows:
            if m.discord_user_id and m.discord_user_id == str(discord_user_id):
                return {"manager": m, "method": "discord_id", "why": ""}
    if not name:
        return {"manager": None, "method": None,
                "why": "unmapped Discord author — map them on /admin/health"}

    want = name.strip().lower()
    exact = [m for m in rows if (m.display or "").strip().lower() == want]
    if len(exact) == 1:
        return {"manager": exact[0], "method": "exact", "why": ""}

    def initials(display: str) -> str:
        return "".join(part[0] for part in (display or "").split() if part).lower()

    if 1 <= len(want) <= 3 and want.isalpha():
        hits = [m for m in rows if initials(m.display) == want]
        if len(hits) == 1:
            return {"manager": hits[0], "method": "initials", "why": ""}
        if len(hits) > 1:
            return {"manager": None, "method": None,
                    "why": f"{name!r} matches {len(hits)} managers "
                           f"({', '.join(sorted(h.display for h in hits))})"}
    return {"manager": None, "method": None, "why": f"no manager matching {name!r}"}


def resolve_player(db, name: str) -> dict:
    """Which player? Returns {"player": Player|None, "method": str, "score": float}.

    Delegates the scoring to services._score_match, so this agrees with the discovery
    queue by construction. Candidates are ranked and only reported when there is a
    single best one — two players tying is a question for a human, not a coin flip.
    """
    import services
    from models import Player

    best: list[tuple[float, str, object]] = []
    for p in db.query(Player).all():
        scored = services._score_match(name, p.full_name, p.name)
        if scored:
            best.append((scored[0], scored[1], p))
    if not best:
        return {"player": None, "method": None, "score": 0.0,
                "why": f"no player matching {name!r}"}
    best.sort(key=lambda t: -t[0])
    top = best[0]
    tied = [b for b in best if b[0] == top[0]]
    if len(tied) > 1:
        return {"player": None, "method": None, "score": top[0],
                "why": f"{name!r} matches {len(tied)} players equally "
                       f"({', '.join(sorted(str(t[2].name) for t in tied))})"}
    return {"player": top[2], "method": top[1], "score": top[0], "why": ""}


def suggest_il_replacement(
    db, league, manager, start_gw: int, position: str | None = None,
    exclude_fpl_id: int | None = None,
) -> list[dict]:
    """Who probably came in for the absentee — the field the announcement never has.

    Ranked, best first, each with the reason it is being offered:

      1. `added` — this manager gained this player at `start_gw`, derived the same way
         `get_transactions` derives add/drops (diff consecutive roster snapshots).
      2. `squad` — everyone else they hold. Not a guess about the swap, just the set of
         players it could legally have been.

    THE SECOND TIER IS NOT A NICETY. Every real IL post in the sample says "1-4", i.e.
    start_gw=1, and at GW1 there is no previous snapshot to diff — so a diff-only
    version returns nothing for exactly the messages that motivated the feature, while
    looking perfectly healthy in a test written at GW2. That is the silent-inert shape
    this repo keeps finding, and it is why this function must return SOMETHING useful
    whenever the manager holds anyone at all.

    `position` narrows to what `place_on_il` will actually accept (its same-position
    rule), turning a fifteen-item dropdown into a three-item one, and `exclude_fpl_id`
    drops the injured player himself — he is on his own squad, so without this the
    top suggestion is frequently "replace Saliba with Saliba", which place_on_il
    refuses outright.

    A suggestion only, never applied. FPL records no paired add/drop, so which arrival
    replaced which departure is genuinely unknowable from a diffed snapshot — the same
    reason docs/DESIGN_IL_OWNERSHIP.md refuses to derive the season-end release. Do not
    promote this to automatic.
    """
    from models import Gameweek, Roster

    def squad(gw_number: int) -> set:
        return {
            pid for (pid,) in db.query(Roster.player_id)
            .join(Gameweek, Gameweek.id == Roster.gameweek_id)
            .filter(Gameweek.league_id == league.id, Gameweek.number == gw_number,
                    Roster.manager_id == manager.id)
        }

    held = squad(start_gw)
    if not held:
        # No snapshot for that GW (a backfilled historical absence, or a post that
        # arrived before the first sync of the season). Fall back to the latest roster
        # we do have rather than returning nothing.
        latest = (
            db.query(Gameweek.number)
            .join(Roster, Roster.gameweek_id == Gameweek.id)
            .filter(Gameweek.league_id == league.id, Roster.manager_id == manager.id)
            .order_by(Gameweek.number.desc())
            .limit(1)
            .scalar()
        )
        held = squad(latest) if latest else set()
    added = (held - squad(start_gw - 1)) if start_gw > 1 else set()

    import services

    ident = services.season_identity(db, league, list(held))
    out = []
    for pid in held:
        row = ident.get(pid)
        if row is None:
            continue
        if position and row.position != position:
            continue
        if exclude_fpl_id is not None and row.fpl_id == exclude_fpl_id:
            continue
        out.append({
            "fpl_id": row.fpl_id, "name": row.name, "position": row.position,
            "reason": "added" if pid in added else "squad",
        })
    # Additions first, then alphabetical inside each tier so the list is stable.
    out.sort(key=lambda r: (r["reason"] != "added", (r["name"] or "").lower()))
    return out


# ---- the ingest pipeline ------------------------------------------------------
def _store_messages(db, league, channel_id: str, raw: list[dict]) -> list:
    """Persist raw messages, skipping ones already seen. Returns the NEW rows.

    Storing before parsing is what makes the pipeline replayable — a parser bug is
    fixed by re-running over these rows rather than re-fetching from Discord — and it
    means a message we cannot interpret is still visible, which already beats the
    status quo where an unrecorded announcement is simply lost.
    """
    import datetime as _dt

    from models import DiscordMessage

    known = {
        mid for (mid,) in db.query(DiscordMessage.discord_message_id)
        .filter(DiscordMessage.channel_id == str(channel_id))
    }
    fresh = []
    for m in raw:
        if m["id"] in known:
            continue
        known.add(m["id"])
        author = m.get("author") or {}
        posted = None
        if m.get("timestamp"):
            try:
                posted = _dt.datetime.fromisoformat(m["timestamp"])
            except ValueError:
                posted = None
        row = DiscordMessage(
            league_id=league.id, channel_id=str(channel_id),
            discord_message_id=m["id"],
            author_discord_id=str(author.get("id")) if author.get("id") else None,
            author_name=author.get("global_name") or author.get("username"),
            content=m.get("content") or "",
            posted_at=posted,
        )
        db.add(row)
        fresh.append(row)
    db.commit()
    return fresh


def _stage(db, league, msg, kind: str, dedupe_key: str, payload: dict,
           resolution: dict, confidence: float) -> None:
    """Upsert one proposal. Idempotent on (message, kind, dedupe_key).

    A row the commissioner already REJECTED is left exactly as it is — never revived,
    never re-proposed. That is the discovery-suggestion rule, and it is what stops a
    dismissed parse coming back on every sweep. An APPLIED row is likewise untouched.
    """
    from models import DiscordIngest

    existing = (
        db.query(DiscordIngest)
        .filter(DiscordIngest.discord_message_id == msg.id,
                DiscordIngest.kind == kind,
                DiscordIngest.dedupe_key == dedupe_key)
        .one_or_none()
    )
    if existing is not None:
        if existing.status != "pending":
            return
        existing.payload = payload
        existing.resolution = resolution
        existing.confidence = confidence
        return
    db.add(DiscordIngest(
        discord_message_id=msg.id, league_id=league.id, kind=kind,
        dedupe_key=dedupe_key, payload=payload, resolution=resolution,
        confidence=confidence, status="pending",
    ))


def _resolve_assets(db, assets: list[dict], giver) -> tuple[list, list, list]:
    """Parsed assets -> (player fpl ids, pick specs, unresolved notes).

    A pick spec is `record_trade`'s own `"{year}:{type}:{round}:{original owner}"`
    string, so nothing new is invented for the write path to understand.

    TWO assumptions are made here, and BOTH are reported as unresolved rather than
    applied silently, because either being wrong reassigns a different manager's slot:

      * the pick's ORIGINAL owner is the person giving it up (never stated in any
        sampled message, and false for any pick acquired in an earlier trade);
      * an overall pick number converts to a round by league size, which needs the
        draft order to be known for that season and is meaningless before it is set.
    """
    players, picks, unresolved = [], [], []
    for a in assets:
        if a["kind"] == "player":
            players.append(a)
        elif a["kind"] == "unresolved":
            unresolved.append({"text": a["text"], "why": a["why"]})
        elif a["kind"] == "pick":
            if a.get("season_year") is None:
                unresolved.append({"text": a["text"], "why": "no season named"})
                continue
            if a["notation"] == "overall":
                unresolved.append({
                    "text": a["text"],
                    "why": f"'Pick {a['number']}' is a position, not a round — "
                           f"confirm which round and whose pick it originally was",
                })
                continue
            for rnd in a["rounds"]:
                picks.append({
                    "spec": f"{a['season_year']}:{a['draft_type']}:{rnd}:"
                            f"{giver.display if giver else '?'}",
                    "text": a["text"],
                    "assumed_owner": giver.display if giver else None,
                })
    return players, picks, unresolved


def ingest_message(db, league, msg) -> str:
    """Parse and stage one stored message. Returns its new `parse_status`.

    Never writes league state and never raises past its own bookkeeping.
    """
    import discord_parse

    text = msg.content or ""
    author = resolve_manager(db, league, discord_user_id=msg.author_discord_id)

    trade = discord_parse.parse_trade(text)
    if trade is not None:
        # The AUTHOR is only a hint here, not an identity: a real sampled message has
        # John announcing a trade between two other managers. Both sides are resolved
        # by name, and neither falls back to the poster.
        a = resolve_manager(db, league, name=trade["a"])
        b = resolve_manager(db, league, name=trade["b"])
        a_players, a_picks, a_un = _resolve_assets(db, trade["a_assets"], a["manager"])
        b_players, b_picks, b_un = _resolve_assets(db, trade["b_assets"], b["manager"])

        resolved_players: dict = {}
        unresolved = list(a_un) + list(b_un)
        for side, group in (("a", a_players), ("b", b_players)):
            out = []
            for item in group:
                hit = resolve_player(db, item["name"])
                if hit["player"] is None or hit["player"].fpl_id is None:
                    unresolved.append({
                        "text": item["text"],
                        "why": hit.get("why") or "player has no current FPL id",
                    })
                    continue
                out.append({"fpl_id": hit["player"].fpl_id, "name": hit["player"].name,
                            "method": hit["method"], "score": hit["score"],
                            "typed": item["name"]})
            resolved_players[side] = out

        payload = {
            "a_fpl": a["manager"].fpl_manager_id if a["manager"] else None,
            "b_fpl": b["manager"].fpl_manager_id if b["manager"] else None,
            "a_players": [p["fpl_id"] for p in resolved_players["a"]],
            "b_players": [p["fpl_id"] for p in resolved_players["b"]],
            "a_picks": [p["spec"] for p in a_picks],
            "b_picks": [p["spec"] for p in b_picks],
        }
        resolution = {
            "a": {"typed": trade["a"], "method": a["method"],
                  "display": a["manager"].display if a["manager"] else None,
                  "why": a["why"]},
            "b": {"typed": trade["b"], "method": b["method"],
                  "display": b["manager"].display if b["manager"] else None,
                  "why": b["why"]},
            "players": resolved_players,
            "picks": {"a": a_picks, "b": b_picks},
            "unresolved": unresolved,
            "author": {"mapped": author["method"] == "discord_id",
                       "name": msg.author_name},
        }
        complete = bool(a["manager"] and b["manager"] and not unresolved)
        _stage(db, league, msg, "trade", "", payload, resolution,
               1.0 if complete else 0.5)
        return "staged"

    il = discord_parse.parse_il(text)
    if il is not None:
        # Here the author IS the manager: IL posts are self-reports, and every sampled
        # one was written by the manager whose player it is.
        hit = resolve_player(db, il["player"])
        manager = author["manager"]
        payload = {
            "fpl_manager_id": manager.fpl_manager_id if manager else None,
            "injured_fpl_id": hit["player"].fpl_id if hit["player"] else None,
            "start_gw": il["start_gw"],
            # ABSENT ON PURPOSE. The announcement does not contain it; `place_on_il`
            # requires it; a human supplies it from the suggestions below.
            "replacement_fpl_id": None,
        }
        unresolved = []
        if manager is None:
            unresolved.append({"text": msg.author_name or "?", "why": author["why"]})
        if hit["player"] is None:
            unresolved.append({"text": il["player"], "why": hit["why"]})
        resolution = {
            "player": {"typed": il["player"], "method": hit["method"],
                       "score": hit["score"],
                       "name": hit["player"].name if hit["player"] else None},
            "manager": {"method": author["method"],
                        "display": manager.display if manager else None},
            "end_gw": il["end_gw"],
            # Narrowed to the injured player's position, which is what place_on_il
            # will actually accept.
            "replacement_suggestions": (
                suggest_il_replacement(
                    db, league, manager, il["start_gw"],
                    position=hit["player"].position if hit["player"] else None,
                    exclude_fpl_id=hit["player"].fpl_id if hit["player"] else None,
                )
                if manager else []
            ),
            "needs": ["replacement_fpl_id"],
            "unresolved": unresolved,
        }
        _stage(db, league, msg, "il_place", "", payload, resolution, 0.5)
        return "staged"

    return "ignored"


def poll_channel(db, league, channel_id: str, token: str | None = None) -> dict:
    """Fetch, store and stage one channel. Never raises."""
    from models import DiscordMessage

    if not channel_id:
        return {"skipped": "no channel"}
    cursor = (
        db.query(DiscordMessage.discord_message_id)
        .filter(DiscordMessage.channel_id == str(channel_id))
        # Snowflakes are monotonic but STRINGS here, so ordering has to be numeric or
        # "10" sorts above "9" and the cursor walks backwards.
        .order_by(sa_cast(DiscordMessage.discord_message_id, BigInteger).desc())
        .limit(1)
        .scalar()
    )
    try:
        raw = fetch_messages(channel_id, after=cursor, token=token)
    except DiscordAuthError as exc:
        return {"error": str(exc), "disabled": True}
    if not raw:
        return {"fetched": 0}

    fresh = _store_messages(db, league, channel_id, raw)
    staged = 0
    for msg in fresh:
        try:
            msg.parse_status = ingest_message(db, league, msg)
        except Exception as exc:  # noqa: BLE001
            # One unparseable message must not stop the sweep or lose the rest.
            log.warning("discord ingest failed for %s: %s", msg.discord_message_id, exc)
            msg.parse_status = "failed"
        staged += msg.parse_status == "staged"
    db.commit()
    return {"fetched": len(raw), "new": len(fresh), "staged": staged}


def run_inbound(db, league) -> dict:
    """Poll every configured channel. Called from the post-sync hook; never raises."""
    if league is None or getattr(league, "sync_locked", False):
        return {"skipped": "frozen" if league is not None else "no league"}
    if not bot_token():
        return {"skipped": "not configured"}
    out: dict = {}
    for label, env in (("trades", TRADE_CHANNEL_ENV), ("il", IL_CHANNEL_ENV)):
        channel = (os.getenv(env) or "").strip()
        if not channel:
            continue
        try:
            out[label] = poll_channel(db, league, channel)
        except Exception as exc:  # noqa: BLE001
            log.warning("discord poll failed for %s: %s", label, exc)
            db.rollback()
            out[label] = {"error": str(exc)}
    return out
