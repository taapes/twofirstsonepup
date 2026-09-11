"""Parsing league announcements out of Discord message text.

PURE. No database, no network, no ORM — it turns a string into a description of what
was announced, using only the vocabulary of the message itself (names as typed, rounds
as written). Resolving those names to rows is `discord_bridge`'s job, and keeping the
two apart is what makes this file table-testable against real message text.

DETERMINISTIC, and deliberately no LLM. The fuzzy half of this problem is not finding
the fields, it is deciding which human "Cunha" is — and `services._score_match` already
does that with difflib and token sets, purely and testably. Once those are separated,
finding the fields in the league's actual `#trades` conventions is a regex.

WHAT THE REAL MESSAGES LOOK LIKE. This is the requirements list, and it is now drawn
from the WHOLE channel (100 messages, swept 2026-09-07) rather than the five-message
sample it started as — that sweep is what turned up forms C and D, which the original
list asserted did not exist.

    A: pivot on "to X for"          │  B: two labelled blocks
    🚨🚨 TRADE ALERT 🚨🚨           │  🚨 TRADE ALERT 🚨
    John trades                     │  KT Trades:
    2026 Pick 6                     │  Cunha
    2026 discovery 2nd              │  Pick 12
    2028 discovery 2nd              │
                                    │  KS Trades:
    to Michael for                  │  6-9 Discoveries
    2026 pick 4                     │

    C: assets on the manager's line │  D: receiver first, "gets ... for ..."
    TRADE ALERT                     │  TRADE ALERT: GABY GETS KEVIN ROUND 4
    KEVIN T TRADES ROUND 3 PICK 10  │  PICK 10 FOR 2027 DISCOVERY, 2ND ROUND
    STEVE TRADES ROUND 5 PICK 5     │

    ekitike IL 1-4 (prob longer and indefinitely)
    Minteh. (1-4) probably longer
    Saliba IL 1-4 probably longer

Every form is PICK-heavy, and that is why the misses mattered: a pick trade appears in
no FPL feed, so a trade post this file cannot read is a draft board that will be wrong
years later, with nothing else to catch it.

Six facts drive everything below:

1. The 🚨 TRADE ALERT header is on every trade post, so classification is free. It is
   NOT absent from everything else, as this list used to claim — one sampled message
   complains *about* the "Trade Alerts channel" and matches. It is ignored only because
   it parses to no form, so the form matchers are the real filter and must stay strict.
2. An IL post names NO replacement player. `place_on_il` requires one, so an IL parse
   is ALWAYS incomplete — that is a property of the announcement, not of this parser,
   and no amount of cleverness fixes it.
3. Pick notation is genuinely two conventions: `Pick N` is the overall pick number,
   a bare ordinal (`6th`) is the round. Confirmed with the commissioner rather than
   guessed, because `pick_round` and `pick_number` are different things in the schema
   and reading one as the other silently reassigns a different manager's slot.
4. Nothing says whose pick a traded pick ORIGINALLY was.
5. Some assets are unparseable (`2026 4th 1st`, `6-9 Discoveries`, `ROUND 3 PICK 10`).
   Those are reported as UNRESOLVED rather than dropped or guessed, so the review UI
   can ask. `ROUND 3 PICK 10` is the instructive one: it USED to return "overall pick
   10", a confident wrong answer for the 10th pick of round 3.
6. A mention (`<@1362...>`) is the best identifier any message carries, so it is
   captured and handed to `resolve_manager`'s exact tier — not merely stripped.
"""

import re

# The classifier. Present on every trade post in the sample, absent from everything
# else, and cheap — an emoji and two words beat any heuristic over the body.
TRADE_HEADER = re.compile(r"trade\s*alert", re.IGNORECASE)

# "John trades ... to Michael for ..." — the pivot is the `to X for` line.
_FORM_A = re.compile(
    r"^\s*(?P<a>.+?)\s+trades?\s*:?\s*$(?P<a_assets>.*?)"
    r"^\s*to\s+(?P<b>.+?)\s+for\s*:?\s*$(?P<b_assets>.*)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
# "KT Trades: ... KS Trades: ..." — two labelled blocks.
_FORM_B_HEADER = re.compile(r"^\s*(?P<who>.+?)\s+trades\s*:\s*$", re.IGNORECASE | re.MULTILINE)

# "KEVIN T TRADES ROUND 3 PICK 10" — the assets are ON the manager's line, one line
# each. Distinguished from form B purely by that trailing content: the `\s+trades?\s+`
# plus a non-space asset group cannot match "John trades", "John trades:" or
# "KT Trades:", which is why trying A and B first leaves every existing parse alone.
_FORM_C = re.compile(
    r"^\s*(?P<who>[^\n:]+?)\s+trades?\s+(?P<assets>\S[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
# "GABY GETS KEVIN ROUND 4 PICK 10 FOR 2027 DISCOVERY, 2ND ROUND" — the RECEIVER is
# named first, inverted from every other form, and the giver's name is buried at the
# head of the asset text.
_FORM_D = re.compile(
    r"^\s*(?P<recv>[^\n]+?)\s+gets\s+(?P<given>[^\n]+?)\s+for\s+(?P<returned>[^\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# A Discord mention. The USER form carries the single highest-certainty identifier in
# any message — `resolve_manager`'s first tier and the whole reason
# `managers.discord_user_id` exists — so it is CAPTURED, not just stripped. Role
# (`<@&123>`) and channel (`<#123>`) mentions are stripped but never read as a person.
_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ANY_MENTION = re.compile(r"<[@#][!&]?\d+>|@everyone\b|@here\b", re.IGNORECASE)

# "ekitike IL 1-4", "Minteh. (1-4)", "Saliba IL 1-4 probably longer".
# The name is everything before an optional "IL" and the gameweek range; the trailing
# prose ("probably longer", "prob longer and indefinitely") is noise and is discarded.
_IL = re.compile(
    r"^\s*(?P<player>[^()\d]+?)\s*\.?\s*(?:\bIL\b)?\s*\(?\s*"
    r"(?P<start>\d{1,2})\s*-\s*(?P<end>\d{1,2})\s*\)?",
    re.IGNORECASE,
)

_ORDINAL = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)$", re.IGNORECASE)
_WORD_ORDINAL = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_YEAR = re.compile(r"\b(20\d{2})\b")
_PICK_N = re.compile(r"\bpicks?\s*#?\s*(\d{1,3})\b", re.IGNORECASE)
# "ROUND 3" — a round named by DIGIT, which is what makes "ROUND 3 PICK 10" a position
# within a round rather than an overall pick number. Deliberately requires the digit to
# FOLLOW the word, so "1st round discovery" and "5th round pick" are unaffected.
_ROUND_N = re.compile(r"\bround\s*#?\s*(\d{1,2})\b", re.IGNORECASE)
# `discovers`/`Discover` included: a real message typos "Tucker's 2027 Discover 2nd",
# and without the optional s/absent suffix it silently reads as a MAIN-draft pick —
# the wrong pick moved, with nothing downstream to catch it.
_DISCOVERY = re.compile(r"\bdiscover(?:y|ies|s)?\b", re.IGNORECASE)

# Lines that are decoration, not assets.
_NOISE = re.compile(r"^[\s\W]*$|^\s*trade\s*alert\s*$", re.IGNORECASE)


def _clean_name(raw: str) -> str:
    """A manager name as written, with the post's decoration stripped off.

    Form A's opening capture runs from the top of the message, so it drags the
    "🚨🚨 TRADE ALERT 🚨🚨" banner along with it. The name is the LAST line of that
    capture, minus any leading/trailing emoji or punctuation — which also handles the
    blank line the league usually leaves under the banner.
    """
    line = [x for x in (raw or "").splitlines() if x.strip()]
    line = line[-1] if line else ""
    # Mentions go FIRST. The punctuation strip below only touches the ends, so
    # "<@1362557356201738515> Kevin S" would keep its interior ">" and become
    # "1362557356201738515> Kevin S" — a name that matches nobody.
    line = _ANY_MENTION.sub(" ", line)
    return re.sub(r"^[\W_]+|[\W_]+$", "", line, flags=re.UNICODE).strip()


def _mention_id(raw: str) -> str | None:
    """The first USER mention's id in a capture, if any."""
    m = _USER_MENTION.search(raw or "")
    return m.group(1) if m else None


def is_trade_post(text: str) -> bool:
    return bool(TRADE_HEADER.search(text or ""))


def _clean_lines(block: str) -> list[str]:
    out = []
    for raw in (block or "").splitlines():
        line = raw.strip().strip("-•*").strip()
        if not line or _NOISE.match(line):
            continue
        out.append(line)
    return out


def _ordinals_in(text: str) -> list[int]:
    """Every bare ordinal in a line: "7th, 8th, and 9th" -> [7, 8, 9]."""
    found = []
    for tok in re.split(r"[,\s]+|\band\b", text):
        tok = tok.strip().strip(".")
        if not tok:
            continue
        m = _ORDINAL.match(tok)
        if m:
            found.append(int(m.group(1)))
        elif tok.lower() in _WORD_ORDINAL:
            found.append(_WORD_ORDINAL[tok.lower()])
    return found


def parse_asset(line: str) -> dict:
    """One asset line -> a description of what it is.

    Returns `{"kind": "player"|"pick"|"unresolved", ...}`. Never guesses: a line using
    both notations at once (`2026 4th 1st`) or a range (`6-9 Discoveries`) comes back
    `unresolved` with the raw text, because the two readings assign different managers'
    slots and picking one silently would be unrecoverable.
    """
    raw = line.strip()
    year = _YEAR.search(raw)
    draft_type = "discovery" if _DISCOVERY.search(raw) else "main"
    body = _YEAR.sub(" ", raw)
    body = _DISCOVERY.sub(" ", body)

    # A range of picks ("6-9") — a real message says this and we cannot tell whether it
    # means picks 6..9, rounds 6..9, or something the league says in conversation.
    if re.search(r"\d\s*-\s*\d", body):
        return {"kind": "unresolved", "text": raw, "why": "a pick range needs spelling out"}

    pick_n = _PICK_N.search(body)
    rest = _PICK_N.sub(" ", body)
    ordinals = _ordinals_in(rest)

    if pick_n and _ROUND_N.search(body):
        # "ROUND 3 PICK 10" is the 10th pick OF ROUND 3 — a positional slot belonging to
        # whoever picks 10th that round, NOT overall pick 10. Naming that manager needs
        # the draft order, which a pure parser does not have and `_resolve_assets` has no
        # path to; reading it as an overall number silently reassigns someone else's
        # slot, which is fact #3 in this module's docstring.
        return {"kind": "unresolved", "text": raw,
                "why": "names a position within a round — needs the draft order to "
                       "say whose pick it is"}
    if pick_n and ordinals:
        # "2026 4th 1st" and friends: two notations in one line, and they disagree.
        return {"kind": "unresolved", "text": raw,
                "why": "mixes 'Pick N' and an ordinal — which is it?"}
    if len(ordinals) > 1 and not re.search(r",|\band\b", rest, re.IGNORECASE):
        # A real message says "2026 4th 1st", and it is genuinely unclear: a LIST of
        # rounds is written with separators ("7th, 8th, and 9th"), so two ordinals
        # merely juxtaposed are more likely "the 4th pick of the 1st round" — a
        # different thing entirely, and one that would reassign someone else's slot.
        return {"kind": "unresolved", "text": raw,
                "why": "two ordinals with no separator — a list, or a pick within a round?"}
    if pick_n:
        return {"kind": "pick", "notation": "overall", "number": int(pick_n.group(1)),
                "season_year": int(year.group(1)) if year else None,
                "draft_type": draft_type, "text": raw}
    if ordinals:
        return {"kind": "pick", "notation": "round", "rounds": ordinals,
                "season_year": int(year.group(1)) if year else None,
                "draft_type": draft_type, "text": raw}
    if year and not body.strip():
        return {"kind": "unresolved", "text": raw, "why": "a year with no pick"}

    # Anything left is a player name as typed.
    name = body.strip(" .,:;")
    if not name:
        return {"kind": "unresolved", "text": raw, "why": "empty"}
    return {"kind": "player", "name": name, "text": raw}


# Words that mean the token is part of an ASSET, not a manager's name.
_ASSET_WORD = re.compile(
    r"^(?:picks?|rounds?|discover(?:y|ies|s)?|20\d{2}|\d{1,2}(?:st|nd|rd|th)?|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|his|her|their|"
    r"rights?)$",
    re.IGNORECASE,
)


def _split_leading_name(blob: str) -> tuple[str, str]:
    """Form D's asset text -> (giver name, remaining asset text).

    "KEVIN ROUND 4 PICK 10" -> ("KEVIN", "ROUND 4 PICK 10"). Only splits when the first
    token could be a name at all; "2027 DISCOVERY 2ND" keeps every word as the asset and
    reports no giver, so the blob is never silently truncated.
    """
    cleaned = _ANY_MENTION.sub(" ", blob or "").strip()
    parts = cleaned.split()
    if not parts:
        return "", cleaned
    head = parts[0].strip(".,:;'\u2019")
    if not head.replace(".", "").replace("'", "").isalpha() or _ASSET_WORD.match(head):
        return "", cleaned
    return head, " ".join(parts[1:])


def parse_trade(text: str) -> dict | None:
    """A trade post -> `{"a": name, "b": name, "a_assets": [...], "b_assets": [...]}`.

    `a` GIVES `a_assets`. Both forms in the sample are normalised to that, including
    form A's "X trades <assets> to Y for <assets>", where the second block is what Y
    gives up. Names are returned AS WRITTEN — resolving "KT" or "Sir Hefty Boy" to a
    manager needs the database and belongs to the caller.
    """
    if not is_trade_post(text):
        return None
    body = TRADE_HEADER.sub(" ", text)

    m = _FORM_A.search(body)
    if m:
        return {
            "form": "A",
            "a": _clean_name(m.group("a")),
            "b": _clean_name(m.group("b")),
            "a_discord_id": _mention_id(m.group("a")),
            "b_discord_id": _mention_id(m.group("b")),
            "a_assets": [parse_asset(x) for x in _clean_lines(m.group("a_assets"))],
            "b_assets": [parse_asset(x) for x in _clean_lines(m.group("b_assets"))],
        }

    headers = list(_FORM_B_HEADER.finditer(body))
    if len(headers) >= 2:
        first, second = headers[0], headers[1]
        return {
            "form": "B",
            "a": _clean_name(first.group("who")),
            "b": _clean_name(second.group("who")),
            "a_discord_id": _mention_id(first.group("who")),
            "b_discord_id": _mention_id(second.group("who")),
            "a_assets": [
                parse_asset(x) for x in _clean_lines(body[first.end():second.start()])
            ],
            "b_assets": [parse_asset(x) for x in _clean_lines(body[second.end():])],
        }

    # Form C, tried third: the assets share the manager's line. TWO such lines are
    # required — a lone "X trades Y" is as likely to be someone narrating a deal as
    # announcing one, and a one-sided trade is not a trade.
    inline = list(_FORM_C.finditer(body))
    if len(inline) >= 2:
        first, second = inline[0], inline[1]
        return {
            "form": "C",
            "a": _clean_name(first.group("who")),
            "b": _clean_name(second.group("who")),
            "a_discord_id": _mention_id(first.group("who")),
            "b_discord_id": _mention_id(second.group("who")),
            "a_assets": [parse_asset(x) for x in _clean_lines(first.group("assets"))],
            "b_assets": [parse_asset(x) for x in _clean_lines(second.group("assets"))],
        }

    d = _FORM_D.search(body)
    if d:
        giver, given = _split_leading_name(d.group("given"))
        return {
            "form": "D",
            # Normalised to this file's invariant that `a` GIVES `a_assets`, which means
            # inverting the message: it names the receiver first.
            "a": giver,
            "b": _clean_name(d.group("recv")),
            "a_discord_id": _mention_id(d.group("given")),
            "b_discord_id": _mention_id(d.group("recv")),
            "a_assets": [parse_asset(x) for x in _clean_lines(given)],
            "b_assets": [parse_asset(x) for x in _clean_lines(d.group("returned"))],
            # The giver is read off the head of the asset text by POSITION, so it is a
            # guess and is labelled as one — the `assumed_owner` precedent. It still
            # goes through `resolve_manager`, which refuses to guess: the real message
            # says "KEVIN" and this league has three, so it stages unresolved.
            "a_assumed": True,
        }
    return None


def parse_il(text: str) -> dict | None:
    """An IL post -> `{"player": name, "start_gw": int, "end_gw": int}`.

    There is NO replacement in the return value because there is none in the message.
    Every real sample proves it: "ekitike IL 1-4", "Minteh. (1-4)", "Saliba IL 1-4".
    The caller has to ask, and `place_on_il` will refuse without one.

    Only ever consulted for a message that is NOT a trade post, and only on a single
    line — a paragraph of prose that happens to contain a hyphenated number pair is not
    an IL announcement.
    """
    if is_trade_post(text):
        return None
    lines = [x for x in (text or "").splitlines() if x.strip()]
    if len(lines) != 1:
        return None
    m = _IL.match(lines[0])
    if not m:
        return None
    player = m.group("player").strip(" .,:;")
    if not player:
        return None
    start, end = int(m.group("start")), int(m.group("end"))
    # A gameweek range, not a scoreline or a date. 1-38 with start <= end.
    if not (1 <= start <= 38 and 1 <= end <= 38 and start <= end):
        return None
    return {"player": player, "start_gw": start, "end_gw": end}


def parse_discovery_pick(text: str) -> str | None:
    """A discovery-pick post -> the player name as typed, or None if this doesn't
    even look like one.

    Unlike a trade post or an IL post, real samples here are just a bare player
    name -- no keyword, no digit pattern, nothing to anchor on. So this stays
    deliberately minimal (blank or multi-line -> None; a real announcement is
    one line, never a paragraph) and does almost none of the discriminating
    work itself. The actual safety property -- not misfiring on ordinary
    channel chatter -- comes from the caller only ever accepting this for a
    manager who currently has an open discovery-draft slot (see
    discord_bridge.ingest_discovery_pick_message / services.
    manager_discovery_open_slots); a bare-name parser with no such gate would
    stage nearly every message in the channel.
    """
    lines = [x for x in (text or "").splitlines() if x.strip()]
    if len(lines) != 1:
        return None
    name = lines[0].strip(" .,:;!\"'")
    return name or None
