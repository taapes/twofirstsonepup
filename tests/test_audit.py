"""The audit log: actor propagation, atomicity, filtering, and the admin page.

CONVERTED 2026-08-31 off the production database. This file sat on conftest's
COMMITTING_DB_MODULES list, skipped by default, on the stated grounds that it tests
"code that commits internally" and that a rollback-based fixture could not cover it.
Neither holds: `services.record_audit` explicitly does NOT commit (services.py:184-188 —
it joins the caller's transaction so the audit row is atomic with the change), and
`test_session` is not rollback-based — it TRUNCATEs on teardown and tolerates commits
freely.

The real blocker was `from db import SessionLocal` at module scope. That copies the
sessionmaker into this module's namespace, where `monkeypatch.setattr(db,
"SessionLocal", ...)` can never reach it — the same trap conftest documents for sync.py.
Combined with reading pre-existing rows off `.first()`, it needed a populated production
database. Seeding removes both problems.

Nothing here deletes rows any more: the fixture truncates.
"""

import pytest

import audit
import services
from models import AuditLog, Fine, Gameweek, League, Manager


@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, follow_redirects=False)


@pytest.fixture
def league_and_manager(test_session):
    """A current league with one manager. Returns (league, fpl_manager_id, manager_id)."""
    lg = League(fpl_league_id="1", name="Audit", season_year=2026, is_current=True,
                sync_locked=False, phase="in_season")
    test_session.add(lg)
    test_session.flush()
    test_session.add(Gameweek(number=1, league_id=lg.id))
    m = Manager(league_id=lg.id, fpl_manager_id="77", name="T", display_name="Tucker")
    test_session.add(m)
    test_session.commit()
    return lg, m.fpl_manager_id, str(m.id)


# ---- the actor ContextVar (no database) ----
def test_actor_default_is_system():
    assert audit.current_actor() == ("system", "system")


def test_set_and_reset_actor():
    tok = audit.set_actor("Tucker", "manager")
    assert audit.current_actor() == ("Tucker", "manager")
    audit.reset_actor(tok)
    assert audit.current_actor() == ("system", "system")


# ---- record_audit ----
def test_record_audit_captures_the_actor(test_session, league_and_manager):
    lg, _fpl, mid = league_and_manager
    services.record_audit(test_session, lg, action="test.system", summary="sys op")
    tok = audit.set_actor("Tucker", "manager")
    services.record_audit(test_session, lg, action="test.manager", summary="mgr op",
                          manager_ids=[mid])
    audit.reset_actor(tok)
    test_session.commit()

    rows = test_session.query(AuditLog).filter(AuditLog.league_id == lg.id).all()
    kinds = {r.action: r.actor_kind for r in rows}
    assert kinds["test.system"] == "system"
    assert kinds["test.manager"] == "manager"
    mrow = next(r for r in rows if r.action == "test.manager")
    assert mrow.manager_ids == [mid], "stored as a string list"
    assert mrow.actor == "Tucker"


def test_record_audit_rolls_back_with_its_caller(test_session, league_and_manager):
    """The reason record_audit doesn't commit: the audit row must be atomic with the
    change it describes, so a caller that aborts leaves no trace of a change that
    never happened."""
    lg, _fpl, _mid = league_and_manager
    services.record_audit(test_session, lg, action="test.rollback",
                          summary="never committed")
    test_session.rollback()
    assert test_session.query(AuditLog).filter_by(
        league_id=lg.id, action="test.rollback").count() == 0


# ---- get_audit_log filtering ----
def test_get_audit_log_filters_by_action_and_manager(test_session, league_and_manager):
    lg, fpl, mid = league_and_manager
    services.record_audit(test_session, lg, action="test.forteam",
                          summary="affects a team", manager_ids=[mid])
    services.record_audit(test_session, lg, action="test.noteam", summary="no team")
    test_session.commit()

    by_action = services.get_audit_log(test_session, lg, action="test.forteam")
    assert by_action and all(e["action"] == "test.forteam" for e in by_action)

    by_mgr = {e["action"] for e in services.get_audit_log(test_session, lg,
                                                          manager_fpl_id=fpl)}
    assert "test.forteam" in by_mgr
    assert "test.noteam" not in by_mgr, \
        "a row with no manager_ids is not part of any per-team view"


# ---- route gating ----
def test_audit_page_requires_a_login(client):
    """Anonymous hits the site-wide gate, which is the OUTER of the two — so this is a
    redirect to /who, never a 200."""
    assert client.get("/admin/audit").status_code in (303, 307)


# ---- end-to-end actor propagation through the middleware ----
def test_an_admin_action_is_logged_as_admin(client, test_session, league_and_manager,
                                            monkeypatch):
    """The whole chain: ActorMiddleware sets the ContextVar from the session, the
    service reads it, and the row lands with actor_kind='admin'."""
    lg, fpl, mid = league_and_manager
    monkeypatch.setenv("ADMIN_PASSWORD", "audit-test-pw")
    assert client.post("/admin/login",
                       data={"password": "audit-test-pw"}).status_code == 303

    r = client.post("/admin/fines/add",
                    data={"fpl_manager_id": fpl, "amount": "1", "reason": "AUDITTEST"})
    assert r.status_code in (200, 303), r.text

    test_session.expire_all()
    row = (
        test_session.query(AuditLog)
        .filter_by(league_id=lg.id, action="fine.add")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None
    assert row.actor_kind == "admin"
    assert mid in (row.manager_ids or [])
    assert (row.details or {}).get("reason") == "AUDITTEST"
    assert test_session.query(Fine).filter_by(league_id=lg.id).count() == 1

    page = client.get("/admin/audit")
    assert page.status_code == 200
    assert b"fine.add" in page.content
