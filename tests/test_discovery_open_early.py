"""Opening the discovery draft window ahead of its Oct-1 auto-open.

`close_discovery` (an admin action) has existed for a while with no way back the other
way — a commissioner who wants the window open early (found 2026-09-08, well before
Oct 1) had no button and no service function for it. `open_discovery` mirrors
`close_discovery`'s shape exactly.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import League
from rules import RuleViolation


def _league(session, *, phase="in_season", discovery_open=False, discovery_done=False):
    lg = League(fpl_league_id="1", name="S", season_year=2026, is_current=True,
                sync_locked=False, phase=phase, discovery_open=discovery_open,
                discovery_done=discovery_done)
    session.add(lg)
    session.commit()
    return lg


def test_opening_early_sets_the_flag(test_session):
    lg = _league(test_session)
    r = services.open_discovery(test_session, lg)
    assert r == {"discovery_open": True, "changed": True}
    test_session.refresh(lg)
    assert lg.discovery_open is True


def test_opening_twice_is_a_no_op_not_an_error(test_session):
    lg = _league(test_session)
    services.open_discovery(test_session, lg)
    r = services.open_discovery(test_session, lg)
    assert r == {"discovery_open": True, "changed": False}


def test_refuses_before_in_season(test_session):
    """phase_features only turns discovery_open into discovery_available under
    in_season — flipping the flag any earlier would be a silent no-op the admin
    would have no way to notice."""
    lg = _league(test_session, phase="preseason")
    with pytest.raises(RuleViolation, match="in_season"):
        services.open_discovery(test_session, lg)


def test_refuses_once_this_seasons_window_already_closed(test_session):
    """discovery_done exists specifically so the Oct-1 tick can't re-open a closed
    window — an admin re-opening has to clear the same gate the calendar does."""
    lg = _league(test_session, discovery_done=True)
    with pytest.raises(RuleViolation, match="already closed"):
        services.open_discovery(test_session, lg)


def test_open_then_close_then_reopen_is_refused(test_session):
    lg = _league(test_session)
    services.open_discovery(test_session, lg)
    services.close_discovery(test_session, lg)
    with pytest.raises(RuleViolation, match="already closed"):
        services.open_discovery(test_session, lg)
