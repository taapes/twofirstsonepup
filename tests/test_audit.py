"""Audit log: actor capture (ContextVar), record_audit atomicity, get_audit_log
filtering, and end-to-end actor propagation through the middleware. Run: pytest

These hit the configured DB (services commit internally), so every test deletes the
rows it creates — rollback can't undo a committed service write.
"""

import os

from fastapi.testclient import TestClient

import audit
import services
from db import SessionLocal
from main import app
from models import AuditLog, Fine, League, Manager

client = TestClient(app, follow_redirects=False)


def _league():
    db = SessionLocal()
    try:
        return db.query(League).filter_by(is_current=True).first() or db.query(League).first()
    finally:
        db.close()


def _a_manager():
    db = SessionLocal()
    try:
        lg = _league()
        m = db.query(Manager).filter_by(league_id=lg.id).first()
        return m.fpl_manager_id, str(m.id)
    finally:
        db.close()


# ---- ContextVar actor ----
def test_actor_default_is_system():
    assert audit.current_actor() == ("system", "system")


def test_set_and_reset_actor():
    tok = audit.set_actor("Tucker", "manager")
    assert audit.current_actor() == ("Tucker", "manager")
    audit.reset_actor(tok)
    assert audit.current_actor() == ("system", "system")


# ---- record_audit ----
def test_record_audit_atomic_and_captures_actor():
    db = SessionLocal()
    created: list = []
    try:
        lg = db.query(League).filter_by(id=_league().id).one()
        _fpl, mid = _a_manager()
        services.record_audit(db, lg, action="test.system", summary="sys op")
        tok = audit.set_actor("Tucker", "manager")
        services.record_audit(db, lg, action="test.manager", summary="mgr op", manager_ids=[mid])
        audit.reset_actor(tok)
        db.commit()
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.league_id == lg.id, AuditLog.action.in_(["test.system", "test.manager"]))
            .all()
        )
        created = [r.id for r in rows]
        kinds = {r.action: r.actor_kind for r in rows}
        assert kinds["test.system"] == "system"
        assert kinds["test.manager"] == "manager"
        mrow = next(r for r in rows if r.action == "test.manager")
        assert mrow.manager_ids == [mid]  # stored as string list
        assert mrow.actor == "Tucker"
    finally:
        for rid in created:
            db.query(AuditLog).filter_by(id=rid).delete()
        db.commit()
        db.close()


def test_record_audit_rolls_back_with_caller():
    db = SessionLocal()
    try:
        lg = db.query(League).filter_by(id=_league().id).one()
        services.record_audit(db, lg, action="test.rollback", summary="never committed")
        db.rollback()
        n = db.query(AuditLog).filter_by(league_id=lg.id, action="test.rollback").count()
        assert n == 0
    finally:
        db.close()


# ---- get_audit_log filtering ----
def test_get_audit_log_filters_by_action_and_manager():
    db = SessionLocal()
    created: list = []
    try:
        lg = db.query(League).filter_by(id=_league().id).one()
        fpl, mid = _a_manager()
        services.record_audit(db, lg, action="test.forteam", summary="affects a team", manager_ids=[mid])
        services.record_audit(db, lg, action="test.noteam", summary="no team")
        db.commit()
        created = [
            r.id for r in db.query(AuditLog)
            .filter(AuditLog.league_id == lg.id, AuditLog.action.in_(["test.forteam", "test.noteam"]))
        ]
        by_action = services.get_audit_log(db, lg, action="test.forteam")
        assert by_action and all(e["action"] == "test.forteam" for e in by_action)
        by_mgr = {e["action"] for e in services.get_audit_log(db, lg, manager_fpl_id=fpl)}
        assert "test.forteam" in by_mgr
        assert "test.noteam" not in by_mgr  # has no manager_ids → excluded from per-team view
    finally:
        for rid in created:
            db.query(AuditLog).filter_by(id=rid).delete()
        db.commit()
        db.close()


# ---- route gating ----
def test_audit_page_requires_admin():
    # not logged in → gate redirects (to /who); never 200
    assert client.get("/admin/audit").status_code in (303, 307)


# ---- end-to-end actor propagation through the middleware ----
def test_admin_action_logs_actor_as_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "audit-test-pw")
    fpl, mid = _a_manager()
    c = TestClient(app, follow_redirects=False)
    c.post("/admin/login", data={"password": "audit-test-pw"})
    r = c.post("/admin/fines/add", data={"fpl_manager_id": fpl, "amount": "1", "reason": "AUDITTEST"})
    assert r.status_code in (200, 303)
    db = SessionLocal()
    try:
        lg = db.query(League).filter_by(id=_league().id).one()
        row = (
            db.query(AuditLog)
            .filter_by(league_id=lg.id, action="fine.add")
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        assert row is not None
        assert row.actor_kind == "admin"  # middleware → ContextVar → service
        assert mid in (row.manager_ids or [])
        assert (row.details or {}).get("reason") == "AUDITTEST"
        # the audit page shows it now that we're admin
        page = c.get("/admin/audit")
        assert page.status_code == 200
        assert b"fine.add" in page.content
    finally:
        # remove the test fine + its audit row (delete directly so we don't log a deletion)
        db.query(Fine).filter_by(league_id=lg.id, reason="AUDITTEST").delete()
        db.query(AuditLog).filter_by(league_id=lg.id, action="fine.add").filter(
            AuditLog.details["reason"].astext == "AUDITTEST"
        ).delete(synchronize_session=False)
        db.commit()
        db.close()
