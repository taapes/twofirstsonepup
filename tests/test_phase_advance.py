"""Pure phase auto-advance decision tests (Phase D4). Run: pytest"""

import datetime as dt

from rules import (
    PHASE_DRAFT,
    PHASE_IN_SEASON,
    PHASE_OFFSEASON,
    PHASE_PRESEASON,
    next_phase,
)

SY = 2026  # season-start year; Oct 1 2026 opens discovery, Feb 1 2027 is the deadline
PRE_OCT = dt.date(2026, 8, 15)
POST_OCT = dt.date(2026, 10, 2)


def _np(macro, **kw):
    base = dict(gw38_done=False, gw1_started=False, today=PRE_OCT, season_year=SY,
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


def test_discovery_auto_opens_oct1_in_season():
    macro, open_disc = _np(PHASE_IN_SEASON, today=POST_OCT)
    assert macro == PHASE_IN_SEASON and open_disc is True


def test_discovery_not_opened_before_oct1():
    _, open_disc = _np(PHASE_IN_SEASON, today=PRE_OCT)
    assert open_disc is None


def test_discovery_not_reopened_when_done():
    _, open_disc = _np(PHASE_IN_SEASON, today=POST_OCT, discovery_done=True)
    assert open_disc is None


def test_discovery_not_reopened_when_already_open():
    _, open_disc = _np(PHASE_IN_SEASON, today=POST_OCT, discovery_open=True)
    assert open_disc is None


def test_offseason_does_not_auto_advance():
    # leaving offseason is admin-driven (draft start); auto-advance leaves it put
    assert _np(PHASE_OFFSEASON, gw1_started=True, today=POST_OCT) == (PHASE_OFFSEASON, None)


def test_draft_does_not_auto_advance():
    assert _np(PHASE_DRAFT, gw1_started=True) == (PHASE_DRAFT, None)


def test_gw38_only_triggers_from_in_season():
    # a stale preseason with gw38_done shouldn't jump to offseason
    macro, _ = _np(PHASE_PRESEASON, gw38_done=True, gw1_started=False)
    assert macro == PHASE_PRESEASON
