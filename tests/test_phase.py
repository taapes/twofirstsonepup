"""League phase feature-matrix tests (Phase D2). Run: pytest"""

import pytest

from rules import (
    PHASE_DRAFT,
    PHASE_IN_SEASON,
    PHASE_OFFSEASON,
    PHASE_PRESEASON,
    phase_features,
)


def test_offseason():
    f = phase_features(PHASE_OFFSEASON)
    assert f["trades_allowed"] and f["keepers_editable"]
    assert not f["draft_available"] and not f["my_team_available"]
    assert f["prior_locked"] and not f["gw_logic_active"]


def test_draft():
    f = phase_features(PHASE_DRAFT)
    assert f["draft_available"] and f["trades_allowed"]
    assert not f["keepers_editable"]            # keepers lock when drafting
    assert not f["my_team_available"]


def test_preseason():
    f = phase_features(PHASE_PRESEASON)
    assert f["my_team_available"] and f["trades_allowed"]
    assert not f["draft_available"] and not f["keepers_editable"]
    assert not f["prior_locked"] and not f["gw_logic_active"]


def test_in_season_base():
    f = phase_features(PHASE_IN_SEASON, gw_logic=True)
    assert f["trades_allowed"] and f["gw_logic_active"] and f["my_team_available"]
    assert not f["discovery_available"] and not f["cups_available"]
    assert not f["keepers_editable"] and not f["draft_available"]


def test_in_season_post_trade_deadline():
    f = phase_features(PHASE_IN_SEASON, trades_off=True, gw_logic=True)
    assert not f["trades_allowed"] and f["gw_logic_active"]


def test_in_season_discovery_window():
    f = phase_features(PHASE_IN_SEASON, discovery_open=True, gw_logic=True)
    assert f["discovery_available"]


def test_in_season_cup_season():
    f = phase_features(PHASE_IN_SEASON, cups_available=True, trades_off=True, gw_logic=True)
    assert f["cups_available"] and not f["trades_allowed"]


def test_unknown_phase_raises():
    with pytest.raises(ValueError):
        phase_features("nonsense")
