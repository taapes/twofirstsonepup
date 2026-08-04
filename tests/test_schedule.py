"""Unit tests for the v2 H2H schedule generator (pure, no DB)."""

from collections import Counter

from rules import round_robin, season_schedule


def _all_pairs(rounds):
    return [frozenset(p) for rnd in rounds for p in rnd]


def test_round_robin_even_every_pair_once():
    ids = [1, 2, 3, 4, 5, 6]
    rounds = round_robin(ids)
    assert len(rounds) == 5              # n-1 rounds
    for rnd in rounds:
        assert len(rnd) == 3             # n/2 games, everyone plays
        played = [m for pair in rnd for m in pair]
        assert sorted(played) == ids     # each manager exactly once per round
    pairs = _all_pairs(rounds)
    assert len(pairs) == len(set(pairs)) == 15   # C(6,2), each once


def test_round_robin_odd_has_bye():
    ids = [1, 2, 3, 4, 5]
    rounds = round_robin(ids)
    assert len(rounds) == 5              # n (with bye) - 1
    for rnd in rounds:
        played = [m for pair in rnd for m in pair]
        assert len(played) == 4          # one sits out (bye)
        assert None not in played
    pairs = _all_pairs(rounds)
    assert len(pairs) == len(set(pairs)) == 10   # C(5,2)


def test_double_round_robin_home_away_perfectly_balanced():
    ids = list(range(1, 11))             # 10 managers -> double RR = 18 rounds
    sched = season_schedule(ids, 18)     # exactly one full double cycle
    home = Counter(h for pairs in sched.values() for (h, _a) in pairs)
    away = Counter(a for pairs in sched.values() for (_h, a) in pairs)
    for mid in ids:                      # each pairing played once home, once away
        assert home[mid] == 9 and away[mid] == 9


def test_season_schedule_covers_every_gw_once_per_manager():
    ids = list(range(1, 11))
    sched = season_schedule(ids, 38)
    assert len(sched) == 38
    for gw, pairs in sched.items():
        played = [m for pair in pairs for m in pair]
        assert sorted(played) == ids     # everyone plays exactly once each GW


def test_season_schedule_double_round_robin_swaps_home_away():
    ids = [1, 2, 3, 4]
    sched = season_schedule(ids, 6)      # single RR = 3 rounds; double = 6
    leg1 = _all_pairs([sched[1], sched[2], sched[3]])
    leg2 = _all_pairs([sched[4], sched[5], sched[6]])
    assert set(leg1) == set(leg2)        # same pairings, second leg reversed
