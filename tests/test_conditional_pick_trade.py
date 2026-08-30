"""A traded draft pick whose ROUND escalates when a condition comes true.

"My 2nd, upgraded to my 1st if Kevin T finishes top 3." The league has actually done
these; three metric families cover every real historical case, and two of them are
MANAGER-level facts (finishing position, winning a cup) rather than player points —
which is why the schema carries a subject pair rather than a player and a threshold.

Two things these tests exist to pin:

  - A condition changes the KEY the ownership fold writes to, not a value. The pick
    lands on the upgraded round and the BASE round is simply never reassigned, so it
    stays with its original owner with nothing to undo. Get that wrong and the seller
    loses both picks, or keeps both.
  - "pending" is not "not_met". A condition resolves only once its season's league row
    is frozen (`sync_locked`) — a live table, an in-progress bracket and a mid-season
    points total are all equally provisional. Until then the BASE round stands, even
    when the live number already clears the threshold.

Every row with `pick_round_if_met IS NULL` must behave exactly as it did before this
feature existed; `test_an_ordinary_pick_trade_is_untouched` is the regression that
matters most, since pick_ownership feeds every season's draft board.

Runs against TEST_DATABASE_URL (see conftest); never the configured database.
"""

import datetime as dt
import uuid

import pytest

import services
from models import (
    Gameweek,
    League,
    Manager,
    Player,
    PlayerSeason,
    SeasonHistory,
    Standing,
    Tournament,
    TournamentMatch,
    Trade,
    TradeConditionTerm,
)
from rules import (
    CONDITION_MET,
    CONDITION_NOT_MET,
    CONDITION_PENDING,
    RuleViolation,
)

# The season the traded PICK belongs to, and the (earlier) season the condition
# resolves in. Deliberately different: a condition is about a season that finishes
# before the draft it modifies.
PICK_YEAR = 2027
COND_YEAR = 2026

T1 = dt.datetime(2026, 1, 1, 12, 0)
T2 = dt.datetime(2026, 1, 2, 12, 0)
T3 = dt.datetime(2026, 1, 3, 12, 0)


def _league(session, year, *, frozen, name=None, current=False):
    lg = League(
        fpl_league_id=str(year), name=name or f"S{year}", season_year=year,
        is_current=current, sync_locked=frozen, phase="offseason",
    )
    session.add(lg)
    session.flush()
    return lg


def _managers(session, lg, names=("A", "B", "C"), *, standings=True):
    out = {}
    for i, name in enumerate(names, start=1):
        m = Manager(league_id=lg.id, fpl_manager_id=f"{lg.season_year}{i}",
                    name=f"{name} FC", display_name=name)
        session.add(m)
        session.flush()
        if standings:
            # rank is recomputed by get_standings from total/points_for, so the
            # ordering here has to come from the numbers, not from Standing.rank.
            session.add(Standing(league_id=lg.id, manager_id=m.id, rank=i,
                                 total=100 - i, points_for=1000 - i))
        out[name] = m
    session.commit()
    return out


def _seed(session, *, cond_frozen=True):
    """Two league rows: the season being drafted, and the earlier season a condition
    resolves against. Both carry the same three people, as separate Manager rows —
    which is exactly why a condition stores a NAME and not a managers.id."""
    cond_lg = _league(session, COND_YEAR, frozen=cond_frozen)
    cond_m = _managers(session, cond_lg)
    pick_lg = _league(session, PICK_YEAR, frozen=False, current=True)
    pick_m = _managers(session, pick_lg)
    session.add(Gameweek(number=1, league_id=pick_lg.id))
    session.commit()
    return pick_lg, pick_m, cond_lg, cond_m


def _player(session, lg, name, fpl_id, *, total_points=None):
    p = Player(fpl_id=fpl_id, code=fpl_id * 1000, name=name, position="MID")
    session.add(p)
    session.flush()
    session.add(PlayerSeason(league_id=lg.id, player_id=p.id, fpl_id=fpl_id,
                             name=name, position="MID", total_points=total_points))
    session.commit()
    return p


# Flat `condition_*` kwarg -> term field. The write API takes terms; these tests
# construct rows DIRECTLY to exercise the read path, and spelling out a one-term
# clause at every call site would bury what each test is actually about. This shim
# exists only here — production has no flat-kwargs path, and the write-path and route
# tests below go through the real `trade_pick(condition_terms=[...])` signature.
_FLAT_TO_TERM = {
    "condition_metric": "metric",
    "condition_player_id": "player_id",
    "condition_manager_name": "manager_name",
    "condition_season_year": "season_year",
    "condition_comparison": "comparison",
    "condition_threshold": "threshold",
    "condition_note": "note",
    "condition_manual_state": "manual_state",
}


def _pick_trade(session, lg, frm, to, orig, *, round=2, created_at=T1,
                terms=None, logic=None, effect=None, **cond):
    """A pick trade, optionally conditional.

    Pass `terms=[...]` for a real multi-term clause, or the flat `condition_*` kwargs
    for the common single-term one. `pick_round_if_met` stays a clause field either way.
    """
    round_if_met = cond.pop("pick_round_if_met", None)
    flat = {_FLAT_TO_TERM[k]: v for k, v in cond.items() if k in _FLAT_TO_TERM}
    unknown = set(cond) - set(_FLAT_TO_TERM)
    assert not unknown, f"unknown condition kwargs: {sorted(unknown)}"
    term_dicts = list(terms) if terms is not None else ([flat] if flat else [])
    if term_dicts:
        logic = logic or "all"
        effect = effect or ("transfer_if_met" if round_if_met is None else "escalate_round")

    t = Trade(league_id=lg.id, from_manager=frm.id, to_manager=to.id,
              pick_original_manager=orig.id, pick_round=round,
              pick_season_year=PICK_YEAR, pick_draft_type="main",
              created_at=created_at,
              condition_logic=logic, condition_effect=effect,
              pick_round_if_met=round_if_met)
    session.add(t)
    session.flush()
    for i, td in enumerate(term_dicts):
        session.add(TradeConditionTerm(
            trade_id=t.id,
            # Explicit stagger: created_at orders the terms in a note, and rows added
            # in one transaction share func.now() to the microsecond.
            created_at=created_at + dt.timedelta(seconds=i),
            **td,
        ))
    session.commit()
    return t


def _term(metric, **over):
    return {"metric": metric, **over}


def _finish_condition(**over):
    """A "finishes top 3" condition on manager A, upgrading R2 -> R1."""
    return {
        "condition_metric": "league_finish", "condition_manager_name": "A",
        "condition_season_year": COND_YEAR, "condition_comparison": "<=",
        "condition_threshold": 3, "pick_round_if_met": 1,
        **over,
    }


def _flat_condition(**flat):
    """Flat `condition_*` kwargs -> the clause/terms kwargs the write API takes.

    Unlike `_pick_trade`'s shim this feeds the REAL trade_pick/edit_trade signature,
    so every validator still runs. The effect is not inferred: `escalate_round` is the
    default, which is what makes "no upgrade round" stay an error rather than quietly
    becoming a `transfer_if_met` clause.
    """
    round_if_met = flat.pop("pick_round_if_met", None)
    logic = flat.pop("condition_logic", None)
    effect = flat.pop("condition_effect", None)
    term = {_FLAT_TO_TERM[k]: v for k, v in flat.items() if k in _FLAT_TO_TERM}
    unknown = set(flat) - set(_FLAT_TO_TERM)
    assert not unknown, f"unknown condition kwargs: {sorted(unknown)}"
    return {
        "condition_logic": logic or "all",
        "condition_effect": effect or "escalate_round",
        "pick_round_if_met": round_if_met,
        "condition_terms": [term] if term else [],
    }


def _only_term(session):
    return session.query(TradeConditionTerm).one()


# ---- the regression that matters most --------------------------------------
def test_an_ordinary_pick_trade_is_untouched(test_session):
    """No condition columns set -> byte-identical behaviour. pick_ownership feeds
    every season's draft board, so this is the blast radius of the whole feature."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], round=2)

    own = services.pick_ownership(test_session, lg, PICK_YEAR)
    assert own == {(2, "A"): "B"}
    assert services.pick_conditions(test_session, lg, PICK_YEAR) == {}


# ---- league_finish ---------------------------------------------------------
def test_league_finish_met_moves_the_upgraded_round_and_leaves_the_base(test_session):
    """The whole mechanic: A finished 1st (top 3), so B gets A's ROUND 1 pick — and
    A's round 2 pick is never reassigned, so it stays with A."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], **_finish_condition())

    own = services.pick_ownership(test_session, lg, PICK_YEAR)
    assert own == {(1, "A"): "B"}, "the upgraded round moves"
    assert (2, "A") not in own, "the base round stays with its original owner"


def test_league_finish_not_met_leaves_the_base_round_traded(test_session):
    """C finished 3rd of three; a "top 1" condition fails, so the base pick moves."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                **_finish_condition(condition_manager_name="C", condition_threshold=1))

    own = services.pick_ownership(test_session, lg, PICK_YEAR)
    assert own == {(2, "A"): "B"}
    conds = services.pick_conditions(test_session, lg, PICK_YEAR)
    assert conds[(2, "A")]["condition_status"] == CONDITION_NOT_MET


def test_a_live_season_stays_pending_even_when_the_number_already_clears(test_session):
    """A is top of the live table, so the condition WOULD be met — but the season
    isn't frozen, so it holds at pending and the base round stands. Freezing on the
    calendar rather than on the data is the point: a table can still change."""
    lg, m, cond_lg, _cm = _seed(test_session, cond_frozen=False)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], **_finish_condition())

    own = services.pick_ownership(test_session, lg, PICK_YEAR)
    assert own == {(2, "A"): "B"}, "unresolved -> base round"
    info = services.pick_conditions(test_session, lg, PICK_YEAR)[(2, "A")]
    assert info["condition_status"] == CONDITION_PENDING
    assert "currently 1st" in info["condition_note"], info["condition_note"]

    # ...and the moment that season freezes, the same rows resolve.
    cond_lg.sync_locked = True
    test_session.commit()
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}


def test_league_finish_uses_adjusted_standings_and_their_tie_break(test_session):
    """get_standings re-ranks on adjusted totals with an alphabetical tie-break, and
    the draft order already consumes that. A condition must read the same ranking —
    never Standing.rank, which is the raw synced value."""
    lg, m, cond_lg, cond_m = _seed(test_session)
    # Force an exact (total, points_for) tie between A and B. Alphabetically A wins,
    # so B is 2nd — while the stored Standing.rank still says A=1, B=2 anyway; the
    # discriminating part is that C is stored rank 3 but adjusted to the top.
    for name, total, pf in (("A", 50, 500), ("B", 50, 500), ("C", 99, 900)):
        st = (test_session.query(Standing)
              .filter_by(league_id=cond_lg.id, manager_id=cond_m[name].id).one())
        st.total, st.points_for = total, pf
    test_session.commit()

    ranked = {r["manager"]: r["rank"] for r in services.get_standings(test_session, cond_lg)}
    assert ranked == {"C": 1, "A": 2, "B": 3}, "sanity: adjusted order, alpha tie-break"

    # "B finishes top 2" is FALSE on the adjusted order (B is 3rd) but TRUE on the
    # stored Standing.rank (2). The condition must agree with the standings page.
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                **_finish_condition(condition_manager_name="B", condition_threshold=2))
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}


# ---- total_points ----------------------------------------------------------
def test_total_points_met_and_unmet(test_session):
    lg, m, cond_lg, _cm = _seed(test_session)
    star = _player(test_session, cond_lg, "Star", 11, total_points=220)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_metric="total_points", condition_player_id=star.id,
                condition_season_year=COND_YEAR, condition_comparison=">=",
                condition_threshold=200, pick_round_if_met=1)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}

    # Raise the bar past his total: the THRESHOLD lives on the term now, and moving
    # it must flip the fold back to the base round.
    _only_term(test_session).threshold = 300
    test_session.commit()
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}


def test_total_points_reads_the_condition_seasons_snapshot_not_the_live_pool(test_session):
    """`players` is global and holds whatever season synced last, so a points
    condition has to read PlayerSeason for the condition's own season."""
    lg, m, cond_lg, _cm = _seed(test_session)
    star = _player(test_session, cond_lg, "Star", 11, total_points=220)
    # A later season's snapshot for the same human, with a very different total.
    test_session.add(PlayerSeason(league_id=lg.id, player_id=star.id, fpl_id=11,
                                  name="Star", position="MID", total_points=3))
    test_session.commit()

    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_metric="total_points", condition_player_id=star.id,
                condition_season_year=COND_YEAR, condition_comparison=">=",
                condition_threshold=200, pick_round_if_met=1)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}


def test_a_player_with_no_snapshot_row_is_not_met_rather_than_an_error(test_session):
    """An absent PlayerSeason row is a condition that hasn't come true, not a crash
    on the draft board."""
    lg, m, _cl, _cm = _seed(test_session)
    ghost = Player(fpl_id=99, code=99000, name="Ghost", position="MID")
    test_session.add(ghost)
    test_session.commit()
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_metric="total_points", condition_player_id=ghost.id,
                condition_season_year=COND_YEAR, condition_comparison=">=",
                condition_threshold=1, pick_round_if_met=1)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}


# ---- cup_win / pup_cup_win -------------------------------------------------
def _bracket(session, lg, name, *, winner, loser, round=3, scored=True):
    t = Tournament(name=name, league_id=lg.id)
    session.add(t)
    session.flush()
    session.add(TournamentMatch(
        tournament_id=t.id, round=round, manager_a=winner.id, manager_b=loser.id,
        score_a=2 if scored else None, score_b=1 if scored else None,
        winner_id=winner.id if scored else None,
    ))
    session.commit()
    return t


def test_cup_win_resolves_from_a_live_scored_bracket(test_session):
    lg, m, cond_lg, cond_m = _seed(test_session)
    # The Cup final is between the two SEMIFINAL winners — _cup_final_and_third
    # identifies it that way, so round 2 has to exist for the final to be found.
    sf = Tournament(name="Cup", league_id=cond_lg.id)
    test_session.add(sf)
    test_session.flush()
    test_session.add_all([
        TournamentMatch(tournament_id=sf.id, round=2, manager_a=cond_m["A"].id,
                        manager_b=cond_m["C"].id, score_a=2, score_b=1,
                        winner_id=cond_m["A"].id),
        TournamentMatch(tournament_id=sf.id, round=2, manager_a=cond_m["B"].id,
                        manager_b=cond_m["C"].id, score_a=2, score_b=1,
                        winner_id=cond_m["B"].id),
        TournamentMatch(tournament_id=sf.id, round=3, manager_a=cond_m["A"].id,
                        manager_b=cond_m["B"].id, score_a=3, score_b=1,
                        winner_id=cond_m["A"].id),
    ])
    test_session.commit()

    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_metric="cup_win", condition_manager_name="A",
                condition_season_year=COND_YEAR, pick_round_if_met=1)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}


def test_someone_else_winning_the_cup_is_not_met(test_session):
    lg, m, cond_lg, cond_m = _seed(test_session)
    _bracket(test_session, cond_lg, "Pup Cup", winner=cond_m["C"], loser=cond_m["B"])
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_metric="pup_cup_win", condition_manager_name="A",
                condition_season_year=COND_YEAR, pick_round_if_met=1)

    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}
    conds = services.pick_conditions(test_session, lg, PICK_YEAR)
    assert conds[(2, "A")]["condition_status"] == CONDITION_NOT_MET


def test_an_unscored_final_holds_at_pending_not_not_met(test_session):
    """A bracket that exists but hasn't been decided is the one case where a FROZEN
    season still can't answer. Saying "not met" there would read as settled."""
    lg, m, cond_lg, cond_m = _seed(test_session)
    _bracket(test_session, cond_lg, "Pup Cup", winner=cond_m["A"], loser=cond_m["B"],
             scored=False)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_metric="pup_cup_win", condition_manager_name="A",
                condition_season_year=COND_YEAR, pick_round_if_met=1)

    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}
    conds = services.pick_conditions(test_session, lg, PICK_YEAR)
    assert conds[(2, "A")]["condition_status"] == CONDITION_PENDING


def test_cup_win_falls_back_to_imported_history_for_a_season_with_no_bracket(test_session):
    """Old seasons ran their cups off-app; the winners live in season_history as
    plain names. get_payouts already resolves them that way and so must this."""
    lg, m, cond_lg, _cm = _seed(test_session)
    test_session.add(SeasonHistory(
        league_id=cond_lg.id, year=f"{COND_YEAR % 100:02d}/{(COND_YEAR + 1) % 100:02d}",
        cup_winner="A", pup_winner="B",
    ))
    test_session.commit()

    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_metric="cup_win", condition_manager_name="A",
                condition_season_year=COND_YEAR, pick_round_if_met=1)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}


def test_the_condition_subject_matches_across_season_manager_rows(test_session):
    """The condition is entered against the 2027 league but resolves against 2026 —
    two different Manager rows for the same person. A managers.id FK could not
    express this, which is why the column is a name."""
    lg, m, cond_lg, cond_m = _seed(test_session)
    assert m["A"].id != cond_m["A"].id, "sanity: one manager row per season"
    _bracket(test_session, cond_lg, "Pup Cup", winner=cond_m["A"], loser=cond_m["B"])
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_metric="pup_cup_win", condition_manager_name="a",  # case-insensitive
                condition_season_year=COND_YEAR, pick_round_if_met=1)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}


def test_a_condition_on_a_season_with_no_league_row_is_pending(test_session):
    """A condition can name a season that doesn't exist in the database yet — that
    is the normal case when it's entered. It must not raise, and must not resolve."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                **_finish_condition(condition_season_year=2099))
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}
    info = services.pick_conditions(test_session, lg, PICK_YEAR)[(2, "A")]
    assert info["condition_status"] == CONDITION_PENDING


# ---- multi-row escalating deals -------------------------------------------
def test_a_multi_row_escalating_deal_resolves_each_row_independently(test_session):
    """Three picks in one deal are three independent Trade rows with no linking FK,
    matching how record_trade already writes a multi-asset trade. No shared state."""
    lg, m, _cl, _cm = _seed(test_session)
    # A's R2 upgrades (A did finish top 3); C's R3 doesn't (C finished 3rd, needs 1st).
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], round=2, **_finish_condition())
    _pick_trade(test_session, lg, m["C"], m["B"], m["C"], round=3, created_at=T2,
                **_finish_condition(condition_manager_name="C", condition_threshold=1))

    own = services.pick_ownership(test_session, lg, PICK_YEAR)
    assert own == {(1, "A"): "B", (3, "C"): "B"}


def test_a_later_trade_of_the_upgraded_slot_still_wins(test_session):
    """Latest-wins is ordered on created_at, and a condition must not disturb that:
    B acquires A's conditional pick, then flips the upgraded slot to C."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], created_at=T1,
                **_finish_condition())
    _pick_trade(test_session, lg, m["B"], m["C"], m["A"], round=1, created_at=T2)

    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "C"}


# ---- display ---------------------------------------------------------------
@pytest.mark.parametrize("cond,expect", [
    (dict(condition_metric="league_finish", condition_manager_name="A",
          condition_comparison="<=", condition_threshold=3),
     "upgrades to R1 if A finishes top 3 in 2026 — met"),
    (dict(condition_metric="cup_win", condition_manager_name="C"),
     "upgrades to R1 if C wins the Cup in 2026 — not met"),
])
def test_condition_note_is_phrased_per_metric(test_session, cond, expect):
    lg, m, cond_lg, cond_m = _seed(test_session)
    # A live, scored Cup so the cup_win case can resolve to a real "not met".
    sf = Tournament(name="Cup", league_id=cond_lg.id)
    test_session.add(sf)
    test_session.flush()
    test_session.add_all([
        TournamentMatch(tournament_id=sf.id, round=2, manager_a=cond_m["A"].id,
                        manager_b=cond_m["C"].id, score_a=2, score_b=1,
                        winner_id=cond_m["A"].id),
        TournamentMatch(tournament_id=sf.id, round=2, manager_a=cond_m["B"].id,
                        manager_b=cond_m["C"].id, score_a=2, score_b=1,
                        winner_id=cond_m["B"].id),
        TournamentMatch(tournament_id=sf.id, round=3, manager_a=cond_m["A"].id,
                        manager_b=cond_m["B"].id, score_a=3, score_b=1,
                        winner_id=cond_m["A"].id),
    ])
    test_session.commit()
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                condition_season_year=COND_YEAR, pick_round_if_met=1, **cond)

    notes = [v["condition_note"] for v in
             services.pick_conditions(test_session, lg, PICK_YEAR).values()]
    assert notes == [expect]


def test_get_trades_surfaces_the_condition(test_session):
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], **_finish_condition())
    rows = [r for season in services.get_trades(test_session) for r in season["trades"]]
    assert len(rows) == 1
    assert rows[0]["conditional"] is True
    assert rows[0]["condition_status"] == CONDITION_MET
    assert "upgrades to R1" in rows[0]["condition_note"]


def test_get_future_picks_surfaces_the_condition(test_session):
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], **_finish_condition())
    years = {e["year"]: e for e in services.get_future_picks(test_session, lg)}
    row = years[PICK_YEAR]["main"][0]
    assert (row["round"], row["original_owner"], row["owner"]) == (1, "A", "B")
    assert row["conditional"] is True
    assert row["condition_status"] == CONDITION_MET
    assert "upgrades to R1" in row["condition_note"]


def test_an_ordinary_row_carries_no_condition_keys_at_all(test_session):
    """Not "conditional: False" — ABSENT. The keys are attached only to a conditional
    row so an ordinary one's dict stays byte-identical to what it was before this
    feature; test_cross_season_trades compares a future-pick dict for exact equality
    and would break the moment three keys appeared on every row."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"])

    rows = [r for season in services.get_trades(test_session) for r in season["trades"]]
    assert not any(k.startswith("condition") for k in rows[0])

    years = {e["year"]: e for e in services.get_future_picks(test_session, lg)}
    pick = years[PICK_YEAR]["main"][0]
    assert pick == {"round": 2, "original_owner": "A", "owner": "B"}


# ---- write path ------------------------------------------------------------
def _fpl(m):
    return m.fpl_manager_id


def test_trade_pick_round_trips_the_clause_and_its_term(test_session):
    lg, m, cond_lg, _cm = _seed(test_session)
    star = _player(test_session, cond_lg, "Star", 11, total_points=220)
    services.trade_pick(
        test_session, lg, from_fpl=_fpl(m["A"]), to_fpl=_fpl(m["B"]),
        original_fpl=_fpl(m["A"]), round=2, season_year=PICK_YEAR,
        **_flat_condition(
            condition_metric="total_points", condition_player_id=star.id,
            condition_season_year=COND_YEAR, condition_comparison=">=",
            condition_threshold=200, pick_round_if_met=1),
    )
    t = test_session.query(Trade).one()
    assert (t.condition_logic, t.condition_effect, t.pick_round_if_met) == (
        "all", "escalate_round", 1)
    term = _only_term(test_session)
    assert (term.metric, term.player_id, term.season_year,
            term.comparison, term.threshold) == (
        "total_points", star.id, COND_YEAR, ">=", 200)
    assert term.manager_name is None
    assert term.trade_id == t.id
    assert "if met" in t.draft_pick


def test_the_cup_metrics_discard_comparison_and_threshold(test_session):
    """They are boolean facts. Persisting a leftover ">= 1" would let a later reader
    mistake it for part of the rule."""
    lg, m, _cl, _cm = _seed(test_session)
    services.trade_pick(
        test_session, lg, from_fpl=_fpl(m["A"]), to_fpl=_fpl(m["B"]),
        original_fpl=_fpl(m["A"]), round=2, season_year=PICK_YEAR,
        **_flat_condition(
            condition_metric="cup_win", condition_manager_name="A",
            condition_season_year=COND_YEAR, condition_comparison=">=",
            condition_threshold=1, pick_round_if_met=1),
    )
    term = _only_term(test_session)
    assert (term.comparison, term.threshold) == (None, None)


@pytest.mark.parametrize("bad,msg", [
    (dict(condition_metric="total_points", condition_manager_name="A",
          condition_season_year=COND_YEAR, condition_comparison=">=",
          condition_threshold=1, pick_round_if_met=1), "needs a player"),
    (dict(condition_metric="cup_win", condition_season_year=COND_YEAR,
          pick_round_if_met=1), "needs a manager"),
    (dict(condition_metric="league_finish", condition_manager_name="A",
          condition_season_year=COND_YEAR, pick_round_if_met=1), "needs a comparison"),
    (dict(condition_metric="league_finish", condition_manager_name="A",
          condition_season_year=COND_YEAR, condition_comparison="<=",
          pick_round_if_met=1), "needs a threshold"),
    (dict(condition_metric="league_finish", condition_manager_name="A",
          condition_season_year=COND_YEAR, condition_comparison="<=",
          condition_threshold=3), "round it upgrades to"),
    (dict(condition_metric="league_finish", condition_manager_name="A",
          condition_comparison="<=", condition_threshold=3,
          pick_round_if_met=1), "season the condition resolves"),
    (dict(condition_metric="nonsense", condition_manager_name="A",
          condition_season_year=COND_YEAR, pick_round_if_met=1), "unknown condition metric"),
])
def test_trade_pick_rejects_a_malformed_condition(test_session, bad, msg):
    lg, m, _cl, _cm = _seed(test_session)
    with pytest.raises(RuleViolation) as e:
        services.trade_pick(
            test_session, lg, from_fpl=_fpl(m["A"]), to_fpl=_fpl(m["B"]),
            original_fpl=_fpl(m["A"]), round=2, season_year=PICK_YEAR,
            **_flat_condition(**bad),
        )
    assert msg in str(e.value)
    assert test_session.query(Trade).count() == 0, "nothing partial is written"


def test_trade_pick_rejects_naming_both_a_player_and_a_manager(test_session):
    lg, m, cond_lg, _cm = _seed(test_session)
    star = _player(test_session, cond_lg, "Star", 11, total_points=220)
    with pytest.raises(RuleViolation, match="not both"):
        services.trade_pick(
            test_session, lg, from_fpl=_fpl(m["A"]), to_fpl=_fpl(m["B"]),
            original_fpl=_fpl(m["A"]), round=2, season_year=PICK_YEAR,
            **_flat_condition(
                condition_metric="total_points", condition_player_id=star.id,
                condition_manager_name="A", condition_season_year=COND_YEAR,
                condition_comparison=">=", condition_threshold=1, pick_round_if_met=1),
        )


def test_edit_trade_sets_and_then_clears_a_condition(test_session):
    lg, m, _cl, _cm = _seed(test_session)
    t = _pick_trade(test_session, lg, m["A"], m["B"], m["A"])
    services.edit_trade(test_session, lg, str(t.id), set_condition=True,
                        **_flat_condition(**_finish_condition()))
    test_session.refresh(t)
    assert t.pick_round_if_met == 1
    assert test_session.query(TradeConditionTerm).count() == 1
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}

    # No terms = clear it, and the pick goes back to being an ordinary R2 trade. The
    # TERM ROWS go too: a clause-less term is unreachable but would still be found by
    # anything querying the table directly.
    services.edit_trade(test_session, lg, str(t.id), set_condition=True)
    test_session.refresh(t)
    assert t.condition_logic is None and t.pick_round_if_met is None
    assert test_session.query(TradeConditionTerm).count() == 0
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}


def test_edit_trade_refuses_a_condition_on_a_player_trade(test_session):
    """Only a pick has a round to escalate."""
    lg, m, cond_lg, _cm = _seed(test_session)
    star = _player(test_session, cond_lg, "Star", 11)
    t = Trade(league_id=lg.id, from_manager=m["A"].id, to_manager=m["B"].id,
              player_id=star.id, created_at=T1)
    test_session.add(t)
    test_session.commit()
    with pytest.raises(RuleViolation, match="only a pick trade"):
        services.edit_trade(test_session, lg, str(t.id), set_condition=True,
                            **_flat_condition(**_finish_condition()))


def test_edit_trade_leaves_the_condition_alone_when_not_asked(test_session):
    """An ordinary correction (fixing the gameweek) must not silently clear a
    condition — which is exactly what a field-by-field editor would do."""
    lg, m, _cl, _cm = _seed(test_session)
    t = _pick_trade(test_session, lg, m["A"], m["B"], m["A"], **_finish_condition())
    services.edit_trade(test_session, lg, str(t.id), event_gw=5)
    test_session.refresh(t)
    assert t.pick_round_if_met == 1 and t.condition_logic == "all"
    assert _only_term(test_session).metric == "league_finish"


def test_resolve_player_by_label_accepts_the_accent_alias(test_session):
    """The corrections form posts a NAME, and the picker offers both spellings."""
    lg, _m, cond_lg, _cm = _seed(test_session)
    p = _player(test_session, cond_lg, "Šeško", 11)
    assert services.resolve_player_by_label(test_session, lg, "Šeško").id == p.id
    assert services.resolve_player_by_label(test_session, lg, "sesko").id == p.id
    with pytest.raises(RuleViolation, match="no player matching"):
        services.resolve_player_by_label(test_session, lg, "Nobody")


# ---- the commissioner-only gate on the route -------------------------------
# Two managers may enter an ordinary pick trade between themselves. A CONDITION is
# different: it binds a future season's result to a pick, and the two parties are not
# the only people it affects. The template hides the sub-form from a non-admin; these
# pin the route's own check, which is the actual boundary.

@pytest.fixture
def client(test_session):
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app, follow_redirects=False)


def _route_seed(session):
    """One current, draftable league — the route resolves the league itself, and a
    frozen or non-current row would fail for the wrong reason."""
    lg = League(fpl_league_id="9", name="S", season_year=PICK_YEAR, is_current=True,
                sync_locked=False, phase="draft")
    session.add(lg)
    session.flush()
    session.add(Gameweek(number=1, league_id=lg.id))
    m = _managers(session, lg)
    return lg, m


def _login_manager(client, session, manager, password="pw"):
    from auth import hash_password

    manager.password_hash = hash_password(password)
    session.commit()
    r = client.post("/login", data={"manager_id": manager.fpl_manager_id,
                                    "password": password})
    assert r.status_code == 303, r.text


def _login_admin(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "cond-test-pw")
    r = client.post("/admin/login", data={"password": "cond-test-pw"})
    assert r.status_code == 303, r.text


def _cond_form(m):
    return {
        "pick": f"{m['A'].fpl_manager_id}:2", "to_fpl": m["B"].fpl_manager_id,
        "condition_metric": "league_finish", "condition_manager_name": "A",
        "condition_comparison": "<=", "condition_threshold": "3",
        "condition_season_year": str(COND_YEAR), "pick_round_if_met": "1",
    }


def test_a_manager_cannot_make_a_pick_conditional_via_the_route(
    client, test_session, monkeypatch
):
    lg, m = _route_seed(test_session)
    _login_manager(client, test_session, m["A"])

    r = client.post(f"/draft/{PICK_YEAR}/trade-pick", data=_cond_form(m))
    assert r.status_code == 403
    assert b"Only the commissioner" in r.content
    assert test_session.query(Trade).count() == 0, "and nothing was written"


def test_a_manager_can_still_enter_an_ordinary_pick_trade(client, test_session):
    """The gate must catch the condition, not the trade — a plain pick trade between
    the two parties stays open to managers, as it has always been."""
    lg, m = _route_seed(test_session)
    _login_manager(client, test_session, m["A"])

    r = client.post(f"/draft/{PICK_YEAR}/trade-pick", data={
        "pick": f"{m['A'].fpl_manager_id}:2", "to_fpl": m["B"].fpl_manager_id,
    })
    assert r.status_code == 200, r.text
    t = test_session.query(Trade).one()
    assert t.pick_round_if_met is None


def test_the_admin_can_make_a_pick_conditional_via_the_route(
    client, test_session, monkeypatch
):
    lg, m = _route_seed(test_session)
    _login_admin(client, monkeypatch)

    r = client.post(f"/draft/{PICK_YEAR}/trade-pick", data=_cond_form(m))
    assert r.status_code == 200, r.text
    t = test_session.query(Trade).one()
    assert (t.condition_logic, t.condition_effect, t.pick_round_if_met) == (
        "all", "escalate_round", 1)
    term = _only_term(test_session)
    assert (term.metric, term.manager_name, term.comparison,
            term.threshold, term.season_year) == ("league_finish", "A", "<=", 3, COND_YEAR)


def test_the_conditional_sub_form_is_hidden_from_a_manager(client, test_session):
    lg, m = _route_seed(test_session)
    _login_manager(client, test_session, m["A"])

    body = client.get(f"/draft/{PICK_YEAR}").content.decode()
    assert "Trade a pick" in body, "the ordinary control stays"
    assert "Make this pick conditional" not in body


def test_the_conditional_sub_form_is_shown_to_the_admin(
    client, test_session, monkeypatch
):
    lg, m = _route_seed(test_session)
    _login_admin(client, monkeypatch)

    body = client.get(f"/draft/{PICK_YEAR}").content.decode()
    assert "Make this pick conditional" in body


def test_the_admin_can_enter_a_points_condition_via_the_route(
    client, test_session, monkeypatch
):
    """The player-subject path through the ROUTE, which the service-level tests miss:
    the form posts an element id, and resolving it is the only step that route does
    on its own. It called _resolve_player with a spurious `league` argument at first —
    a 500 that no service-level test could have seen."""
    lg, m = _route_seed(test_session)
    star = _player(test_session, lg, "Star", 11, total_points=220)
    _login_admin(client, monkeypatch)

    r = client.post(f"/draft/{PICK_YEAR}/trade-pick", data={
        "pick": f"{m['A'].fpl_manager_id}:2", "to_fpl": m["B"].fpl_manager_id,
        "condition_metric": "total_points", "condition_player_fpl_id": "11",
        "condition_comparison": ">=", "condition_threshold": "200",
        "condition_season_year": str(PICK_YEAR), "pick_round_if_met": "1",
    })
    assert r.status_code == 200, r.text
    test_session.query(Trade).one()
    term = _only_term(test_session)
    assert term.player_id == star.id
    assert term.manager_name is None


def test_an_unknown_condition_player_is_a_clean_400_not_a_500(
    client, test_session, monkeypatch
):
    lg, m = _route_seed(test_session)
    _login_admin(client, monkeypatch)

    r = client.post(f"/draft/{PICK_YEAR}/trade-pick", data={
        "pick": f"{m['A'].fpl_manager_id}:2", "to_fpl": m["B"].fpl_manager_id,
        "condition_metric": "total_points", "condition_player_fpl_id": "4242",
        "condition_comparison": ">=", "condition_threshold": "200",
        "condition_season_year": str(PICK_YEAR), "pick_round_if_met": "1",
    })
    assert r.status_code == 400, r.text
    assert test_session.query(Trade).count() == 0


def test_resolve_player_by_label_handles_a_departed_player(test_session):
    """`players.fpl_id` is NULL for anyone who has left the PL, and there can be many
    of them. Resolving a label by round-tripping through fpl_id therefore issued
    `WHERE fpl_id IS NULL`, matched every departed row at once and raised
    MultipleResultsFound — a 500 — which is the exact trap `_resolve_player` carries a
    guard for. This resolves the row directly, so no element id is involved."""
    lg, _m, _cl, _cm = _seed(test_session)
    gone = Player(code=901, fpl_id=None, name="Gone", position="MID")
    test_session.add_all([gone, Player(code=902, fpl_id=None, name="Alsogone",
                                       position="MID")])
    test_session.commit()

    assert services.resolve_player_by_label(test_session, lg, "Gone").id == gone.id


# ---- v2: multi-term clauses, manual terms, conditional transfer ---------------
# Everything below covers what the flat seven-column version could not express. The
# driving example is a real 2026 deal (KT <-> KS) whose three clauses between them
# needed an OR, an AND, and a metric nothing in this codebase can compute.


def _met_term():
    """A term that resolves MET against the frozen COND_YEAR league (A finished 1st)."""
    return _term("league_finish", manager_name="A", season_year=COND_YEAR,
                 comparison="<=", threshold=3)


def _unmet_term():
    """Same shape, but C finished 3rd, so "finishes 1st" is a real not_met."""
    return _term("league_finish", manager_name="C", season_year=COND_YEAR,
                 comparison="<=", threshold=1)


def _manual_term(note="Cunha picks up fewer than 3 red cards", state=None):
    return _term("manual", note=note, manual_state=state)


@pytest.mark.parametrize("logic,terms,expected_round", [
    # AND: both true -> upgraded. This is the shape every pre-v2 condition had.
    ("all", ["met", "met"], 1),
    # AND: one false decides it, without waiting on anything else.
    ("all", ["met", "unmet"], 2),
    # OR: one true is enough.
    ("any", ["unmet", "met"], 1),
    # OR: all false -> base round.
    ("any", ["unmet", "unmet"], 2),
])
def test_a_clause_folds_its_terms_under_all_or_any(
    test_session, logic, terms, expected_round
):
    lg, m, _cl, _cm = _seed(test_session)
    built = [_met_term() if t == "met" else _unmet_term() for t in terms]
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                terms=built, logic=logic, pick_round_if_met=1)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {
        (expected_round, "A"): "B"}


def test_an_or_clause_is_met_by_a_known_branch_while_a_manual_one_is_undecided(
    test_session,
):
    """The KT<->KS four-way OR, in miniature. One branch ("pick 12 scoring 225") is
    unevaluable, so a clause-level manual flag would strand the whole thing at pending
    forever. Because manual lives on the TERM, the knowable branches still decide it."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="any",
                pick_round_if_met=1,
                terms=[_unmet_term(), _manual_term(), _met_term()])

    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}
    conds = services.pick_conditions(test_session, lg, PICK_YEAR)
    assert conds[(1, "A")]["condition_status"] == CONDITION_MET


def test_an_or_clause_with_no_met_branch_waits_on_the_manual_one(test_session):
    """The other half of the same rule: undecided is PENDING, never not_met, so the
    base round stays in force rather than the deal being silently called off."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="any",
                pick_round_if_met=1, terms=[_unmet_term(), _manual_term()])

    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}
    assert services.pick_conditions(test_session, lg, PICK_YEAR)[(2, "A")][
        "condition_status"] == CONDITION_PENDING


def test_an_and_clause_waits_on_a_manual_term_even_with_every_other_term_met(
    test_session,
):
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="all",
                pick_round_if_met=1, terms=[_met_term(), _manual_term()])
    assert services.pick_conditions(test_session, lg, PICK_YEAR)[(2, "A")][
        "condition_status"] == CONDITION_PENDING


def test_ruling_on_a_manual_term_resolves_the_clause(test_session):
    """The escape valve end to end: record a clause nothing can compute, then let the
    commissioner settle it."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="all",
                pick_round_if_met=1, terms=[_met_term(), _manual_term()])
    term = test_session.query(TradeConditionTerm).filter_by(metric="manual").one()

    services.set_condition_term_state(test_session, lg, str(term.id), CONDITION_MET)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(1, "A"): "B"}

    services.set_condition_term_state(test_session, lg, str(term.id), CONDITION_NOT_MET)
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}

    # Back to undecided -> pending, and the base round again.
    services.set_condition_term_state(test_session, lg, str(term.id), None)
    assert services.pick_conditions(test_session, lg, PICK_YEAR)[(2, "A")][
        "condition_status"] == CONDITION_PENDING


def test_only_a_manual_term_can_be_ruled_on_by_hand(test_session):
    """An evaluable metric is answered by the data. Letting a human override it would
    make the draft board disagree with the standings page for reasons nothing records."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], **_finish_condition())
    term = _only_term(test_session)
    with pytest.raises(RuleViolation, match="resolves from the data"):
        services.set_condition_term_state(test_session, lg, str(term.id), CONDITION_MET)


# ---- the second effect -------------------------------------------------------
def test_transfer_if_met_moves_nothing_until_the_condition_is_met(test_session):
    """The pick stays with its ORIGINAL owner — the fold writes no entry at all, so
    there is nothing to undo if the condition later flips."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                logic="all", effect="transfer_if_met", terms=[_unmet_term()])
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {}


def test_transfer_if_met_moves_the_pick_once_met(test_session):
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                logic="all", effect="transfer_if_met", terms=[_met_term()])
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}


def test_a_pending_transfer_leaves_the_pick_put(test_session):
    """Pending is not met: an undecided manual term must not hand the pick over early,
    because taking it back is exactly what the fold cannot do."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"],
                logic="all", effect="transfer_if_met", terms=[_manual_term()])
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {}


def test_transfer_if_met_refuses_an_upgrade_round(test_session):
    """Storing one would leave a number on the row that reads as part of the rule."""
    lg, m, _cl, _cm = _seed(test_session)
    with pytest.raises(RuleViolation, match="no upgrade round"):
        services.trade_pick(
            test_session, lg, from_fpl=_fpl(m["A"]), to_fpl=_fpl(m["B"]),
            original_fpl=_fpl(m["A"]), round=2, season_year=PICK_YEAR,
            condition_logic="all", condition_effect="transfer_if_met",
            pick_round_if_met=1, condition_terms=[_met_term()],
        )


def test_a_manual_term_needs_its_text(test_session):
    """Without it there is nothing for the commissioner to rule on later, and the
    clause would be an unlabelled permanent pending."""
    lg, m, _cl, _cm = _seed(test_session)
    with pytest.raises(RuleViolation, match="written out"):
        services.trade_pick(
            test_session, lg, from_fpl=_fpl(m["A"]), to_fpl=_fpl(m["B"]),
            original_fpl=_fpl(m["A"]), round=2, season_year=PICK_YEAR,
            condition_logic="all", condition_effect="transfer_if_met",
            condition_terms=[_term("manual")],
        )


def test_a_clause_with_no_terms_is_refused(test_session):
    lg, m, _cl, _cm = _seed(test_session)
    with pytest.raises(RuleViolation, match="at least one condition"):
        services.trade_pick(
            test_session, lg, from_fpl=_fpl(m["A"]), to_fpl=_fpl(m["B"]),
            original_fpl=_fpl(m["A"]), round=2, season_year=PICK_YEAR,
            condition_logic="all", condition_effect="escalate_round",
            pick_round_if_met=1, condition_terms=[],
        )


def test_a_clause_whose_terms_vanished_is_pending_not_met(test_session):
    """all([]) is True in Python, so a clause that lost its terms would otherwise
    apply itself. Reachable if a term row is deleted out from under a trade."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], **_finish_condition())
    test_session.query(TradeConditionTerm).delete()
    test_session.commit()
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}


def test_deleting_a_trade_takes_its_terms_with_it(test_session):
    """The FK cascade — otherwise delete_trade fails on rows it knows nothing about."""
    lg, m, _cl, _cm = _seed(test_session)
    t = _pick_trade(test_session, lg, m["A"], m["B"], m["A"], **_finish_condition())
    assert test_session.query(TradeConditionTerm).count() == 1
    services.delete_trade(test_session, lg, str(t.id))
    assert test_session.query(TradeConditionTerm).count() == 0


# ---- notes -------------------------------------------------------------------
def test_a_multi_term_note_reads_as_a_sentence(test_session):
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="any",
                pick_round_if_met=1, terms=[_unmet_term(), _manual_term()])
    note = services.pick_conditions(test_session, lg, PICK_YEAR)[(2, "A")]["condition_note"]
    assert note == (
        "upgrades to R1 if C finishes top 1 in 2026 or Cunha picks up fewer than 3 "
        "red cards — pending (currently 3rd)"
    )


def test_a_transfer_note_says_transfers_not_upgrades(test_session):
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="all",
                effect="transfer_if_met", terms=[_met_term()])
    note = services.pick_conditions(test_session, lg, PICK_YEAR)[(2, "A")]["condition_note"]
    assert note == "transfers only if A finishes top 3 in 2026 — met"


def test_a_manual_terms_wording_is_quoted_not_paraphrased(test_session):
    """The commissioner's words are the rule; rewording them would misstate an
    agreement between real people."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="all",
                pick_round_if_met=1,
                terms=[_manual_term("pick 12 scores 225 or more")])
    note = services.pick_conditions(test_session, lg, PICK_YEAR)[(2, "A")]["condition_note"]
    assert "pick 12 scores 225 or more" in note


def test_get_trades_surfaces_manual_terms_for_the_corrections_editor(test_session):
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="all",
                pick_round_if_met=1, terms=[_met_term(), _manual_term()])
    rows = [r for season in services.get_trades(test_session) for r in season["trades"]]
    row = next(r for r in rows if r.get("conditional"))
    assert [t["note"] for t in row["manual_terms"]] == [
        "Cunha picks up fewer than 3 red cards"]
    assert row["manual_terms"][0]["manual_state"] is None


def test_an_ordinary_trade_has_no_manual_terms_key(test_session):
    """Ordinary rows must stay key-identical — two tests assert exact dict equality
    on them, one of them in another module."""
    lg, m, _cl, _cm = _seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"])
    rows = [r for season in services.get_trades(test_session) for r in season["trades"]]
    row = next(r for r in rows if r["kind"] == "pick")
    assert "manual_terms" not in row and "conditional" not in row


def test_the_free_text_of_a_deal_is_kept_verbatim(test_session):
    """A clause only partly expressible in terms must not lose its wording."""
    lg, m, _cl, _cm = _seed(test_session)
    services.trade_pick(
        test_session, lg, from_fpl=_fpl(m["A"]), to_fpl=_fpl(m["B"]),
        original_fpl=_fpl(m["A"]), round=2, season_year=PICK_YEAR,
        conditions="+1 additional discovery (2027 2nd) = one of ks winning cup, ...",
        **_flat_condition(**_finish_condition()),
    )
    t = test_session.query(Trade).one()
    assert t.conditions.startswith("+1 additional discovery (2027 2nd)")


# ---- the corrections editor renders ------------------------------------------
def test_the_corrections_page_renders_the_term_editor_and_manual_rulings(
    client, test_session, monkeypatch
):
    """The editor is the only surface that can settle a manual term, so a template
    error here silently strands every unevaluable clause at pending forever."""
    lg, m = _route_seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="any",
                pick_round_if_met=1,
                terms=[_manual_term("Cunha picks up fewer than 3 red cards")])
    _login_admin(client, monkeypatch)

    r = client.get("/admin/corrections")
    assert r.status_code == 200, r.text
    # the clause editor
    assert 'name="condition_logic"' in r.text
    assert 'name="condition_effect"' in r.text
    assert "+ another condition" in r.text
    # the manual-term ruling control, carrying this term's own text
    assert "/admin/corrections/trade/term-state" in r.text
    assert "Cunha picks up fewer than 3 red cards" in r.text


def test_the_admin_can_rule_on_a_manual_term_via_the_route(
    client, test_session, monkeypatch
):
    lg, m = _route_seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="all",
                effect="transfer_if_met", terms=[_manual_term()])
    term = test_session.query(TradeConditionTerm).one()
    _login_admin(client, monkeypatch)

    # Pending, so the pick has not moved.
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {}

    r = client.post("/admin/corrections/trade/term-state",
                    data={"term_id": str(term.id), "manual_state": "met"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    test_session.expire_all()
    assert services.pick_ownership(test_session, lg, PICK_YEAR) == {(2, "A"): "B"}


def test_a_manager_cannot_rule_on_a_manual_term(test_session, client, monkeypatch):
    """A condition binds a future season's result; the two managers in the trade are
    not the only people it affects."""
    lg, m = _route_seed(test_session)
    _pick_trade(test_session, lg, m["A"], m["B"], m["A"], logic="all",
                pick_round_if_met=1, terms=[_manual_term()])
    term = test_session.query(TradeConditionTerm).one()
    _login_manager(client, test_session, m["A"])

    r = client.post("/admin/corrections/trade/term-state",
                    data={"term_id": str(term.id), "manual_state": "met"},
                    follow_redirects=False)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
    test_session.expire_all()
    assert term.manual_state is None


def test_a_multi_term_condition_round_trips_through_the_draft_route(
    client, test_session, monkeypatch
):
    """The parallel-array form encoding, end to end — the part no service-level test
    can see, since it is the browser's repeated-key shape being zipped back up."""
    lg, m = _route_seed(test_session)
    _login_admin(client, monkeypatch)

    r = client.post(f"/draft/{PICK_YEAR}/trade-pick", data={
        "pick": f"{m['A'].fpl_manager_id}:2", "to_fpl": m["B"].fpl_manager_id,
        "condition_logic": "any", "condition_effect": "escalate_round",
        "pick_round_if_met": "1",
        # two terms, one field name each, exactly as the repeater posts them
        "condition_metric": ["league_finish", "manual"],
        "condition_manager_name": ["A", ""],
        "condition_player_fpl_id": ["", ""],
        "condition_comparison": ["<=", ""],
        "condition_threshold": ["3", ""],
        "condition_season_year": [str(COND_YEAR), ""],
        "condition_note": ["", "pick 12 scores 225 or more"],
    })
    assert r.status_code == 200, r.text
    t = test_session.query(Trade).one()
    assert (t.condition_logic, t.condition_effect) == ("any", "escalate_round")
    terms = test_session.query(TradeConditionTerm).order_by(
        TradeConditionTerm.created_at).all()
    assert [x.metric for x in terms] == ["league_finish", "manual"]
    assert terms[0].manager_name == "A" and terms[0].threshold == 3
    assert terms[1].note == "pick 12 scores 225 or more"
    # A manual term keeps no structured subject, so a blank column can't read as data.
    assert terms[1].manager_name is None and terms[1].season_year is None


def test_a_blank_repeater_row_is_dropped_rather_than_validated(
    client, test_session, monkeypatch
):
    """The + button leaves an empty row behind constantly. It must never fail a
    submission."""
    lg, m = _route_seed(test_session)
    _login_admin(client, monkeypatch)

    r = client.post(f"/draft/{PICK_YEAR}/trade-pick", data={
        "pick": f"{m['A'].fpl_manager_id}:2", "to_fpl": m["B"].fpl_manager_id,
        "condition_logic": "all", "condition_effect": "escalate_round",
        "pick_round_if_met": "1",
        "condition_metric": ["league_finish", ""],
        "condition_manager_name": ["A", ""],
        "condition_player_fpl_id": ["", ""],
        "condition_comparison": ["<=", ""],
        "condition_threshold": ["3", ""],
        "condition_season_year": [str(COND_YEAR), ""],
        "condition_note": ["", ""],
    })
    assert r.status_code == 200, r.text
    assert test_session.query(TradeConditionTerm).count() == 1


# ---- the deal this redesign exists for ---------------------------------------
def test_the_kt_ks_trade_is_representable(test_session):
    """The real 2026 deal that the seven-column version could not hold.

        KT trades: Cunha, Pick 12
        KS trades: 6-9 Discoveries, 2028/2029/2030 1sts and 2nds
        +1 additional discovery (2027 1st) = cunha less than 3 red cards
        +1 additional discovery (2027 2nd) = one of ks winning cup, ks winning league,
                                             cunha scoring 190, or pick 12 scoring 225
        +1 additional discovery 2026 2nd   = cunha 200+ and ks wins league

    Three conditional clauses, so three Trade rows — which is what a three-pick deal
    already was. Between them they need an OR, an AND, a metric nothing computes
    (red cards), and a subject that isn't a player (a pick's eventual points). The
    two unevaluable ones ride as `manual` terms, which is the whole point: the deal
    is recorded in full even though a third of it can't be resolved automatically.
    """
    lg, m, cond_lg, _cm = _seed(test_session)
    cunha = _player(test_session, cond_lg, "Cunha", 12, total_points=205)

    # Clause 1: one manual term. Nothing in the schema counts red cards.
    c1 = _pick_trade(test_session, lg, m["A"], m["B"], m["A"], round=1,
                     logic="all", effect="transfer_if_met", created_at=T1,
                     terms=[_manual_term("cunha less than 3 red cards")])
    # Clause 2: a four-way OR, one branch of which is unevaluable.
    c2 = _pick_trade(test_session, lg, m["A"], m["B"], m["A"], round=2,
                     logic="any", effect="transfer_if_met", created_at=T2,
                     terms=[
                         _term("cup_win", manager_name="B", season_year=COND_YEAR),
                         _term("league_finish", manager_name="B",
                               season_year=COND_YEAR, comparison="<=", threshold=1),
                         _term("total_points", player_id=cunha.id,
                               season_year=COND_YEAR, comparison=">=", threshold=190),
                         _manual_term("pick 12 scoring 225"),
                     ])
    # Clause 3: a two-way AND, both branches evaluable.
    c3 = _pick_trade(test_session, lg, m["A"], m["B"], m["A"], round=3,
                     logic="all", effect="transfer_if_met", created_at=T3,
                     terms=[
                         _term("total_points", player_id=cunha.id,
                               season_year=COND_YEAR, comparison=">=", threshold=200),
                         _term("league_finish", manager_name="B",
                               season_year=COND_YEAR, comparison="<=", threshold=1),
                     ])
    assert test_session.query(TradeConditionTerm).count() == 7

    conds = services.pick_conditions(test_session, lg, PICK_YEAR)
    own = services.pick_ownership(test_session, lg, PICK_YEAR)

    # 1: a lone undecided manual term -> pending, so R1 stays put.
    assert conds[(1, "A")]["condition_status"] == CONDITION_PENDING
    assert (1, "A") not in own

    # 2: Cunha's 205 clears 190, so the OR is MET despite the manual branch and
    #    despite B winning neither the cup nor the league. R2 moves.
    assert conds[(2, "A")]["condition_status"] == CONDITION_MET
    assert own[(2, "A")] == "B"

    # 3: Cunha clears 200, but B finished 2nd, so the AND is a real not_met.
    assert conds[(3, "A")]["condition_status"] == CONDITION_NOT_MET
    assert (3, "A") not in own

    # Ruling on clause 1's manual term is all it takes to settle it.
    term = test_session.query(TradeConditionTerm).filter_by(
        note="cunha less than 3 red cards").one()
    services.set_condition_term_state(test_session, lg, str(term.id), CONDITION_MET)
    assert services.pick_ownership(test_session, lg, PICK_YEAR)[(1, "A")] == "B"

    assert {c1.pick_round, c2.pick_round, c3.pick_round} == {1, 2, 3}
