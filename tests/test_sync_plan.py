"""Pure sync-cadence decision tests (Phase D7). Run: pytest"""

from rules import decide_sync


def test_full_when_nothing_synced_today():
    assert decide_sync(full_today=False, live_fixture=False, gw_starts_today=False) == "full"


def test_full_on_a_gw_start_day_even_if_already_synced():
    assert decide_sync(full_today=True, live_fixture=False, gw_starts_today=True) == "full"


def test_live_when_a_match_is_in_window():
    assert decide_sync(full_today=True, live_fixture=True, gw_starts_today=False) == "live"


def test_skip_when_done_and_nothing_live():
    assert decide_sync(full_today=True, live_fixture=False, gw_starts_today=False) == "skip"


def test_full_takes_precedence_over_live():
    # a GW-start day with a live match still does the full pipeline
    assert decide_sync(full_today=True, live_fixture=True, gw_starts_today=True) == "full"
