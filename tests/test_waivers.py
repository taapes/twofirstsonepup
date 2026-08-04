"""Unit tests for the v2 waiver logic (pure, no DB)."""

from rules import advance_waiver_priority, initial_waiver_order, resolve_waivers


def test_initial_order_is_reverse_standings():
    order = initial_waiver_order(["worst", "mid", "best"])
    assert order == {"worst": 0, "mid": 1, "best": 2}


def test_resolve_waivers_priority_wins_contested_player():
    # Two managers claim the same free player X; higher priority (listed first) wins.
    claims = [
        {"id": "c1", "add": "X", "drop_owned": True},   # priority 0
        {"id": "c2", "add": "X", "drop_owned": True},   # priority 1
    ]
    res = resolve_waivers(claims, owned=set())
    assert res["c1"] == ("won", None)
    assert res["c2"][0] == "lost"


def test_resolve_waivers_owned_player_unavailable():
    claims = [{"id": "c1", "add": "Y", "drop_owned": True}]
    res = resolve_waivers(claims, owned={"Y"})
    assert res["c1"][0] == "lost"


def test_resolve_waivers_bad_drop_loses():
    claims = [{"id": "c1", "add": "Z", "drop_owned": False}]
    res = resolve_waivers(claims, owned=set())
    assert res["c1"][0] == "lost"
    assert "drop" in res["c1"][1]


def test_resolve_waivers_independent_players_all_win():
    claims = [
        {"id": "c1", "add": "A", "drop_owned": True},
        {"id": "c2", "add": "B", "drop_owned": True},
    ]
    res = resolve_waivers(claims, owned=set())
    assert res["c1"][0] == "won" and res["c2"][0] == "won"


def test_advance_priority_moves_winners_to_back():
    order = {"a": 0, "b": 1, "c": 2, "d": 3}
    new = advance_waiver_priority(order, winners=["a", "c"])
    # non-winners keep relative order first, then winners in their prior order
    assert new == {"b": 0, "d": 1, "a": 2, "c": 3}


def test_advance_priority_no_winners_unchanged():
    order = {"a": 0, "b": 1, "c": 2}
    assert advance_waiver_priority(order, winners=[]) == order
