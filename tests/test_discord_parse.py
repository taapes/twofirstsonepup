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

IL_POSTS = [
    "ekitike IL 1-4 (prob longer and indefinitely)",
    "Minteh. (1-4) probably longer",
    "Saliba IL 1-4 probably longer",
]


# ---- classification -----------------------------------------------------------
@pytest.mark.parametrize("text", [
    TRADE_A_PICKS, TRADE_A_ORDINALS, TRADE_B_PLAYER, TRADE_B_INITIALS])
def test_every_real_trade_post_is_classified_as_one(text):
    """The banner is on every trade post and nothing else, so classification is exact
    rather than heuristic — which is why no scoring is needed here at all."""
    assert P.is_trade_post(text) is True


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
