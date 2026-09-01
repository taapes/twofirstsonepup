"""AI-generated league content — currently the gameweek review.

WHY A SEPARATE MODULE
---------------------
The second outbound network call in this codebase, and the first PAID one. It follows
the discipline `discord_bridge.py` established, for the same reasons:

  * Nothing here is called from a request handler. The only caller is the post-sync hook
    in `sync.run_sync`. A page view reads the STORED row and never generates.
  * `generate` NEVER raises. A failed or refused generation must leave the sync that
    carried it untouched — the sync is the real work.
  * The timeout is bounded. A review can tolerate ~60s (unlike a webhook ping), but it
    must not be able to hang the sync indefinitely.
  * The feature is OFF when ANTHROPIC_API_KEY is unset — a logged no-op. A fresh
    checkout, the test suite and the demo sandbox all cost nothing by default.

NOTHING HERE EVER POSTS TO DISCORD. Generation and sending are separate on purpose: a
model cannot tell when a joke lands badly on a particular person in a particular week,
and a chat message cannot be unsent. `status` becomes 'posted' only through the admin
route in ui.py. Two independent places, because this is the property most worth not
losing to a later refactor.

THE INJECTABLE SEAM
-------------------
`ensure_review` takes a `generate=` callable so tests never mock SDK internals — the same
shape as `discord_bridge`'s `send=`. No test in this repo touches the network.
"""

import logging
import os

from rules import (
    AI_MIN_REGENERATE_SECONDS,
    AI_REVIEW_KIND,
    MAX_AI_CALLS_PER_GW,
    RuleViolation,
)

log = logging.getLogger(__name__)

API_KEY_ENV = "ANTHROPIC_API_KEY"

# Opus 5. Deliberately not downgraded for cost: a gameweek review is ~2,500 input and
# ~500 output tokens, which at $5/$25 per M is about four cents a gameweek — under $2 a
# season including regenerations. Tone is the entire deliverable here and the cheaper
# models are worse at it, so saving three cents a week would be a bad trade.
MODEL = "claude-opus-5"

# Generous on purpose. The failure mode of a low cap is a review truncated mid-joke,
# which is worse than a long one.
MAX_TOKENS = 4000

# A review can take its time — this is a background sync task, not a page render. But it
# must not be able to hang the sync forever.
TIMEOUT_SECONDS = 90.0


# ---------------------------------------------------------------------------
# THE PERSONA. This is the whole deliverable — edit it here and nowhere else.
#
# The register is AFFECTIONATE. Everything else is a modifier on that.
#
# Two failure modes, and they pull in opposite directions:
#   * too soft -> a bland recap nobody reads. Recoverable by tuning.
#   * too harsh -> a line that makes someone quietly not want to be in the league.
#     NOT recoverable. This is why Discord posting is gated behind a human.
# When in doubt, err soft: the commissioner can ask for sharper, but he can't unsend.
#
# Per-manager context (e.g. "John is the favourite and insufferable — never flatter
# him") lives in the `manager_notes` table, editable on /admin/corrections without a
# deploy, because league in-jokes move faster than releases.
# ---------------------------------------------------------------------------
PERSONA = """You write the weekly gameweek review for a ten-manager fantasy Premier \
League draft keeper league. The managers are real friends who have played together for \
years and all read this.

Your job is to be FUNNY AND ENTERTAINING FIRST, informative second. The scoreboard is \
already on the page above you — nobody needs you to repeat it. They need you to make \
them laugh about it.

Voice:
- A mate in the group chat who watches every match, not a pundit paid to be \
controversial.
- Affectionate ribbing. Take the piss, warmly.
- Easy laughs are good laughs. An obvious joke that lands beats a clever one that \
strains. Don't reach.
- Being a bit mean about a THING that happened is fine and often the funniest option — \
a shocking score, someone who benched the week's top scorer, four blanks in one XI, a \
defence that conceded everything. The joke is the situation.
- Do NOT be mean about a PERSON. Never write a line that would sting to read about \
yourself the next day. Never pile on someone who had a genuinely miserable week — that \
week writes its own joke, and kicking someone who is already down is not funny, it's \
just unkind.
- No sarcasm so dry it reads as contempt. No nicknames you invented. No moralising.

Length: two or three short paragraphs. Stop while it's still funny.

ABOUT THE NUMBERS: every figure you need is given to you below. Use them exactly as \
provided. Do NOT calculate, total, average, rank or infer any number that is not \
written down for you — if it isn't in the data, don't say it. Getting a score wrong in \
front of the people who played the match is the one unrecoverable mistake here."""


def api_key() -> str | None:
    """The configured key, or None when the feature is off."""
    return (os.getenv(API_KEY_ENV) or "").strip() or None


# ---- prompt building --------------------------------------------------------
def _manager_notes(db) -> dict:
    """person -> standing note. Keyed on the PERSON, so it survives the rollover."""
    from models import ManagerNote

    return {
        n.person: n.note for n in db.query(ManagerNote) if (n.note or "").strip()
    }


def _squad_lines(db, league, gw_number) -> dict:
    """manager display -> per-player lines, so the model can find its own angles.

    This is the "let it pick what's notable" half of the design, and the honest cost is
    that a model handed raw rows CAN state one wrongly. Mitigated by giving every number
    explicitly and by the persona's instruction never to compute one — not by withholding
    the data, which would just make the review boring.
    """
    import services
    from models import Gameweek, GameweekPoints, Manager

    gw = (
        db.query(Gameweek)
        .filter_by(league_id=league.id, number=gw_number)
        .one_or_none()
    )
    if gw is None:
        return {}
    season = services._season_by_fpl_id(db, league)
    names = {m.id: m.display for m in db.query(Manager).filter_by(league_id=league.id)}
    out: dict = {}
    for gp in db.query(GameweekPoints).filter_by(gameweek_id=gw.id):
        who = names.get(gp.manager_id)
        if not who:
            continue
        lines = []
        for e in gp.player_points or []:
            ps = season.get(e.get("fpl_id"))
            if ps is None:
                continue
            lines.append(
                f"    {ps.name} ({ps.position}, {ps.current_team}): "
                f"{e.get('points') or 0} pts, {e.get('minutes') or 0} min"
                + ("" if e.get("is_starting") else ", benched")
            )
        out[who] = lines
    return out


def build_prompt(db, league, gw_number: int) -> str:
    """Everything the model is allowed to know, as plain text.

    Built only from already-computed reads — `get_scoreboard` (whose scores are
    PROJECTED, with auto-subs applied), `get_standings`, and the stored per-pick rows.
    No new query work, per the epic's rule.

    The per-match `analysis` sentence is included as the AUTHORITATIVE result. It is
    generated deterministically by `services.matchup_analysis`, so the model has a
    correct sentence to paraphrase rather than a scoreline to interpret.
    """
    import services

    board = services.get_scoreboard(db, league, gw_number)
    standings = services.get_standings(db, league)
    squads = _squad_lines(db, league, gw_number)
    notes = _manager_notes(db)

    parts = [f"GAMEWEEK {gw_number} — final results", ""]
    for m in board.get("matches") or []:
        parts.append(
            f"  {m['home']} {m['home_score']} – {m['away_score']} {m['away']}"
        )
        if m.get("analysis"):
            parts.append(f"    result: {m['analysis']}")
        for side, label in (("home_subs", m["home"]), ("away_subs", m["away"])):
            for s in m.get(side) or []:
                parts.append(
                    f"    {label}: {s['out']['name']} didn't play, "
                    f"{s['in']['name']} came off the bench for {s['in']['points']} pts"
                )

    parts += ["", "STANDINGS after this gameweek", ""]
    for row in standings:
        parts.append(
            f"  {row['rank']}. {row['manager']} — {row['total']} pts "
            f"({row['matches_won']}W {row['matches_drawn']}D {row['matches_lost']}L, "
            f"{row['points_for']} for)"
        )

    if squads:
        parts += ["", "EVERY SQUAD, PLAYER BY PLAYER", ""]
        for who in sorted(squads):
            parts.append(f"  {who}:")
            parts.extend(squads[who])

    if notes:
        parts += ["", "WHAT YOU KNOW ABOUT THESE PEOPLE", ""]
        for who in sorted(notes):
            parts.append(f"  {who}: {notes[who]}")

    return "\n".join(parts)


# ---- the API call ----------------------------------------------------------
_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "A short, funny title for the week. No more than 10 words.",
        },
        "body": {
            "type": "string",
            "description": "Two or three short paragraphs, separated by blank lines.",
        },
    },
    "required": ["headline", "body"],
    "additionalProperties": False,
}


def call_model(prompt: str, *, key: str | None = None) -> dict | None:
    """One Messages API call. Returns {"headline","body","model"} or None. NEVER raises.

    Every failure collapses to None for the same reason `discord_bridge.post_message`
    collapses to False: the caller's only sane response to a timeout, a refusal, a bad
    key or a malformed response is identical — record the failure and let the next sweep
    or a manual regenerate try again.

    `output_config.format` shapes the response instead of an assistant prefill, which
    returns a 400 on this model family. `budget_tokens` is likewise removed — thinking is
    adaptive and on by default.
    """
    key = key or api_key()
    if not key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=PERSONA,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            # Deliberately attacking-adjacent copy about named people is more likely
            # than average to trip a safety classifier. A refusal without a fallback is
            # simply no review that week.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "category", None)
            log.warning("ai review refused (%s)", detail)
            raise RuleViolation(f"the model declined to write this one ({detail})")
        text = "".join(b.text for b in response.content if b.type == "text")
        import json

        data = json.loads(text)
        return {
            "headline": (data.get("headline") or "").strip() or None,
            "body": (data.get("body") or "").strip() or None,
            "model": response.model,
        }
    except RuleViolation:
        raise
    except Exception as exc:  # noqa: BLE001 — see the docstring; this is the point
        log.warning("ai review generation failed: %s", exc)
        return None


# ---- orchestration ---------------------------------------------------------
def _gameweek_over(db, league, gw_number: int) -> bool:
    """Has every PL fixture in this gameweek finished?

    Gated on real fixture progress, NOT `services.gw_finished`. That function is "ANY
    finished H2H match", and `Match.finished` is FPL's scoring-lock, which lags by hours.
    The Discord gameweek summary uses this same test, so the two features can never
    disagree about when a gameweek ended.

    `total == 0` is an unsynced gameweek, not a finished one — the same
    missing-data-isn't-emptiness guard used throughout.
    """
    import services

    counts = (services.gw_fixture_progress(db, league, gw_number) or {}).get("counts") or {}
    total = counts.get("total") or 0
    return bool(total) and counts.get("finished", 0) == total


def existing_review(db, league, gw_number: int):
    """The stored row for this gameweek, or None. What page views read."""
    from models import AiGeneratedContent, Gameweek

    gw = (
        db.query(Gameweek)
        .filter_by(league_id=league.id, number=gw_number)
        .one_or_none()
    )
    if gw is None:
        return None
    return (
        db.query(AiGeneratedContent)
        .filter_by(league_id=league.id, gameweek_id=gw.id, kind=AI_REVIEW_KIND)
        .one_or_none()
    )


def ensure_review(db, league, gw_number: int, *, force: bool = False, generate=None) -> dict:
    """Generate the gameweek review if it's due. Returns a small status dict.

    Called two ways: automatically from the post-sync hook (force=False, so an existing
    row short-circuits and the cost is paid exactly once), and from the admin regenerate
    button (force=True, which always calls and always UPDATES in place).

    NEVER posts anywhere. The row lands at status='ready' and a human sends it.

    Raises RuleViolation only on the manual path's guards (too soon, cap hit) so the
    route can show the reason; the automatic path swallows everything.
    """
    import datetime as _dt

    from models import AiGeneratedContent, Gameweek

    generate = generate or call_model
    if generate is call_model and not api_key():
        log.info("ai review skipped: %s is not set", API_KEY_ENV)
        return {"skipped": "not configured"}

    gw = (
        db.query(Gameweek)
        .filter_by(league_id=league.id, number=gw_number)
        .one_or_none()
    )
    if gw is None:
        return {"skipped": "no gameweek"}
    if not _gameweek_over(db, league, gw_number):
        return {"skipped": "gameweek not finished"}

    row = (
        db.query(AiGeneratedContent)
        .filter_by(league_id=league.id, gameweek_id=gw.id, kind=AI_REVIEW_KIND)
        .one_or_none()
    )
    if row is not None and not force:
        # The idempotency that makes the automatic path cost one call per gameweek. A
        # 'failed' row still counts: the next MANUAL attempt can retry, but the nightly
        # sync must not keep paying for a gameweek the model won't write.
        return {"skipped": "already generated", "status": row.status}

    if row is not None and force:
        if (row.attempts or 0) >= MAX_AI_CALLS_PER_GW:
            raise RuleViolation(
                f"this gameweek has already had {row.attempts} generation attempts "
                f"(cap {MAX_AI_CALLS_PER_GW}) — the cap exists so a stuck key can't "
                "run up a bill"
            )
        age = _dt.datetime.now(_dt.timezone.utc) - row.generated_at
        if age.total_seconds() < AI_MIN_REGENERATE_SECONDS:
            wait = int(AI_MIN_REGENERATE_SECONDS - age.total_seconds())
            raise RuleViolation(f"just regenerated — try again in {wait}s")

    prompt = build_prompt(db, league, gw_number)
    try:
        result = generate(prompt)
    except RuleViolation as exc:
        result, failure = None, str(exc)
    else:
        failure = None if result else "generation failed or timed out"

    if row is None:
        row = AiGeneratedContent(
            league_id=league.id, gameweek_id=gw.id, kind=AI_REVIEW_KIND, attempts=0
        )
        db.add(row)
    row.attempts = (row.attempts or 0) + 1
    row.generated_at = _dt.datetime.now(_dt.timezone.utc)

    if result:
        row.headline = result.get("headline")
        row.content = result.get("body")
        row.model = result.get("model")
        row.error = None
        # Back to 'ready' on a regenerate, so a previously posted or discarded review
        # returns to the queue for a fresh decision rather than staying sent.
        row.status = "ready"
    else:
        row.error = failure
        row.status = "failed"
    db.commit()
    return {"generated": bool(result), "status": row.status, "gameweek": gw_number}


def run_after_sync(db, league) -> dict:
    """The post-sync entry point. Never raises, never posts.

    The frozen-season and feature-off guards live here rather than at the call site so
    they travel with the feature and are testable — the same reason
    `discord_bridge.run_outbound` holds its own.
    """
    import services

    if league is None or getattr(league, "sync_locked", False):
        return {"skipped": "frozen" if league is not None else "no league"}
    gw = services.current_gameweek(db, league)
    if not gw:
        return {"skipped": "no gameweek"}
    try:
        return ensure_review(db, league, gw)
    except Exception as exc:  # noqa: BLE001
        # A bug in our own prompt building must not fail the sync that carried it.
        log.warning("ai review sweep failed: %s", exc)
        db.rollback()
        return {"error": str(exc)}
