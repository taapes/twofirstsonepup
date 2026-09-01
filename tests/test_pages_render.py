"""Every HTML page renders. A sweep, not a feature test.

WHY THIS EXISTS. Nothing asserted that the site's pages return anything at all — 37
HTML routes, each covered only where some feature test happened to touch it. A template
typo, a renamed context key or a helper that starts returning None takes a page down and
the suite stays green.

WHY IT ASSERTS 200 AND NOT "NOT 500". Writing this the sloppy way is worse than not
writing it: an ad-hoc version of this check "passed" while 26 of 35 routes were quietly
303-ing to /who, because it logged in as admin and the site-wide gate wants a manager
identity for manager pages. A smoke test that accepts a redirect proves the redirect
works and nothing else.

WHY AN EXPLICIT LIST AND NOT `app.routes`. Deriving the list would sweep in 60+ POST
routes and the /v1 + /admin JSON routers, and — worse — would silently stop covering a
page the moment someone dropped `response_class=HTMLResponse`. An explicit list fails
loudly when a route is added, which is the entire point.

It goes through `test_session`, whose TRUNCATE teardown is what stops it leaking. A
throwaway script version of this seeded a league, left it behind, and broke
test_absence_ownership on the next full run.
"""

import pytest

from auth import hash_password
from models import Gameweek, League, Manager

SEASON = 2026
OWNER_FPL = "77"

# Pages reachable with an admin session. Path params are filled from the seed below.
ADMIN_SESSION_PAGES = [
    "/",
    "/who",
    "/admin/login",
    "/teams",
    "/keepers",
    "/keepers/candidates",
    "/keepers/discovery-search",
    "/my-team",
    "/my-team/upcoming",
    "/picks",
    "/players",
    "/trade",
    "/trades",
    "/transactions",
    "/scoreboard",
    "/cups",
    "/history",
    "/seasons",
    # admin-only (the inner is_admin gate)
    "/admin/health",
    "/admin/season",
    "/admin/standings",
    "/admin/cups",
    "/admin/corrections",
    "/admin/keepers",
    "/admin/audit",
    # parameterised
    f"/team/{OWNER_FPL}",
    "/season/1",
    "/trade/assets/a",
    f"/draft/{SEASON}",
    f"/draft/{SEASON}/board",
    f"/draft/{SEASON}/queue",
    f"/draft/{SEASON}/search",
    f"/discovery/{SEASON}",
    f"/discovery/{SEASON}/board",
    # required query params: /login 422s without manager_id, mapping 400s without `new`
    f"/login?manager_id={OWNER_FPL}",
]

# `/draft-prep` is gated on is_owner(), not on the admin password — an admin cookie does
# NOT open it. Kept separate rather than bent into the list above, because pretending one
# session covers everything is how the sloppy version of this test passed.
OWNER_SESSION_PAGES = ["/draft-prep"]


@pytest.fixture
def seeded(test_session):
    """The minimum every page needs: a current league, a manager, two gameweeks.

    Every non-admin page calls `_league_or_404` (ui.py:35), so without a league row
    thirty-odd routes return 404 and the sweep proves nothing.
    """
    lg = League(fpl_league_id="1", name="Smoke", season_year=SEASON, is_current=True,
                sync_locked=False, phase="in_season")
    test_session.add(lg)
    test_session.flush()
    for n in (1, 2):
        test_session.add(Gameweek(number=n, league_id=lg.id))
    m = Manager(league_id=lg.id, fpl_manager_id=OWNER_FPL, name="Team A",
                display_name="Ann", password_hash=hash_password("pw"))
    test_session.add(m)
    test_session.commit()
    return lg, m


@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


@pytest.fixture
def admin_client(client, seeded, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "smoke-pw")
    assert client.post("/admin/login",
                       data={"password": "smoke-pw"}).status_code == 303
    return client


@pytest.mark.parametrize("path", ADMIN_SESSION_PAGES)
def test_page_renders(admin_client, path):
    r = admin_client.get(path)
    assert r.status_code == 200, (
        f"{path} returned {r.status_code}"
        + (f" -> {r.headers.get('location')}" if r.status_code in (302, 303, 307)
           else f": {r.text[:200]}")
    )


@pytest.mark.parametrize("path", OWNER_SESSION_PAGES)
def test_owner_page_renders(client, seeded, monkeypatch, path):
    """is_owner compares the logged-in manager_id against OWNER_ENTRY_ID."""
    monkeypatch.setenv("OWNER_ENTRY_ID", OWNER_FPL)
    assert client.post("/login", data={"manager_id": OWNER_FPL,
                                       "password": "pw"}).status_code == 303
    r = client.get(path)
    assert r.status_code == 200, f"{path} returned {r.status_code}: {r.text[:200]}"


def test_the_sweep_covers_every_html_route(seeded):
    """The list must not drift behind the app.

    Derived here ONLY to compare against the literal list — the tests above still
    parametrize over the literal, so a page that stops being HTML stops being covered
    loudly rather than silently.
    """
    from main import app

    declared = {
        r.path for r in app.routes
        if "GET" in getattr(r, "methods", set())
        and not r.path.startswith(("/v1", "/static", "/openapi", "/docs", "/redoc"))
        and r.path not in {"/health", "/favicon.ico", "/logout", "/admin/logout",
                           "/demo-login", "/set-password", "/admin/season/mapping"}
    }
    # Normalise our literal paths back to route templates.
    covered = set()
    for p in ADMIN_SESSION_PAGES + OWNER_SESSION_PAGES:
        p = p.split("?")[0]
        for template, concrete in (
            ("/team/{fpl_manager_id}", f"/team/{OWNER_FPL}"),
            ("/season/{fpl_league_id}", "/season/1"),
            ("/trade/assets/{side}", "/trade/assets/a"),
            ("/draft/{year}", f"/draft/{SEASON}"),
            ("/draft/{year}/board", f"/draft/{SEASON}/board"),
            ("/draft/{year}/queue", f"/draft/{SEASON}/queue"),
            ("/draft/{year}/search", f"/draft/{SEASON}/search"),
            ("/discovery/{year}", f"/discovery/{SEASON}"),
            ("/discovery/{year}/board", f"/discovery/{SEASON}/board"),
        ):
            if p == concrete:
                p = template
                break
        covered.add(p)
    missing = declared - covered
    assert not missing, (
        f"these GET routes render HTML but aren't in the sweep: {sorted(missing)}. "
        "Add them to ADMIN_SESSION_PAGES (or OWNER_SESSION_PAGES if owner-gated)."
    )
