"""A discovery-draft acquisition is worth a draft-length keeper clock, not a waiver one.

The September discovery draft is an ACQUISITION, but it leaves no trace in the two
things `_derive_keeper_status` actually reads. A player taken in September joins the
Premier League in January, so he is on nobody's GW1 roster (`started_with_manager` is
False) and there is no `Trade` row — and `rules.keeper_status` therefore falls through
to `("waiver", 3)`. He loses a keeper year purely because the evidence of how he
arrived was never written down anywhere the derivation looks.

Two independent witnesses now supply it, because neither alone covers every caller:

  - a **linked discovery DraftPick** (`services.link_discovery_pick`) — public draft
    history under no privacy gate, so it works for every caller including the
    viewer-less ones;
  - the manager's own **is_discovery KeeperSelection** — behind the keeper privacy
    gate, so it is invisible to a viewer-less caller (`submit_keepers` among them,
    which is why that function synthesizes the label itself as well).

Linking is always an explicit admin action. `Player.name` is FPL's short `web_name`
while a manager types a full name, so name-matching is a coin flip, and a wrong link
silently hands one manager another's keeper on a 4-year clock.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import pytest

import services
from models import (
    DraftPick,
    Gameweek,
    KeeperSeed,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerSeason,
    PlTeam,
    Roster,
    Standing,
    Trade,
)
from rules import KEEPER_FRESH_DRAFT, KEEPER_FRESH_WAIVER, RuleViolation

SEASON = 2026
UPCOMING = SEASON + 1
ALL_GWS = range(1, 39)
# A discovery pick joins the PL in January, so he first appears mid-season. This is
# the whole shape of the bug: no GW1 presence, no trade, no drop.
JANUARY_GWS = range(20, 39)


def _seed(session, *, mode="off", season=SEASON, is_current=True):
    lg = League(fpl_league_id=str(season), name=f"S{season}", season_year=season,
                is_current=is_current, sync_locked=False, phase="offseason",
                goalie_team_mode=mode)
    session.add(lg)
    session.flush()
    gws = {}
    for n in ALL_GWS:
        g = Gameweek(number=n, league_id=lg.id)
        session.add(g)
        session.flush()
        gws[n] = g
    mgrs = {}
    for i, name in enumerate(["A", "B"], start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=str(i), name=name, display_name=name)
        session.add(m)
        session.flush()
        session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i,
                             total=100 - i, points_for=1000 - i))
        mgrs[name] = m
    session.commit()
    return lg, mgrs, gws


def _player(session, lg, name, fpl_id, pos="MID"):
    p = Player(name=name, code=fpl_id * 7, fpl_id=fpl_id, position=pos,
               current_team="ARS", price=50, status="a")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id,
                             name=name, position=pos, current_team="ARS"))
    session.commit()
    return p


def _rostered(session, mgr, player, gws, over=ALL_GWS):
    for n in over:
        session.add(Roster(manager_id=mgr.id, player_id=player.id,
                           gameweek_id=gws[n].id))
    session.commit()


def _discovery_pick(session, lg, mgr, label, *, pick_number=1, season=SEASON,
                    player=None):
    session.add(DraftPick(
        league_id=lg.id, season_year=season, draft_type="discovery",
        round=1, pick_number=pick_number, manager_id=mgr.id,
        player_id=player.id if player else None,
        player_label=label, source="discovery",
    ))
    session.commit()


# ---- the derivation: a linked pick supplies the missing acquisition -----------

def test_a_linked_discovery_pick_gives_a_four_year_clock(test_session):
    """The core fix. No keeper selection submitted at all — the label comes purely
    from draft history, which is what makes it work for a viewer-less caller."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade", player=p)

    status = services._derive_keeper_status(test_session, lg)
    st = status[mgrs["A"].id][p.id]
    assert st["acquisition"] == "discovery"
    assert st["years_remaining"] == KEEPER_FRESH_DRAFT
    assert st["eligible"] is True


def test_without_the_link_he_is_still_a_waiver_pickup(test_session):
    """The control that proves the test above is measuring the link and not something
    incidental: the identical roster shape with an UNLINKED pick derives the bug."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade", player=None)

    status = services._derive_keeper_status(test_session, lg)
    st = status[mgrs["A"].id][p.id]
    assert st["acquisition"] == "waiver"
    assert st["years_remaining"] == KEEPER_FRESH_WAIVER


def test_a_linked_pick_for_another_manager_does_not_leak(test_session):
    """The map is keyed on (manager, player). B's discovery pick must not relabel the
    same player on A's roster."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    _discovery_pick(test_session, lg, mgrs["B"], "Nick Woltemade", player=p)

    status = services._derive_keeper_status(test_session, lg)
    assert status[mgrs["A"].id][p.id]["acquisition"] == "waiver"


def test_a_seed_still_beats_a_linked_pick(test_session):
    """Same precedence the off-roster discovery path already pins
    (test_discovery_keeper_slot.py): the commissioner's override wins over every
    derived value, and the linked pick is just another derived value."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade", player=p)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=mgrs["A"].id,
                                player_id=p.id, years_remaining=1,
                                acquisition="waiver"))
    test_session.commit()

    st = services._derive_keeper_status(test_session, lg)[mgrs["A"].id][p.id]
    assert (st["acquisition"], st["years_remaining"]) == ("waiver", 1)


def test_a_dropped_and_reacquired_discovery_player_is_a_waiver_pickup(test_session):
    """The label may only ADD the story the roster can't tell, never erase one it
    can. A player genuinely dropped and re-acquired went through the open wire, and
    `acquisition=` short-circuits rules.keeper_status's dropped branch entirely — so
    without this gate a linked pick would launder a real drop clean forever."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    # held Jan-Feb, dropped, re-acquired in April: a gap inside his own tenure
    _rostered(test_session, mgrs["A"], p, gws, over=[20, 21, 22, 33, 34, 35, 36, 37, 38])
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade", player=p)

    st = services._derive_keeper_status(test_session, lg)[mgrs["A"].id][p.id]
    assert st["acquisition"] == "waiver"
    assert st["years_remaining"] == KEEPER_FRESH_WAIVER


def test_the_discovery_label_survives_a_trade_with_the_senders_clock(test_session):
    """A trade changes ownership and nothing else — rules.keeper_status returns
    `traded_from` verbatim, so 'discovery' chains through untouched."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    # A held him from January until the trade; B holds him after it.
    _rostered(test_session, mgrs["A"], p, gws, over=range(20, 30))
    _rostered(test_session, mgrs["B"], p, gws, over=range(30, 39))
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade", player=p)
    test_session.add(Trade(league_id=lg.id, from_manager=mgrs["A"].id,
                           to_manager=mgrs["B"].id, player_id=p.id, event_gw=30))
    test_session.commit()

    st = services._derive_keeper_status(test_session, lg)[mgrs["B"].id][p.id]
    assert st["acquisition"] == "discovery"
    assert st["years_remaining"] == KEEPER_FRESH_DRAFT


def test_a_pick_on_an_older_league_row_still_bridges(test_session):
    """Manager rows are per-season. A pick made before a rollover carries the OUTGOING
    row's manager uuid, so a raw managers.id key would silently miss it — the same
    hazard _goalie_team_history documents."""
    old, old_mgrs, _old_gws = _seed(test_session, season=SEASON - 1, is_current=False)
    lg, mgrs, gws = _seed(test_session, season=SEASON)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    # recorded last season, on last season's league row and manager row
    _discovery_pick(test_session, old, old_mgrs["A"], "Nick Woltemade",
                    season=SEASON - 1, player=p)

    st = services._derive_keeper_status(test_session, lg)[mgrs["A"].id][p.id]
    assert st["acquisition"] == "discovery"
    assert st["years_remaining"] == KEEPER_FRESH_DRAFT


# ---- submit_keepers: the on-roster branch (the originally reported bug) -------

def test_an_on_roster_discovery_submission_records_four_years(test_session):
    """The near-miss the backlog entry describes: submit_keepers synthesized the
    right thing only in the OFF-roster branch, so the ordinary success case — the
    discovery pick actually joined and is on the roster — kept the derived
    ("waiver", 3). No linked pick here, so this exercises submit_keepers' own path."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)

    out = services.submit_keepers(
        test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
        season_year=UPCOMING, discovery_fpl_id=900,
    )
    kept = out["keepers"][0]
    assert kept["player"] == "Woltemade"
    assert kept["acquisition"] == "discovery"
    assert kept["years_remaining"] == KEEPER_FRESH_DRAFT


def test_an_ordinary_on_roster_keeper_is_untouched(test_session):
    """The control: only the is_discovery player gets the override."""
    lg, mgrs, gws = _seed(test_session)
    held = _player(test_session, lg, "Saka", 901)
    _rostered(test_session, mgrs["A"], held, gws)

    out = services.submit_keepers(
        test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[901],
        season_year=UPCOMING,
    )
    assert out["keepers"][0]["acquisition"] == "draft"
    assert out["keepers"][0]["years_remaining"] == KEEPER_FRESH_DRAFT


def test_a_seed_still_beats_the_on_roster_discovery_override(test_session):
    """Same precedence as everywhere else. Without the `not in seeded` check the
    submission would silently overwrite a commissioner's deliberate correction."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=mgrs["A"].id,
                                player_id=p.id, years_remaining=1,
                                acquisition="waiver"))
    test_session.commit()

    out = services.submit_keepers(
        test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
        season_year=UPCOMING, discovery_fpl_id=900,
    )
    assert out["keepers"][0]["acquisition"] == "waiver"
    assert out["keepers"][0]["years_remaining"] == 1


def test_an_exhausted_clock_is_recomputed_eligible_not_inherited(test_session):
    """`eligible` is derived FROM the clock, so correcting the clock has to recompute
    it — inheriting the derived flag would leave a fresh 4-year discovery keeper
    marked ineligible and the submission would be refused."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    test_session.add(KeeperSeed(league_id=lg.id, manager_id=mgrs["A"].id,
                                player_id=p.id, years_remaining=0))
    test_session.commit()
    # derived is now ("waiver", 0) -> eligible False; the seed has no acquisition, so
    # only the CLOCK is pinned and the discovery override still applies to the label.
    assert services._derive_keeper_status(
        test_session, lg
    )[mgrs["A"].id][p.id]["eligible"] is False

    # A seeded player keeps the seeded clock, so this is refused for the RIGHT reason
    # (0 years left), not because of a stale eligible flag.
    with pytest.raises(RuleViolation, match="ineligible"):
        services.submit_keepers(
            test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
            season_year=UPCOMING, discovery_fpl_id=900,
        )


def test_an_on_roster_goalkeeper_discovery_pick_is_still_refused(test_session):
    """The goalie-team rule isn't a clock question, so it survives the override —
    a keeper who joined the PL in January is still kept as a club, not a player."""
    lg, mgrs, gws = _seed(test_session, mode="keeper")
    gk = _player(test_session, lg, "January Keeper", 900, pos="GKP")
    _rostered(test_session, mgrs["A"], gk, gws, over=JANUARY_GWS)

    with pytest.raises(RuleViolation, match="goalkeepers are kept as a club"):
        services.submit_keepers(
            test_session, lg, fpl_manager_id="1", keeper_fpl_ids=[],
            season_year=UPCOMING, discovery_fpl_id=900,
        )


# ---- set_keeper_override accepts the new label -------------------------------

def test_set_keeper_override_accepts_discovery(test_session):
    """The label is now a legal override value — before this it was rejected, and the
    only workaround was to claim 'draft', which recorded something that never
    happened."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)

    out = services.set_keeper_override(
        test_session, lg, fpl_manager_id="1", player_fpl_id=900,
        acquisition="discovery",
    )
    assert out["acquisition"] == "discovery"
    st = services._derive_keeper_status(test_session, lg)[mgrs["A"].id][p.id]
    assert st["acquisition"] == "discovery"


def test_an_acquisition_only_override_freezes_the_clock_it_found(test_session):
    """PRE-EXISTING behaviour of set_keeper_override, pinned here because it is
    genuinely surprising next to `discovery` meaning "4 years": with no explicit
    `years_remaining`, the new seed snapshots the CURRENTLY DERIVED clock — 3, from
    the waiver label being corrected — and the seed then outranks the fresh-clock
    default the new label would otherwise imply. It applies identically to 'draft'
    and 'trade' and is not specific to this change; the commissioner passes
    `years_remaining` to move the clock."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)

    services.set_keeper_override(
        test_session, lg, fpl_manager_id="1", player_fpl_id=900,
        acquisition="discovery",
    )
    st = services._derive_keeper_status(test_session, lg)[mgrs["A"].id][p.id]
    assert st["years_remaining"] == KEEPER_FRESH_WAIVER, "snapshotted, not refreshed"

    services.set_keeper_override(
        test_session, lg, fpl_manager_id="1", player_fpl_id=900,
        years_remaining=KEEPER_FRESH_DRAFT,
    )
    st = services._derive_keeper_status(test_session, lg)[mgrs["A"].id][p.id]
    assert (st["acquisition"], st["years_remaining"]) == (
        "discovery", KEEPER_FRESH_DRAFT)


def test_set_keeper_override_still_rejects_nonsense(test_session):
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    with pytest.raises(RuleViolation, match="acquisition must be one of"):
        services.set_keeper_override(
            test_session, lg, fpl_manager_id="1", player_fpl_id=900,
            acquisition="lottery",
        )


# ---- link_discovery_pick ------------------------------------------------------

def test_linking_keeps_the_label_as_entered(test_session):
    """The free-text name is the record of what was actually called out on draft
    night, and get_discovery_board deliberately prefers it — so the board keeps
    reading the way the draft happened."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade")

    out = services.link_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900,
    )
    assert out["player"] == "Woltemade"
    assert out["label"] == "Nick Woltemade"
    row = test_session.query(DraftPick).one()
    assert row.player_id == p.id
    assert row.player_label == "Nick Woltemade"

    board = services.get_discovery_board(test_session, lg, SEASON)
    assert board[0]["player"] == "Nick Woltemade"


def test_linking_writes_an_audit_entry(test_session):
    from models import AuditLog

    lg, mgrs, gws = _seed(test_session)
    _player(test_session, lg, "Woltemade", 900)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.link_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900,
    )
    entry = test_session.query(AuditLog).filter_by(action="discovery.link").one()
    assert "Woltemade" in entry.summary
    assert entry.details["previous"]["player_id"] is None


def test_linking_the_same_player_twice_is_a_no_op(test_session):
    """Idempotent on purpose: the follow-up suggestion flow can confirm the same
    match twice (two admins, a double-submit) without an error."""
    from models import AuditLog

    lg, mgrs, gws = _seed(test_session)
    _player(test_session, lg, "Woltemade", 900)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    first = services.link_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900)
    again = services.link_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900)

    assert first["changed"] is True and again["changed"] is False
    assert test_session.query(AuditLog).filter_by(action="discovery.link").count() == 1


def test_linking_a_nonexistent_pick_is_refused(test_session):
    lg, mgrs, gws = _seed(test_session)
    _player(test_session, lg, "Woltemade", 900)
    with pytest.raises(RuleViolation, match="no 2026 discovery pick #7"):
        services.link_discovery_pick(
            test_session, lg, season_year=SEASON, pick_number=7, player_fpl_id=900)


def test_relinking_an_already_linked_pick_is_refused(test_session):
    """Overwriting silently would lose the prior link with no trace — unlink first."""
    lg, mgrs, gws = _seed(test_session)
    _player(test_session, lg, "Woltemade", 900)
    _player(test_session, lg, "Someone Else", 901)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.link_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900)

    with pytest.raises(RuleViolation, match="already linked to Woltemade"):
        services.link_discovery_pick(
            test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=901)


def test_linking_one_player_to_two_picks_is_refused(test_session):
    """Two picks on one player would make him two managers' keeper at once."""
    lg, mgrs, gws = _seed(test_session)
    _player(test_session, lg, "Woltemade", 900)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade", pick_number=1)
    _discovery_pick(test_session, lg, mgrs["B"], "N. Woltemade", pick_number=2)
    services.link_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900)

    with pytest.raises(RuleViolation, match="already linked to 2026 discovery pick #1"):
        services.link_discovery_pick(
            test_session, lg, season_year=SEASON, pick_number=2, player_fpl_id=900)


def test_a_goalie_team_pick_cannot_be_linked(test_session):
    """The DraftPick CHECK would reject it as an opaque IntegrityError; refuse it
    cleanly instead."""
    lg, mgrs, gws = _seed(test_session, mode="keeper")
    _player(test_session, lg, "Woltemade", 900)
    team = PlTeam(code=3, fpl_id=3, short_name="ARS", name="Arsenal",
                  is_current_pl=True)
    test_session.add(team)
    test_session.flush()
    test_session.add(DraftPick(
        league_id=lg.id, season_year=SEASON, draft_type="discovery", round=1,
        pick_number=1, manager_id=mgrs["A"].id, team_id=team.id, source="discovery"))
    test_session.commit()

    with pytest.raises(RuleViolation, match="goalie team, not a player"):
        services.link_discovery_pick(
            test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900)


def test_unlinking_restores_the_free_text_pick(test_session):
    lg, mgrs, gws = _seed(test_session)
    _player(test_session, lg, "Woltemade", 900)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.link_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900)

    out = services.unlink_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1)
    assert out["linked"] is False
    row = test_session.query(DraftPick).one()
    assert row.player_id is None
    assert row.player_label == "Nick Woltemade"


def test_unlinking_an_unlinked_pick_is_refused(test_session):
    lg, mgrs, gws = _seed(test_session)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    with pytest.raises(RuleViolation, match="isn't linked to a player"):
        services.unlink_discovery_pick(
            test_session, lg, season_year=SEASON, pick_number=1)


# ---- availability: the taken overlay is draft_type-scoped ---------------------

def test_a_linked_pick_marks_him_taken_in_the_discovery_draft_only(test_session):
    """search_players' `drafted:` overlay is scoped by draft_type, so linking a
    discovery pick takes him off the DISCOVERY board (correct — he's already been
    picked there) without touching the main draft, where he is a legitimate target.
    Mirrors test_draft_availability.py's two-drafts-don't-block-each-other case."""
    lg, mgrs, gws = _seed(test_session)
    _player(test_session, lg, "Woltemade", 900)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade")
    services.link_discovery_pick(
        test_session, lg, season_year=SEASON, pick_number=1, player_fpl_id=900)

    disc = services.search_players(
        test_session, lg, q="Woltemade", available_year=SEASON,
        draft_type="discovery", include_taken=True)
    assert len(disc) == 1
    assert disc[0]["taken"] is True
    assert disc[0]["taken_by"] == "drafted: A"

    main = services.search_players(
        test_session, lg, q="Woltemade", available_year=SEASON,
        draft_type="main", include_taken=True)
    assert len(main) == 1
    assert main[0]["taken"] is False


def test_an_unlinked_pick_marks_nobody_taken(test_session):
    """The control: a free-text pick has no player_id, so the overlay can't see it —
    which is precisely the gap linking closes."""
    lg, mgrs, gws = _seed(test_session)
    _player(test_session, lg, "Woltemade", 900)
    _discovery_pick(test_session, lg, mgrs["A"], "Nick Woltemade")

    rows = services.search_players(
        test_session, lg, q="Woltemade", available_year=SEASON,
        draft_type="discovery", include_taken=True)
    assert rows[0]["taken"] is False


# ---- privacy: the selection-sourced witness stays behind the gate -------------

def test_an_on_roster_discovery_selection_is_invisible_without_a_viewer(test_session):
    """discovery_flagged is built from `kept`, which is privacy-filtered — so a
    viewer-less caller learns nothing from it. The linked-pick witness is public
    draft history and deliberately is NOT gated; this test pins that the two are
    kept on opposite sides of the line."""
    lg, mgrs, gws = _seed(test_session)
    p = _player(test_session, lg, "Woltemade", 900)
    _rostered(test_session, mgrs["A"], p, gws, over=JANUARY_GWS)
    test_session.add(KeeperSelection(
        league_id=lg.id, manager_id=mgrs["A"].id, player_id=p.id,
        season_year=UPCOMING, is_discovery=True))
    test_session.commit()

    blind = services._derive_keeper_status(test_session, lg)[mgrs["A"].id][p.id]
    assert blind["acquisition"] == "waiver", "no viewer must learn nothing"
    assert blind["kept"] is False

    seeing = services._derive_keeper_status(
        test_session, lg, kept_all=True)[mgrs["A"].id][p.id]
    assert seeing["acquisition"] == "discovery"
    assert seeing["years_remaining"] == KEEPER_FRESH_DRAFT
