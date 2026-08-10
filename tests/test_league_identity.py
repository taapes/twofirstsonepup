"""The guard that stopped a stranger's league being merged into ours.

Aug 2026: FPL handed our finished 25/26 league id (1754) to a different league.
The nightly sync upserted it into our season row — 12 foreign managers, 228
foreign fixtures, and our league name/season/draft date overwritten.
"""

import pytest

from rules import verify_league_feed

OURS = ["43908", "264571", "5520", "21768", "192955"]


def test_fresh_league_row_accepts_anything():
    # No managers stored yet = a new season's row; nothing to compare against.
    ok, reason = verify_league_feed([], ["1", "2", "3"])
    assert ok and reason == ""


def test_same_league_passes():
    ok, reason = verify_league_feed(OURS, OURS)
    assert ok and reason == ""


def test_tolerates_a_departed_manager():
    # One manager leaves and is replaced: still clearly our league.
    ok, _ = verify_league_feed(OURS, OURS[:-1] + ["999999"])
    assert ok


def test_rejects_a_completely_different_league():
    # The actual incident: zero overlap with the 'Rottehulen' feed.
    ok, reason = verify_league_feed(OURS, ["4969", "5186", "7847", "4877"])
    assert not ok
    assert "0/5 known managers" in reason


def test_rejects_when_majority_of_managers_vanish():
    ok, reason = verify_league_feed(OURS, [OURS[0], OURS[1], "111", "222", "333"])
    assert not ok
    assert "2/5 known managers" in reason


def test_rejects_a_season_year_jump():
    # Same people would pass the overlap check; the season still moved on.
    ok, reason = verify_league_feed(
        OURS, OURS, stored_season_year=2025, fetched_season_year=2026
    )
    assert not ok
    assert "reused league id" in reason


def test_season_check_skipped_when_feed_has_no_draft_date():
    ok, _ = verify_league_feed(
        OURS, OURS, stored_season_year=2025, fetched_season_year=None
    )
    assert ok


def test_empty_feed_is_rejected():
    ok, reason = verify_league_feed(OURS, [])
    assert not ok
    assert "no league entries" in reason


def test_ids_compare_across_int_and_str():
    ok, _ = verify_league_feed([43908, 264571], ["43908", "264571"])
    assert ok


@pytest.mark.parametrize("overlap,expected", [(0.9, False), (0.4, True)])
def test_threshold_is_tunable(overlap, expected):
    # 3 of 5 known managers present = 60% overlap.
    ok, _ = verify_league_feed(
        OURS, OURS[:3] + ["888", "999"], min_overlap=overlap
    )
    assert ok is expected
