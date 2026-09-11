"""The Discord message parser, against the messages the league actually posts.

Every fixture in this file is a REAL message sampled from `#trades` and the IL channel
in August 2026, reproduced verbatim including its emoji and its trailing prose. They are
the requirements list for the parser, so they are pinned rather than paraphrased.

The point of most of these tests is what the parser REFUSES to decide. Two of the five
sampled trades contain an asset that cannot be read unambiguously, and guessing would
silently reassign a different manager's draft slot — with nothing downstream to catch
it, exactly like the discovery-link failure mode `models.py` documents. So "unresolved"
is the correct answer, not a gap.

Pure: no database, no network, no fixtures.
"""

import pytest

import discord_parse as P

# ---- the five real messages ---------------------------------------------------
TRADE_A_PICKS = """🚨🚨 Trade Alert 🚨

John trades
2026 Pick 6
2026 discovery 2nd
2028 discovery 2nd

to Michael for
2026 pick 4"""

TRADE_A_ORDINALS = """🚨🚨TRADE ALERT🚨🚨
John trades:
2026 7th, 8th, and 9th
to Tucker for:
2026 6th"""

TRADE_B_PLAYER = """🚨🚨 TRADE ALERT 🚨🚨
Kevin trades:
2026 4th 1st
Steve trades:
Guehi"""

TRADE_B_INITIALS = """🚨 TRADE ALERT 🚨

KT Trades:

Cunha
Pick 12

KS Trades:

6-9 Discoveries"""

# Forms C and D, found by sweeping the whole channel rather than sampling it. All four
# of these parsed to NOTHING until 2026-09-07, and all four are pick-only trades — the
# kind that exist in no FPL feed, so a missed one is a wrong draft board in 2027.
TRADE_C_ROUND_PICK = """TRADE ALERT

KEVIN T TRADES ROUND 3 PICK 10

STEVE TRADES ROUND 5 PICK 5 AND 2027 2nd round discovery"""

TRADE_C_DISCOVERY = """TRADE ALERT:

🚨🚨🚨🚨
Tucker trades 2027 1st round discovery

Kevin trades 2028 1st round discovery

🚨🚨🚨🚨"""

TRADE_D_GETS = ("TRADE ALERT: GABY GETS KEVIN ROUND 4 PICK 10 FOR "
                "2027 DISCOVERY, 2ND ROUND")

# The mention form. `<@...>` used to survive the name cleanup as "1362...> Kevin S".
TRADE_A_MENTION = """🚨🚨TRADE ALERT 🚨🚨
John trades:
2028 John 1st round
To <@1362557356201738515> Kevin S for:
Bruno Fernandes"""

# A real message carrying TWO trades: an inline pair (form C's shape) followed by a
# form A body. It is the reason the form matchers are tried A, B, C, D and not in any
# other order — and the reason that order needs a test rather than a comment.
TRADE_TWO_IN_ONE = """adding for posterity

TRADE!\u0020

Kevin F trades his 2026 Discovery 2nd

Kevin T trades his 2026, 5th and 6th rounds


\U0001f6a8 \U0001f6a8 Trade Alert\U0001f6a8 \U0001f6a8

John trades
2026 Pick 6
2026 discovery 2nd
2028 discovery 2nd

to Michael for
2026 pick 4"""

IL_POSTS = [
    "ekitike IL 1-4 (prob longer and indefinitely)",
    "Minteh. (1-4) probably longer",
    "Saliba IL 1-4 probably longer",
]


# ---- forms C and D ------------------------------------------------------------
def test_assets_on_the_managers_own_line_are_a_trade():
    """Form C. `_FORM_B_HEADER` wants "Who trades:" alone on its line, so three real
    messages matched nothing at all."""
    r = P.parse_trade(TRADE_C_DISCOVERY)
    assert r is not None and r["form"] == "C"
    assert (r["a"], r["b"]) == ("Tucker", "Kevin")
    assert r["a_assets"] == [{
        "kind": "pick", "notation": "round", "rounds": [1], "season_year": 2027,
        "draft_type": "discovery", "text": "2027 1st round discovery"}]
    assert r["b_assets"][0]["season_year"] == 2028


def test_form_c_is_only_a_trade_when_two_managers_give():
    """A lone "X trades Y" line is as likely to be someone narrating a deal as
    announcing one, and a one-sided trade is not a trade."""
    assert P.parse_trade("TRADE ALERT\nTucker trades 2027 1st round discovery") is None


def test_a_gets_for_post_is_read_with_the_giver_and_receiver_the_right_way_round():
    """Form D names the RECEIVER first — inverted from every other form. This file's
    invariant is that `a` gives `a_assets`, so the message has to be turned around."""
    r = P.parse_trade(TRADE_D_GETS)
    assert r is not None and r["form"] == "D"
    assert r["a"] == "KEVIN", "the giver, read off the head of the asset text"
    assert r["b"] == "GABY", "the receiver, named first in the message"
    # Gaby gives the discovery pick; Kevin gives the round-4 slot.
    assert r["b_assets"] == [{
        "kind": "pick", "notation": "round", "rounds": [2], "season_year": 2027,
        "draft_type": "discovery", "text": "2027 DISCOVERY, 2ND ROUND"}]


def test_the_form_d_giver_is_flagged_as_an_assumption():
    """It is read by POSITION, so it is a guess and has to travel as one. The real
    message says "KEVIN" and this league has three, so `resolve_manager` will refuse
    it — but only if the queue knows not to trust the name."""
    assert P.parse_trade(TRADE_D_GETS)["a_assumed"] is True


def test_form_d_never_swallows_an_asset_word_as_a_name():
    """If the giver isn't named, the asset text must survive intact rather than losing
    its first token to a manager slot."""
    r = P.parse_trade("TRADE ALERT: GABY GETS 2027 DISCOVERY 2ND FOR CUNHA")
    assert r["a"] == ""
    assert r["a_assets"][0]["text"] == "2027 DISCOVERY 2ND"


def test_form_a_wins_when_a_message_carries_an_inline_pair_as_well():
    """The matcher ORDER is a safety property, not a style choice. This real message has
    two inline "X trades <assets>" lines above a form A body; if C ran first it would
    claim the message and report the wrong two managers and the wrong assets.

    It also pins the known limitation rather than hiding it: only ONE of the two trades
    in this post is extracted. Two trades in one message is out of scope — splitting on
    the banner would be a different change, and the post is visible either way.
    """
    r = P.parse_trade(TRADE_TWO_IN_ONE)
    assert r["form"] == "A"
    assert (r["a"], r["b"]) == ("John", "Michael")
    assert [x.get("number") or x.get("rounds") for x in r["a_assets"]] == [6, [2], [2]]


# ---- a position within a round is not an overall pick number ------------------
def test_round_n_pick_m_is_unresolved_not_an_overall_pick():
    """It USED to return {"notation": "overall", "number": 10} — the 10th pick of the
    whole draft instead of the 10th pick of round 3, which is a different manager's
    slot. Naming that manager needs the draft order, which a pure parser hasn't got."""
    a = P.parse_asset("ROUND 3 PICK 10")
    assert a["kind"] == "unresolved"
    assert "position within a round" in a["why"]


def test_the_round_pick_guard_leaves_every_other_notation_alone():
    """The guard keys on a round named by DIGIT, so an ordinal round is untouched."""
    assert P.parse_asset("2026 Pick 6")["notation"] == "overall"
    assert P.parse_asset("2027 1st round discovery")["notation"] == "round"
    assert P.parse_asset("2026 5th round pick")["notation"] == "round"
    assert P.parse_asset("2026 4th (Mark's pick - 8th)")["notation"] == "round"


def test_a_discovery_typo_still_reads_as_a_discovery_pick():
    """A real message says "Tucker's 2027 Discover 2nd". Reading that as a MAIN-draft
    pick moves the wrong pick, and nothing downstream would catch it."""
    assert P.parse_asset("Tucker's 2027 Discover 2nd")["draft_type"] == "discovery"


# ---- mentions -----------------------------------------------------------------
def test_a_mention_does_not_mangle_the_manager_name():
    r = P.parse_trade(TRADE_A_MENTION)
    assert r["b"] == "Kevin S", "was '1362557356201738515> Kevin S'"


def test_a_mention_is_captured_as_an_id_not_merely_stripped():
    """It is `resolve_manager`'s exact tier and the whole reason
    `managers.discord_user_id` exists — throwing it away would discard the best
    identifier in the message."""
    r = P.parse_trade(TRADE_A_MENTION)
    assert r["b_discord_id"] == "1362557356201738515"
    assert r["a_discord_id"] is None, "John is written as a plain name"


def test_a_role_mention_is_stripped_but_never_read_as_a_person():
    r = P.parse_trade(
        "TRADE ALERT\nJohn trades:\nBruno\nTo <@&99887766> the league for:\n2026 6th")
    assert r["b"] == "the league"
    assert r["b_discord_id"] is None


# ---- classification -----------------------------------------------------------
ALL_REAL_TRADES = [
    TRADE_A_PICKS, TRADE_A_ORDINALS, TRADE_B_PLAYER, TRADE_B_INITIALS,
    TRADE_C_ROUND_PICK, TRADE_C_DISCOVERY, TRADE_D_GETS, TRADE_A_MENTION,
]


@pytest.mark.parametrize("text", ALL_REAL_TRADES)
def test_every_real_trade_post_is_classified_as_one(text):
    """The banner is on every trade post, so classification is free. It is NOT absent
    from everything else — see the prose test below — so the form matchers, not this,
    are what actually filter."""
    assert P.is_trade_post(text) is True


@pytest.mark.parametrize("text", ALL_REAL_TRADES)
def test_every_real_trade_post_yields_two_managers_and_some_assets(text):
    """The sweep that matters: a trade post this file cannot read is a pick trade lost
    for good, since no FPL feed carries one. Four of these eight returned None until
    2026-09-07."""
    r = P.parse_trade(text)
    assert r is not None, "parsed to nothing"
    assert r["b"], "no counterparty"
    assert r["a_assets"] and r["b_assets"], "a trade moves something both ways"


def test_a_message_complaining_about_the_channel_is_not_a_trade():
    """The header check alone is not a filter: this real message matches it. It is safe
    only because no form matches, which is why the forms must stay strict."""
    text = ("PLEASE MOVE THIS CHAT TO THE RULES-DELIBERATION channel. "
            "This is the Trade Alerts channel SMH")
    assert P.is_trade_post(text) is True
    assert P.parse_trade(text) is None


@pytest.mark.parametrize("text", IL_POSTS + ["who's starting haaland this week?", ""])
def test_ordinary_chatter_is_not_a_trade_post(text):
    assert P.is_trade_post(text) is False


# ---- names --------------------------------------------------------------------
def test_the_banner_does_not_leak_into_the_manager_name():
    """Form A's opening capture starts at the top of the message, so it drags the
    emoji banner along; the name is the last line of it."""
    r = P.parse_trade(TRADE_A_PICKS)
    assert (r["a"], r["b"]) == ("John", "Michael")
    assert "🚨" not in r["a"]


def test_both_body_layouts_normalise_to_a_gives_b_gives():
    """Form A ("X trades ... to Y for ...") and form B ("X Trades: ... Y Trades: ...")
    are the only two shapes in the sample, and downstream should not care which."""
    a = P.parse_trade(TRADE_A_ORDINALS)
    b = P.parse_trade(TRADE_B_PLAYER)
    assert (a["form"], a["a"], a["b"]) == ("A", "John", "Tucker")
    assert (b["form"], b["a"], b["b"]) == ("B", "Kevin", "Steve")


def test_initials_are_returned_as_written():
    """"KT"/"KS" are resolved against display_name by the caller, which needs the
    database. The parser's job is to report what was typed, not to guess who it is."""
    r = P.parse_trade(TRADE_B_INITIALS)
    assert (r["a"], r["b"]) == ("KT", "KS")


# ---- pick notation ------------------------------------------------------------
def test_pick_n_is_the_overall_number_and_an_ordinal_is_the_round():
    """The league uses BOTH conventions, confirmed rather than assumed. Reading one as
    the other assigns a different slot, and `pick_round` and `pick_number` are
    different things in this schema."""
    assert P.parse_asset("2026 Pick 6") == {
        "kind": "pick", "notation": "overall", "number": 6,
        "season_year": 2026, "draft_type": "main", "text": "2026 Pick 6"}
    assert P.parse_asset("2026 6th") == {
        "kind": "pick", "notation": "round", "rounds": [6],
        "season_year": 2026, "draft_type": "main", "text": "2026 6th"}


def test_a_separated_ordinal_list_is_one_asset_line_of_several_rounds():
    got = P.parse_asset("2026 7th, 8th, and 9th")
    assert got["notation"] == "round" and got["rounds"] == [7, 8, 9]


def test_discovery_picks_carry_their_draft_type():
    got = P.parse_asset("2026 discovery 2nd")
    assert (got["draft_type"], got["rounds"], got["season_year"]) == (
        "discovery", [2], 2026)


def test_a_future_season_pick_keeps_its_year():
    """2028-2030 picks appear in real deals, years before those league rows exist."""
    assert P.parse_asset("2028 discovery 2nd")["season_year"] == 2028


# ---- what the parser refuses to decide ----------------------------------------
def test_two_juxtaposed_ordinals_are_unresolved():
    """A real message says "2026 4th 1st". A LIST of rounds is written with separators
    ("7th, 8th, and 9th"), so two ordinals merely juxtaposed are more likely "the 4th
    pick of the 1st round" — a different thing, and one that would reassign someone
    else's slot."""
    got = P.parse_asset("2026 4th 1st")
    assert got["kind"] == "unresolved"
    assert got["text"] == "2026 4th 1st"
    assert "separator" in got["why"]


def test_a_pick_range_is_unresolved():
    """"6-9 Discoveries" could be picks 6..9 or rounds 6..9. It is not guessable."""
    got = P.parse_asset("6-9 Discoveries")
    assert got["kind"] == "unresolved" and got["text"] == "6-9 Discoveries"


def test_mixing_pick_n_with_an_ordinal_is_unresolved():
    assert P.parse_asset("2026 Pick 6 2nd")["kind"] == "unresolved"


def test_an_unresolved_asset_still_appears_in_the_trade():
    """Dropping it would silently shrink the deal. It has to reach the review queue so
    a human can be asked."""
    r = P.parse_trade(TRADE_B_INITIALS)
    assert [a["kind"] for a in r["b_assets"]] == ["unresolved"]
    assert r["b_assets"][0]["text"] == "6-9 Discoveries"


def test_a_bare_name_is_a_player():
    assert P.parse_asset("Guehi") == {"kind": "player", "name": "Guehi", "text": "Guehi"}
    assert P.parse_asset("Cunha")["name"] == "Cunha"


# ---- the full trades ----------------------------------------------------------
def test_the_multi_pick_trade_parses_every_asset():
    r = P.parse_trade(TRADE_A_PICKS)
    assert [(a["draft_type"], a.get("number") or a.get("rounds"), a["season_year"])
            for a in r["a_assets"]] == [
        ("main", 6, 2026), ("discovery", [2], 2026), ("discovery", [2], 2028)]
    assert [(a["draft_type"], a.get("number")) for a in r["b_assets"]] == [("main", 4)]


def test_a_player_for_picks_trade_keeps_both_sides():
    r = P.parse_trade(TRADE_B_PLAYER)
    assert [a["kind"] for a in r["a_assets"]] == ["unresolved"]
    assert [a["name"] for a in r["b_assets"]] == ["Guehi"]


def test_a_message_that_is_not_a_trade_returns_none():
    assert P.parse_trade("just posting a meme") is None


# ---- IL -----------------------------------------------------------------------
@pytest.mark.parametrize("text,player", list(zip(IL_POSTS, ["ekitike", "Minteh", "Saliba"])))
def test_every_real_il_post_yields_a_player_and_a_start_gw(text, player):
    got = P.parse_il(text)
    assert got == {"player": player, "start_gw": 1, "end_gw": 4}


def test_an_il_post_never_names_a_replacement():
    """THE structural fact of this feature. `place_on_il` requires a replacement and
    the announcement does not contain one, so an IL proposal is always incomplete and
    a human always has to finish it. No parser improvement changes this."""
    for text in IL_POSTS:
        assert "replacement" not in P.parse_il(text)


def test_the_word_il_is_not_required():
    """"Minteh. (1-4)" is a real post and says only the name and the range."""
    assert P.parse_il("Minteh. (1-4) probably longer")["player"] == "Minteh"


def test_trailing_prose_is_discarded():
    assert P.parse_il("ekitike IL 1-4 (prob longer and indefinitely)")["player"] == "ekitike"


def test_a_trade_post_is_never_read_as_an_il_post():
    for text in (TRADE_A_PICKS, TRADE_B_INITIALS):
        assert P.parse_il(text) is None


def test_a_multi_line_message_is_not_an_il_post():
    """A paragraph that happens to contain a hyphenated number pair is chatter."""
    assert P.parse_il("saliba looks rough\nmaybe 1-4 weeks out?") is None


@pytest.mark.parametrize("text", [
    "Saliba IL 0-4",     # GW 0 doesn't exist
    "Saliba IL 1-40",    # past GW38
    "Saliba IL 6-2",     # backwards
])
def test_an_implausible_gameweek_range_is_not_an_il_post(text):
    assert P.parse_il(text) is None


def test_content_alone_cannot_tell_a_gameweek_range_from_a_scoreline():
    """An honest limit, not a bug. "Arsenal 1-3" has the same shape as an IL post and
    parses as one — which is precisely why the poller reads a DEDICATED IL channel
    rather than sniffing every message in #general. Recording it here so nobody later
    "fixes" the parser to be cleverer than its input allows.

    Note the reversed scoreline ("3-1") IS rejected, but only incidentally, by the
    start <= end bound — not because anything understood it was a football result.
    """
    assert P.parse_il("Arsenal 1-3") is not None
    assert P.parse_il("Arsenal 3-1") is None


# ---- parse_discovery_pick -----------------------------------------------------
# No keyword or digit pattern to anchor on here -- real posts are just a bare
# player name. The safety property lives one layer up, in the caller (see
# discord_bridge.ingest_discovery_pick_message): only ever accepted for a
# manager who currently has an open discovery-draft slot.

def test_a_bare_name_parses_as_itself():
    assert P.parse_discovery_pick("Wolfsburg's new signing") == "Wolfsburg's new signing"


@pytest.mark.parametrize("text", ["", "   ", "\n\n", None])
def test_blank_content_does_not_parse(text):
    assert P.parse_discovery_pick(text) is None


def test_a_multiline_message_does_not_parse():
    """A real announcement is one bare name, never a paragraph."""
    assert P.parse_discovery_pick("Taking a keeper\nfor the future") is None


def test_surrounding_punctuation_is_stripped():
    assert P.parse_discovery_pick("  Some Kid.  ") == "Some Kid"
