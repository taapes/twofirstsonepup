"""Tests for the hard-gate middleware. Run: pytest

Only exercises gate behaviors that don't require the database: unauthenticated
redirects, exempt prefixes/paths, the token bypass, and the HTMX redirect form.
"""

import os

from fastapi.testclient import TestClient

from main import app

client = TestClient(app, follow_redirects=False)


def test_unauthenticated_html_redirects_to_who():
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/who"


def test_health_is_exempt():
    assert client.get("/health").status_code == 200


def test_v1_prefix_is_exempt_from_gate():
    # An unknown /v1 path skips the gate and reaches routing -> 404, not a 303 redirect.
    r = client.get("/v1/this-route-does-not-exist")
    assert r.status_code == 404


def test_valid_token_bypasses_gate():
    os.environ["SYNC_AUTH_TOKEN"] = "test-token-123"
    r = client.get("/nope-not-a-route", headers={"X-Auth-Token": "test-token-123"})
    assert r.status_code == 404  # passed gate, then 404 from routing (not 303)

    r2 = client.get("/nope-not-a-route", headers={"X-Auth-Token": "wrong"})
    assert r2.status_code == 303
    assert r2.headers["location"] == "/who"


def test_htmx_unauthenticated_gets_hx_redirect():
    r = client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert r.headers["HX-Redirect"] == "/who"
