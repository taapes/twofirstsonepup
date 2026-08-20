"""The rollover pairs managers by an explicit, confirmed mapping — or refuses.

`advance_season` used to derive the pairing from `managers.fpl_manager_id` and
`continue` on a miss. At the 26/27 rollover FPL reissued every entry id (25/26:
5520-268927; 26/27: a contiguous 58528-58537 block, overlap ZERO), so all three
carries matched nothing and did nothing: ten NULL display names, ten NULL password
hashes — every login broken — and zero keeper seeds against 152 on the old row. It
returned `managers_carried=0, keepers_seeded=0` and wrote that to the audit log. The
information was never the problem; nothing failed on it and nobody looked.

Two changes, tested here. The pairing is now supplied by the commissioner and
VALIDATED — an incomplete one raises rather than silently dropping people, with
`force=True` as the explicit escape hatch for a season where the roster really
changed. And `suggest_manager_pairing` narrows the job without ever deciding it: on
the real 25/26 -> 26/27 names it gets six of ten and leaves four blank.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    Gameweek,
    KeeperSeed,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerSeason,
    Roster,
    Standing,
)
from rules import RuleViolation

_FPL = [0]


@pytest.fixture(autouse=True)
def _reset_ids():
    _FPL[0] = 800
    yield


def _league(session, *, season_year, fpl, is_current=False, locked=False):
    lg = League(fpl_league_id=str(fpl), name=f"S{season_year}",
                season_year=season_year, is_current=is_current,
                sync_locked=locked, phase="preseason", goalie_team_mode="off")
    session.add(lg)
    session.flush()
    return lg


def _mgr(session, lg, *, entry, team, person=None, password=None):
    m = Manager(league_id=lg.id, fpl_manager_id=str(entry), name=team,
                display_name=person, password_hash=password)
    session.add(m)
    session.flush()
    session.add(Standing(league_id=lg.id, manager_id=m.id, rank=1, total=10,
                         points_for=100))
    session.commit()
    return m


def _player(session, lg, name):
    _FPL[0] += 1
    fid = _FPL[0]
    p = Player(name=name, code=fid * 7, fpl_id=fid, position="MID", current_team="ARS")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fid, name=name,
                             position="MID", current_team="ARS"))
    session.commit()
    return p


def _hold(session, lg, mgr, player):
    gws = {g.number: g for g in session.query(Gameweek).filter_by(league_id=lg.id)}
    for n in range(1, 39):
        g = gws.get(n)
        if g is None:
            g = Gameweek(number=n, league_id=lg.id)
            session.add(g)
            session.flush()
            gws[n] = g
        session.add(Roster(manager_id=mgr.id, player_id=player.id, gameweek_id=g.id))
    session.commit()


def _reissued(session):
    """Production's shape: same two people, brand-new entry ids, changed team names."""
    old = _league(session, season_year=2025, fpl=1754, is_current=True)
    o1 = _mgr(session, old, entry=21768, team="Le Roi De Coupe", person="Scott",
              password="pbkdf2$scott")
    o2 = _mgr(session, old, entry=17902, team="Fighting Franckes", person="Kevin F",
              password="pbkdf2$kevinf")
    new = _league(session, season_year=2026, fpl=11818)
    n1 = _mgr(session, new, entry=58532, team="Smashers de Puppies")
    n2 = _mgr(session, new, entry=58531, team="Fighting Franckes")
    return old, {"Scott": o1, "Kevin F": o2}, new, {"Scott": n1, "Kevin F": n2}


# ---- refusing a silent no-op --------------------------------------------------

def test_reissued_entry_ids_are_refused_not_silently_skipped(test_session):
    """The exact production failure. Auto-pairing on entry id matches nothing, and
    that must now be an error rather than a successful-looking no-op."""
    old, _om, new, _nm = _reissued(test_session)
    with pytest.raises(RuleViolation, match="pairing is incomplete"):
        services.advance_season(test_session, old, new)
    test_session.rollback()

    assert [m.display_name for m in
            test_session.query(Manager).filter_by(league_id=new.id)] == [None, None]
    assert test_session.query(League).filter_by(is_current=True).one().id == old.id, \
        "a refused rollover must not flip the current season"


def test_the_refusal_names_everyone_unpaired_on_both_sides(test_session):
    old, _om, new, _nm = _reissued(test_session)
    with pytest.raises(RuleViolation) as exc:
        services.advance_season(test_session, old, new)
    msg = str(exc.value)
    for who in ("Smashers de Puppies", "Fighting Franckes", "Scott", "Kevin F"):
        assert who in msg, f"{who} missing from the refusal"
    test_session.rollback()


def test_an_explicit_pairing_carries_identity_and_clocks(test_session):
    old, om, new, nm = _reissued(test_session)
    kept = _player(test_session, old, "Kept")
    _hold(test_session, old, om["Scott"], kept)
    test_session.add(KeeperSelection(league_id=old.id, manager_id=om["Scott"].id,
                                     player_id=kept.id, season_year=2026))
    test_session.add(KeeperSeed(league_id=old.id, manager_id=om["Scott"].id,
                                player_id=kept.id, years_remaining=3))
    test_session.commit()

    out = services.advance_season(test_session, old, new, pairing={
        nm["Scott"].id: om["Scott"].id,
        nm["Kevin F"].id: om["Kevin F"].id,
    })
    assert out["managers_carried"] == 2
    assert out["keepers_seeded"] == 1
    assert (out["unpaired_new"], out["unpaired_old"]) == ([], [])

    test_session.refresh(nm["Scott"])
    assert nm["Scott"].display_name == "Scott"
    assert nm["Scott"].password_hash == "pbkdf2$scott", "logins must survive"
    seed = test_session.query(KeeperSeed).filter_by(league_id=new.id).one()
    assert seed.manager_id == nm["Scott"].id
    assert seed.years_remaining == 2, "the clock must tick down one"
    assert test_session.query(League).filter_by(is_current=True).one().id == new.id


def test_force_proceeds_and_reports_who_was_dropped(test_session):
    """A season where the roster genuinely changed. Explicit, and recorded."""
    old, om, new, nm = _reissued(test_session)
    extra = _mgr(test_session, old, entry=999, team="Departed FC", person="Steve")

    out = services.advance_season(test_session, old, new, pairing={
        nm["Scott"].id: om["Scott"].id,
        nm["Kevin F"].id: om["Kevin F"].id,
    }, force=True)
    assert out["unpaired_old"] == ["Steve"]
    assert out["unpaired_new"] == []
    assert out["managers_carried"] == 2
    assert extra.display_name == "Steve"


def test_force_is_recorded_in_the_audit(test_session):
    from models import AuditLog

    old, om, new, nm = _reissued(test_session)
    _mgr(test_session, old, entry=999, team="Departed FC", person="Steve")
    services.advance_season(test_session, old, new, pairing={
        nm["Scott"].id: om["Scott"].id,
        nm["Kevin F"].id: om["Kevin F"].id,
    }, force=True)
    entry = test_session.query(AuditLog).filter_by(action="season.rollover").one()
    assert "FORCED" in entry.summary and "Steve" in entry.summary
    assert entry.details["forced"] is True


def test_entry_id_matching_still_works_when_ids_do_carry(test_session):
    """Backwards compatibility: pairing=None auto-pairs on entry id. That path is now
    VALIDATED rather than trusted, but it must still succeed when it genuinely can."""
    old = _league(test_session, season_year=2025, fpl=1, is_current=True)
    o = _mgr(test_session, old, entry=7, team="Same FC", person="Scott",
             password="pbkdf2$x")
    new = _league(test_session, season_year=2026, fpl=2)
    n = _mgr(test_session, new, entry=7, team="Same FC Renamed")

    out = services.advance_season(test_session, old, new)
    assert out["managers_carried"] == 1
    test_session.refresh(n)
    assert n.display_name == "Scott" and n.password_hash == "pbkdf2$x"
    assert o.id != n.id


def test_rerunning_fills_blanks_only(test_session):
    """Idempotent: a name or password set since the rollover must not be clobbered."""
    old, om, new, nm = _reissued(test_session)
    pairing = {nm["Scott"].id: om["Scott"].id, nm["Kevin F"].id: om["Kevin F"].id}
    services.advance_season(test_session, old, new, pairing=pairing)
    nm["Scott"].display_name = "Scotty"
    nm["Scott"].password_hash = "pbkdf2$changed"
    test_session.commit()

    services.advance_season(test_session, old, new, pairing=pairing)
    test_session.refresh(nm["Scott"])
    assert nm["Scott"].display_name == "Scotty"
    assert nm["Scott"].password_hash == "pbkdf2$changed"


# ---- the suggestion ------------------------------------------------------------

def test_the_suggestion_matches_an_unchanged_team_name(test_session):
    old, om, new, nm = _reissued(test_session)
    out = services.suggest_manager_pairing(test_session, old, new)
    assert out[nm["Kevin F"].id] == om["Kevin F"].id


def test_the_suggestion_leaves_a_renamed_team_blank_rather_than_guessing(test_session):
    """"Le Roi De Coupe" -> "Smashers de Puppies" is not derivable. A confident wrong
    guess is worse than a blank: the commissioner would tab past it."""
    old, _om, new, nm = _reissued(test_session)
    out = services.suggest_manager_pairing(test_session, old, new)
    assert out[nm["Scott"].id] is None


def test_the_suggestion_handles_the_real_shapes(test_session):
    """The six that were legible from this year's actual data."""
    old = _league(test_session, season_year=2025, fpl=1, is_current=True)
    new = _league(test_session, season_year=2026, fpl=2)
    cases = [
        ("Sid Hefty +III", "Sid Hefty", "Kevin T"),
        ("🐶☕️🤴", "Culver City HS🐶☕️🤴", "Tucker"),
        ("Pep’s Scraps", "Pep’s Scraps", "Michael"),
    ]
    olds, news = {}, {}
    for i, (o_team, n_team, person) in enumerate(cases):
        olds[person] = _mgr(test_session, old, entry=100 + i, team=o_team,
                            person=person)
        news[person] = _mgr(test_session, new, entry=200 + i, team=n_team)

    out = services.suggest_manager_pairing(test_session, old, new)
    for _o, _n, person in cases:
        assert out[news[person].id] == olds[person].id, f"{person} not suggested"


def test_one_old_manager_is_never_suggested_twice(test_session):
    """Greedy on best score: two similar new names must not both claim one person."""
    old = _league(test_session, season_year=2025, fpl=1, is_current=True)
    o = _mgr(test_session, old, entry=1, team="Fighting Franckes", person="Kevin F")
    new = _league(test_session, season_year=2026, fpl=2)
    n1 = _mgr(test_session, new, entry=11, team="Fighting Franckes")
    n2 = _mgr(test_session, new, entry=12, team="Fighting Francke")

    out = services.suggest_manager_pairing(test_session, old, new)
    claimed = [k for k, v in out.items() if v == o.id]
    assert len(claimed) == 1
    assert claimed[0] == n1.id, "the exact match should win"
    assert out[n2.id] is None


def test_the_suggestion_is_never_applied_on_its_own(test_session):
    """Same rule as the discovery-pick matcher, for the same reason: a wrong pairing
    hands one manager another's whole season. Calling it must not write anything."""
    old, _om, new, _nm = _reissued(test_session)
    services.suggest_manager_pairing(test_session, old, new)
    assert [m.display_name for m in
            test_session.query(Manager).filter_by(league_id=new.id)] == [None, None]
    with pytest.raises(RuleViolation):
        services.advance_season(test_session, old, new)
    test_session.rollback()


REAL_2026 = [
    # (25/26 team, 26/27 team, person) — the actual names, verified 2026-08-19.
    ("Booyaka's Forest 🌳",   "Booyaka Boys",           "Gaby"),
    ("João Puppy’s Stable",  "João’s Absolute Dogs",   "John"),
    ("Fighting Franckes",    "Fighting Franckes",      "Kevin F"),
    ("Wünder🐶s",            "Woofs for Roefs",        "Kevin S"),
    ("Sid Hefty +III",       "Sid Hefty",              "Kevin T"),
    ("The Expansion Rival",  "Le Féez Nuts",           "Mark"),
    ("Pep’s Scraps",         "Pep’s Scraps",           "Michael"),
    ("Le Roi De Coupe",      "Smashers de Puppies",    "Scott"),
    ("Pts DeductionFC-3/6",  "Kerkez du Soleil",       "Steve"),
    ("🐶☕️🤴",                "Culver City HS🐶☕️🤴",     "Tucker"),
]


def test_the_suggestion_is_never_confidently_wrong_on_real_data(test_session):
    """Measured against the actual 25/26 -> 26/27 team names.

    The number that matters is WRONG=0. A blank costs the commissioner ten seconds;
    a confident wrong pairing is the failure mode of the whole page, because it is
    the one they would tab straight past — and it hands one manager another's logins
    and keeper clocks, exactly what this step exists to prevent.

    Six of ten is fine. It is a labour-saver, not an oracle.
    """
    old = _league(test_session, season_year=2025, fpl=1, is_current=True)
    new = _league(test_session, season_year=2026, fpl=2)
    olds, news = {}, {}
    for i, (o_team, n_team, person) in enumerate(REAL_2026):
        olds[person] = _mgr(test_session, old, entry=1000 + i, team=o_team,
                            person=person)
        news[person] = _mgr(test_session, new, entry=58528 + i, team=n_team)

    out = services.suggest_manager_pairing(test_session, old, new)
    by_old = {m.id: person for person, m in olds.items()}
    wrong, correct = [], 0
    for person, nm in news.items():
        got = out.get(nm.id)
        if got is None:
            continue
        if by_old.get(got) == person:
            correct += 1
        else:
            wrong.append(f"{nm.name} -> {by_old.get(got)} (should be {person})")

    assert wrong == [], "a confident wrong pairing is worse than a blank"
    assert correct >= 6, f"only {correct}/10 suggested; the page stops earning its keep"


# ---- caught by the Neon-branch rehearsal, 2026-08-20 -------------------------

def test_the_confirm_route_awaits_the_post_rollover_sync(test_session, monkeypatch):
    """`admin_season_mapping_confirm` is `async def` (it awaits request.form()), and
    calling `asyncio.run()` inside a running event loop raises RuntimeError EVERY
    time. So the rollover committed and then always returned 502 "post-rollover sync
    failed", leaving the new season current with no rosters or player_season until
    someone ran /admin/sync?force=1 by hand.

    Unit tests could not catch this — it needs a real ASGI event loop, which is why
    it survived until the rehearsal. Hence a route-level test.
    """
    import sync
    from fastapi.testclient import TestClient
    from main import app

    old, om, new, nm = _reissued(test_session)

    called = []

    async def _fake_sync_all(fpl_league_id=None, **kw):
        called.append(fpl_league_id)

    monkeypatch.setattr(sync, "sync_all", _fake_sync_all)
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")

    c = TestClient(app, follow_redirects=False)
    c.post("/admin/login", data={"password": "pw"})
    r = c.post("/admin/season/mapping", data={
        "new_fpl": new.fpl_league_id,
        f"pair[{nm['Scott'].id}]": str(om["Scott"].id),
        f"pair[{nm['Kevin F'].id}]": str(om["Kevin F"].id),
    })

    assert r.status_code == 303, f"expected a redirect, got {r.status_code}: {r.text[:200]}"
    assert "carried=2" in (r.headers.get("location") or "")
    assert called == [new.fpl_league_id], "the post-rollover sync must actually run"


def test_snapshot_player_pool_actually_captures_a_pool(test_session):
    """It never imported `PlayerPoolSnapshot` — the name is imported function-locally
    in flag_ineligible and advance_season, neither in scope here — so every call
    raised NameError. Pre-existing and older than the rollover work.

    The failure was silent rather than loud: no pool was captured for any season, and
    flag_ineligible returns 0 on an empty snapshot BY DESIGN, so the
    ineligible-player rule has never fired. Confirmed against real data — 0 snapshot
    rows for every season.
    """
    from models import PlayerPoolSnapshot

    lg = _league(test_session, season_year=2026, fpl=1, is_current=True)
    _mgr(test_session, lg, entry=1, team="T", person="A")
    _player(test_session, lg, "Someone")
    _player(test_session, lg, "Somebody Else")

    n = services.snapshot_player_pool(test_session, lg)
    assert n == 2, "no pool captured"
    assert test_session.query(PlayerPoolSnapshot).filter_by(league_id=lg.id).count() == 2
    # idempotent — a second run adds nothing
    assert services.snapshot_player_pool(test_session, lg) == 0
