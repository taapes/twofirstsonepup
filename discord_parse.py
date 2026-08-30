"""Parsing league announcements out of Discord message text.

PURE. No database, no network, no ORM — it turns a string into a description of what
was announced, using only the vocabulary of the message itself (names as typed, rounds
as written). Resolving those names to rows is `discord_bridge`'s job, and keeping the
two apart is what makes this file table-testable against real message text.

DETERMINISTIC, and deliberately no LLM. The fuzzy half of this problem is not finding
the fields, it is deciding which human "Cunha" is — and `services._score_match` already
does that with difflib and token sets, purely and testably. Once those are separated,
finding the fields in the league's actual `#trades` conventions is a regex.

WHAT THE REAL MESSAGES LOOK LIKE (sampled Aug 2026; this is the requirements list):

    🚨🚨 TRADE ALERT 🚨🚨          │  🚨 TRADE ALERT 🚨
    John trades                    │  KT Trades:
    2026 Pick 6                    │  Cunha
    2026 discovery 2nd             │  Pick 12
    2028 discovery 2nd             │
                                   │  KS Trades:
    to Michael for                 │  6-9 Discoveries
    2026 pick 4                    │

    ekitike IL 1-4 (prob longer and indefinitely)
    Minteh. (1-4) probably longer
    Saliba IL 1-4 probably longer

Five facts drive everything below:

1. The 🚨 TRADE ALERT header is on every trade post and on nothing else, so message
   classification is free and exact.
2. An IL post names NO replacement player. `place_on_il` requires one, so an IL parse
   is ALWAYS incomplete — that is a property of the announcement, not of this parser,
   and no amount of cleverness fixes it.
3. Pick notation is genuinely two conventions: `Pick N` is the overall pick number,
   a bare ordinal (`6th`) is the round. Confirmed with the commissioner rather than
   guessed, because `pick_round` and `pick_number` are different things in the schema
   and reading one as the other silently reassigns a different manager's slot.
4. Nothing says whose pick a traded pick ORIGINALLY was.
5. Some assets are unparseable (`2026 4th 1st`, `6-9 Discoveries`). Those are reported
   as UNRESOLVED rather than dropped or guessed, so the review UI can ask.
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
_DISCOVERY = re.compile(r"\bdiscover(?:y|ies)\b", re.IGNORECASE)

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
    return re.sub(r"^[\W_]+|[\W_]+$", "", line, flags=re.UNICODE).strip()


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
            "a_assets": [
                parse_asset(x) for x in _clean_lines(body[first.end():second.start()])
            ],
            "b_assets": [parse_asset(x) for x in _clean_lines(body[second.end():])],
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
