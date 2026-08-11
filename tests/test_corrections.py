"""Commissioner corrections: editing and deleting historical records.

Until now nothing in the app could change a trade or a pick once written — fixing a
wrong record meant editing the database by hand. These are the services behind
/admin/corrections. Every one writes an audit entry carrying the PREVIOUS values,
because "what did it used to say" is the point of a correction log.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import asyncio

import pytest

import services
import sync
from models import AuditLog, DiscoveryResult, DraftPick, League, Manager, Player, Trade
from rules import RuleViolation


def _league(session):
    lg = League(fpl_league_id="1", name="S", season_year=2025, is_current=True,
                sync_locked=False, phase="offseason")
    session.add(lg)
    session.flush()
    return lg


def _mgr(session, league, fpl, name):
    m = Manager(league_id=league.id, fpl_manager_id=fpl, name=name, display_name=name)
    session.add(m)
    session.flush()
    return m


def _audit(session, action):
    return (
        session.query(AuditLog).filter_by(action=action)
        .order_by(AuditLog.created_at.desc()).first()
    )


# ---- trades ---------------------------------------------------------------
def test_edit_trade_corrects_direction_and_records_the_old_value(test_session):
    lg = _league(test_session)
    a, b = _mgr(test_session, lg, "1", "Ann"), _mgr(test_session, lg, "2", "Bob")
    t = Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id, event_gw=5)
    test_session.add(t)
    test_session.commit()

    services.edit_trade(test_session, lg, str(t.id), from_fpl="2", to_fpl="1",
                        event_gw=9)
    test_session.expire_all()

    row = test_session.get(Trade, t.id)
    assert (row.from_manager, row.to_manager, row.event_gw) == (b.id, a.id, 9)
    assert row.manually_edited is True, "sync would otherwise undo this"

    log = _audit(test_session, "trade.edit")
    assert log is not None
    prev = log.details["previous"]
    assert prev["from_manager"] == str(a.id) and prev["event_gw"] == "5"


def test_edit_trade_rejects_a_trade_with_itself(test_session):
    lg = _league(test_session)
    a, b = _mgr(test_session, lg, "1", "Ann"), _mgr(test_session, lg, "2", "Bob")
    t = Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id)
    test_session.add(t)
    test_session.commit()

    with pytest.raises(RuleViolation):
        services.edit_trade(test_session, lg, str(t.id), to_fpl="1")


def test_delete_trade_audits_before_removing(test_session):
    lg = _league(test_session)
    a, b = _mgr(test_session, lg, "1", "Ann"), _mgr(test_session, lg, "2", "Bob")
    t = Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id, event_gw=7)
    test_session.add(t)
    test_session.commit()
    tid = t.id

    services.delete_trade(test_session, lg, str(tid))
    assert test_session.get(Trade, tid) is None
    log = _audit(test_session, "trade.delete")
    assert log is not None and log.details["previous"]["event_gw"] == "7"


def test_missing_rows_raise_rather_than_pass_silently(test_session):
    lg = _league(test_session)
    test_session.commit()
    missing = "00000000-0000-0000-0000-000000000000"
    for fn in (services.delete_trade, services.delete_discovery_result,
               services.delete_draft_pick):
        with pytest.raises(RuleViolation):
            fn(test_session, lg, missing)


def test_a_corrected_trade_survives_a_sync(test_session, monkeypatch):
    """The trap. sync_trades reconciles on an exact (player, from, to) triple and
    upserts event_gw on top — so a corrected direction would come back as a DUPLICATE
    and a corrected gameweek would be rewritten. The edit flag has to stop both."""
    lg = _league(test_session)
    a, b = _mgr(test_session, lg, "1", "Ann"), _mgr(test_session, lg, "2", "Bob")
    p = Player(name="Gabriel", code=111, fpl_id=5, position="DEF")
    test_session.add(p)
    test_session.flush()
    # as the feed reported it, then corrected by hand: direction flipped, GW moved
    t = Trade(league_id=lg.id, from_manager=a.id, to_manager=b.id, player_id=p.id,
              event_gw=5, fpl_trade_id="T1")
    test_session.add(t)
    test_session.commit()
    services.edit_trade(test_session, lg, str(t.id), from_fpl="2", to_fpl="1",
                        event_gw=9)

    async def _feed(client, url):
        # Shape matters: sync_trades skips anything whose state isn't "p", and reads
        # the moves out of tradeitem_set. element_in moves INTO the offering team.
        return {"trades": [{
            "id": "T1", "event": 5, "state": "p",
            "offered_entry": 1, "received_entry": 2,
            "tradeitem_set": [{"element_in": 5, "element_out": None}],
        }]}

    monkeypatch.setattr(sync, "_get_json", _feed)
    asyncio.run(sync.sync_trades(fpl_league_id="1"))
    test_session.expire_all()

    rows = test_session.query(Trade).filter_by(league_id=lg.id).all()
    assert len(rows) == 1, f"sync duplicated a corrected trade ({len(rows)} rows)"
    assert (rows[0].from_manager, rows[0].to_manager) == (b.id, a.id)
    assert rows[0].event_gw == 9, "sync rewrote the corrected gameweek"


# ---- discovery + draft picks ---------------------------------------------
def test_edit_discovery_result_fixes_free_text(test_session):
    lg = _league(test_session)
    d = DiscoveryResult(league_id=lg.id, season="24/25", round=1, pick_number=1,
                        manager_name="Wrong", player_name="Also Wrong")
    test_session.add(d)
    test_session.commit()

    services.edit_discovery_result(test_session, lg, str(d.id),
                                   manager_name="John", player_name="Harry Kane")
    test_session.expire_all()
    row = test_session.get(DiscoveryResult, d.id)
    assert (row.manager_name, row.player_name) == ("John", "Harry Kane")
    log = _audit(test_session, "discovery.edit")
    assert log.details["previous"]["manager_name"] == "Wrong"


def test_delete_discovery_result(test_session):
    lg = _league(test_session)
    d = DiscoveryResult(league_id=lg.id, season="24/25", round=1, pick_number=1,
                        manager_name="X", player_name="Y")
    test_session.add(d)
    test_session.commit()
    did = d.id

    services.delete_discovery_result(test_session, lg, str(did))
    assert test_session.get(DiscoveryResult, did) is None
    assert _audit(test_session, "discovery.delete") is not None


def test_delete_draft_pick_frees_the_slot(test_session):
    lg = _league(test_session)
    m = _mgr(test_session, lg, "1", "Ann")
    p = Player(name="Gabriel", code=111, fpl_id=5, position="DEF")
    test_session.add(p)
    test_session.flush()
    pick = DraftPick(league_id=lg.id, season_year=2026, draft_type="main", round=1,
                     pick_number=1, manager_id=m.id, player_id=p.id)
    test_session.add(pick)
    test_session.commit()
    pid = pick.id

    services.delete_draft_pick(test_session, lg, str(pid))
    assert test_session.get(DraftPick, pid) is None
    log = _audit(test_session, "pick.delete")
    assert log is not None and log.details["previous"]["pick_number"] == "1"


def test_record_pick_overwrite_lets_an_admin_correct_a_filled_slot(test_session):
    """record_pick has always had an `overwrite` flag; nothing ever passed it, so a
    wrong pick was unfixable."""
    lg = _league(test_session)
    m = _mgr(test_session, lg, "1", "Ann")
    right = Player(name="Right", code=111, fpl_id=5, position="DEF")
    wrong = Player(name="Wrong", code=222, fpl_id=6, position="MID")
    test_session.add_all([right, wrong])
    test_session.commit()

    services.record_pick(test_session, lg, season_year=2026, pick_number=1,
                         owner_fpl="1", player_fpl_id=6, round=1)
    with pytest.raises(RuleViolation):
        services.record_pick(test_session, lg, season_year=2026, pick_number=1,
                             owner_fpl="1", player_fpl_id=5, round=1)
    services.record_pick(test_session, lg, season_year=2026, pick_number=1,
                         owner_fpl="1", player_fpl_id=5, round=1, overwrite=True)
    test_session.expire_all()

    row = test_session.query(DraftPick).filter_by(league_id=lg.id).one()
    assert row.player_id == right.id
