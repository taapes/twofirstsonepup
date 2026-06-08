"""Injury-list helper tests (Phase C). Rules (min stay, same position) are covered
in test_rules; here we cover the return-eligibility helper. Run: pytest"""

from rules import MIN_IL_STAY_GWS, SEASON_LAST_GW
from services import il_return_eligible_gw


def test_eligible_gw_is_start_plus_min_stay():
    assert il_return_eligible_gw(10) == 10 + MIN_IL_STAY_GWS


def test_eligible_gw_capped_at_season_end():
    # placed late: can always return by the season's last GW
    assert il_return_eligible_gw(36) == SEASON_LAST_GW
    assert il_return_eligible_gw(38) == SEASON_LAST_GW
