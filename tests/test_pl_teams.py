"""Premier League clubs become rows, and stay the same rows across seasons.

A club is now an ownable asset (the goalie-team rule), so it needs a stable id. The
trap it has to survive is the one `players.code` already survived: FPL's `teams[].id`
is the alphabetical 1-20 index WITHIN a season and is reassigned every August as clubs
go up and down. Matching on it would re-point a historical goalie-team pick at a
different club, so `pl_teams` matches on the permanent `code` and these tests exist to
keep it that way.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import asyncio

import pytest

import sync
from models import PlTeam

GKP, DEF, MID, FWD = 1, 2, 3, 4

# Twenty clubs is the threshold at which membership is rewritten wholesale, so most of
# these fixtures need a full division. (short_name, code) pairs; code is permanent.
DIVISION = [
    ("ARS", 3), ("AVL", 7), ("BOU", 91), ("BRE", 94), ("BHA", 36),
    ("CHE", 8), ("CRY", 31), ("EVE", 11), ("FUL", 54), ("IPS", 40),
    ("LEI", 13), ("LIV", 14), ("MCI", 43), ("MUN", 1), ("NEW", 4),
    ("NFO", 17), ("SOU", 20), ("TOT", 6), ("WHU", 21), ("WOL", 39),
]

# Ipswich down, Sunderland up. Chosen so the reshuffle actually BITES: SUN sorts after
# IPS, so every club between them slides down one `teams[].id` — Leicester's 11 lands
# on Liverpool. A swap like IPS -> LEE would cancel out alphabetically and the
# reassignment test would pass without ever exercising the hazard.
NEXT_SEASON = [p for p in DIVISION if p[0] != "IPS"] + [("SUN", 56)]


def _teams(pairs, drop_code=()):
    """bootstrap `teams[]`. `id` is assigned ALPHABETICALLY, exactly as FPL does it —
    which is why simply reordering `pairs` reshuffles every id."""
    out = []
    for i, (short, code) in enumerate(sorted(pairs), start=1):
        t = {"id": i, "short_name": short, "name": f"{short} FC"}
        if short not in drop_code:
            t["code"] = code
        out.append(t)
    return out


def _feed(teams):
    return {
        "elements": [],
        "element_types": [
            {"id": i, "singular_name_short": s}
            for i, s in {GKP: "GKP", DEF: "DEF", MID: "MID", FWD: "FWD"}.items()
        ],
        "teams": teams,
    }


def _run(payload, monkeypatch):
    async def _get_json(client, url):
        return payload

    monkeypatch.setattr(sync, "_get_json", _get_json)
    asyncio.run(sync.sync_players())


def _by_short(session):
    return {t.short_name: t for t in session.query(PlTeam)}


# --------------------------------------------------------------------------
def test_a_sync_stores_the_division(test_session, monkeypatch):
    _run(_feed(_teams(DIVISION)), monkeypatch)
    test_session.expire_all()

    rows = _by_short(test_session)
    assert len(rows) == 20
    assert rows["ARS"].code == 3 and rows["ARS"].name == "ARS FC"
    assert all(t.is_current_pl for t in rows.values())
    assert rows["ARS"].last_seen_at is not None


def test_resyncing_changes_nothing(test_session, monkeypatch):
    """Idempotence, asserted on the PK: a second sync must not duplicate a club."""
    _run(_feed(_teams(DIVISION)), monkeypatch)
    test_session.expire_all()
    before = {t.short_name: t.id for t in test_session.query(PlTeam)}

    _run(_feed(_teams(DIVISION)), monkeypatch)
    test_session.expire_all()
    after = {t.short_name: t.id for t in test_session.query(PlTeam)}

    assert before == after


def test_a_reassigned_team_id_does_not_re_point_a_row(test_session, monkeypatch):
    """The whole reason `code` is the key.

    Swap Ipswich for Leeds and every club from I onwards shifts its `teams[].id` by
    one. Matching on that id would hand Leicester's row to Liverpool.
    """
    _run(_feed(_teams(DIVISION)), monkeypatch)
    test_session.expire_all()
    arsenal_id = _by_short(test_session)["ARS"].id
    leicester_id = _by_short(test_session)["LEI"].id
    leicester_fpl_before = _by_short(test_session)["LEI"].fpl_id

    _run(_feed(_teams(NEXT_SEASON)), monkeypatch)
    test_session.expire_all()

    rows = _by_short(test_session)
    assert rows["ARS"].id == arsenal_id
    assert rows["LEI"].id == leicester_id, "a club was re-pointed by its seasonal id"
    assert rows["LEI"].code == 13
    # The seasonal id really did move, and onto Liverpool — so this is not a test that
    # passes because nothing happened.
    assert rows["LEI"].fpl_id != leicester_fpl_before
    assert rows["LIV"].fpl_id == leicester_fpl_before


def test_a_relegated_club_keeps_its_row_and_a_promoted_one_reuses_it(
    test_session, monkeypatch
):
    _run(_feed(_teams(DIVISION)), monkeypatch)
    test_session.expire_all()
    ipswich_id = _by_short(test_session)["IPS"].id

    _run(_feed(_teams(NEXT_SEASON)), monkeypatch)
    test_session.expire_all()

    rows = _by_short(test_session)
    assert len(rows) == 21, "a relegated club must keep its history, not be deleted"
    assert rows["IPS"].id == ipswich_id and rows["IPS"].is_current_pl is False
    assert rows["SUN"].is_current_pl is True

    # ...and back up again, into the very same row.
    _run(_feed(_teams(DIVISION)), monkeypatch)
    test_session.expire_all()

    rows = _by_short(test_session)
    assert len(rows) == 21
    assert rows["IPS"].id == ipswich_id and rows["IPS"].is_current_pl is True
    assert rows["SUN"].is_current_pl is False


def test_a_club_with_no_code_is_skipped_not_invented(test_session, monkeypatch):
    """A row with no stable identity is worse than no row — the next sync would
    insert it again under a different id."""
    _run(_feed(_teams(DIVISION, drop_code={"ARS"})), monkeypatch)
    test_session.expire_all()

    rows = _by_short(test_session)
    assert "ARS" not in rows and len(rows) == 19


def test_a_short_payload_does_not_relegate_the_whole_division(
    test_session, monkeypatch
):
    """A truncated or partial feed must not read as 'nineteen clubs went down'."""
    _run(_feed(_teams(DIVISION)), monkeypatch)
    test_session.expire_all()

    _run(_feed(_teams(DIVISION[:3])), monkeypatch)
    test_session.expire_all()

    rows = _by_short(test_session)
    assert len(rows) == 20
    assert sum(1 for t in rows.values() if t.is_current_pl) == 20


def test_an_empty_payload_writes_nothing(test_session, monkeypatch):
    _run(_feed([]), monkeypatch)
    test_session.expire_all()
    assert test_session.query(PlTeam).count() == 0
