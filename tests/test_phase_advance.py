"""Pure phase auto-advance decision tests (Phase D4). Run: pytest"""

import datetime as dt
from zoneinfo import ZoneInfo

from rules import (
    PHASE_DRAFT,
    PHASE_IN_SEASON,
    PHASE_OFFSEASON,
    PHASE_PRESEASON,
    next_phase,
)

SY = 2026  # season-start year; Sept 2 2026 10am Pacific opens discovery, Feb 1 2027 the deadline
PACIFIC = ZoneInfo("America/Los_Angeles")
# The open instant itself, expressed in Pacific local time then converted — this is
# what "10am Pacific" means (rules.DISCOVERY_OPEN_TZ), not a fixed UTC offset.
OPENS_AT = dt.datetime(2026, 9, 2, 10, 0, tzinfo=PACIFIC)
PRE = OPENS_AT - dt.timedelta(days=18)   # well before
JUST_BEFORE = OPENS_AT - dt.timedelta(minutes=1)
JUST_AFTER = OPENS_AT + dt.timedelta(minutes=1)
POST = OPENS_AT + dt.timedelta(days=1)


def _np(macro, **kw):
    base = dict(gw38_done=False, gw1_started=False, now=PRE, season_year=SY,
                discovery_open=False, discovery_done=False)
    base.update(kw)
    return next_phase(macro, **base)


def test_in_season_to_offseason_at_gw38():
    assert _np(PHASE_IN_SEASON, gw38_done=True) == (PHASE_OFFSEASON, None)


def test_preseason_to_in_season_at_gw1():
    macro, _ = _np(PHASE_PRESEASON, gw1_started=True)
    assert macro == PHASE_IN_SEASON


def test_preseason_holds_until_gw1():
    macro, _ = _np(PHASE_PRESEASON, gw1_started=False)
    assert macro == PHASE_PRESEASON


def test_discovery_auto_opens_sept2_10am_pacific_in_season():
    macro, open_disc = _np(PHASE_IN_SEASON, now=POST)
    assert macro == PHASE_IN_SEASON and open_disc is True


def test_discovery_not_opened_before_sept2_10am_pacific():
    _, open_disc = _np(PHASE_IN_SEASON, now=PRE)
    assert open_disc is None


def test_discovery_opens_at_the_precise_minute_not_just_the_date():
    """The whole point of moving off a date-only check: one minute either side of
    10am Pacific on Sept 2 must read differently, not just "today >= Sept 2"."""
    assert _np(PHASE_IN_SEASON, now=JUST_BEFORE)[1] is None
    assert _np(PHASE_IN_SEASON, now=JUST_AFTER)[1] is True


def test_a_utc_instant_that_reads_sept2_but_is_earlier_in_pacific_is_not_open_yet():
    """Sept 2 10am Pacific is 17:00 UTC (PDT). A UTC clock already showing Sept 2 by
    a few hours (say 08:00 UTC, still Sept 1 evening in Pacific) must NOT open the
    window — this is exactly the bug a naive UTC date check would reintroduce."""
    early_utc = dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.timezone.utc)
    assert _np(PHASE_IN_SEASON, now=early_utc)[1] is None


def test_discovery_not_reopened_when_done():
    _, open_disc = _np(PHASE_IN_SEASON, now=POST, discovery_done=True)
    assert open_disc is None


def test_discovery_not_reopened_when_already_open():
    _, open_disc = _np(PHASE_IN_SEASON, now=POST, discovery_open=True)
    assert open_disc is None


def test_offseason_does_not_auto_advance():
    # leaving offseason is admin-driven (draft start); auto-advance leaves it put
    assert _np(PHASE_OFFSEASON, gw1_started=True, now=POST) == (PHASE_OFFSEASON, None)


def test_draft_does_not_auto_advance():
    assert _np(PHASE_DRAFT, gw1_started=True) == (PHASE_DRAFT, None)


def test_gw38_only_triggers_from_in_season():
    # a stale preseason with gw38_done shouldn't jump to offseason
    macro, _ = _np(PHASE_PRESEASON, gw38_done=True, gw1_started=False)
    assert macro == PHASE_PRESEASON
