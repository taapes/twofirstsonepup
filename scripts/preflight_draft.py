"""Read-only pre-draft data checks — the things GET /admin/health does not verify:
draft order completeness, keeper submission coverage, stale keepers that would still
block a player, future-pick name matching, and projections/passwords/pool freshness.

SELECT-only. No commits, no writes, no service calls that mutate state. Prints one
PASS/FAIL line per check and exits non-zero if any FAIL.

Run:  python scripts/preflight_draft.py
"""

import sys

sys.path.insert(0, ".")

import services
from db import SessionLocal
from models import (
    DraftLottery,
    DraftPick,
    FuturePick,
    KeeperSelection,
    League,
    Manager,
    Player,
    PlayerProjection,
    PlTeam,
    Standing,
)
from rules import draft_picks_per_manager, goalie_teams_on

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def main() -> None:
    db = SessionLocal()
    try:
        leagues = db.query(League).all()
        check("exactly one league row", len(leagues) == 1,
              f"{len(leagues)} found")
        league = services.current_league(db)
        if league is None:
            check("a current league exists", False, "no current_league() — aborting")
            return

        check(
            "rollover NOT done (still on the pre-draft season)",
            league.is_current and league.phase != "draft",
            f"season_year={league.season_year} is_current={league.is_current} "
            f"phase={league.phase}",
        )

        # Derived, not a module constant — the pinned 2026 went stale the moment the
        # season it named arrived, and every check below silently looked at the wrong
        # year.
        upcoming_season = (league.season_year or 0) + 1
        print(f"       (checking the {upcoming_season} draft)")

        managers = db.query(Manager).filter_by(league_id=league.id).all()
        n_mgrs = len(managers)
        check("10 managers", n_mgrs == 10, f"{n_mgrs} found")

        lottery = db.query(DraftLottery).filter_by(league_id=league.id).all()
        with_result = [r for r in lottery if r.pick_result is not None]
        check(
            "draft_lottery: every manager has an R1 pick_result",
            len(with_result) == n_mgrs and n_mgrs > 0,
            f"{len(with_result)}/{n_mgrs} set — a gap silently falls back to "
            f"reverse standings for R1",
        )

        standings = db.query(Standing).filter_by(league_id=league.id).all()
        check(
            "standings row per manager",
            len(standings) == n_mgrs,
            f"{len(standings)}/{n_mgrs} — missing rows silently TRUNCATE the board "
            f"to round 1 and it renders as 'Draft complete'",
        )

        by_person = {m.display: m for m in managers}
        selections = (
            db.query(KeeperSelection)
            .filter_by(league_id=league.id, season_year=upcoming_season)
            .all()
        )
        submitted_mgr_ids = {s.manager_id for s in selections}
        submitters = sorted(m.display for m in managers if m.id in submitted_mgr_ids)
        non_submitters = sorted(m.display for m in managers if m.id not in submitted_mgr_ids)
        check(
            f"keeper selections submitted for {upcoming_season}",
            not non_submitters,
            f"submitted: {submitters or '(none)'} | NOT submitted: "
            f"{non_submitters or '(none)'} — a non-submitter gets a full "
            f"{draft_picks_per_manager(league.goalie_team_mode)}-round board",
        )

        effective = services.effective_keeper_selections(db, league, upcoming_season)
        effective_ids = {s.id for s in effective}
        mgr_display = {m.id: m.display for m in managers}
        stale = [s for s in selections if s.id not in effective_ids]
        check(
            "no stale keeper selections (player no longer effectively owned)",
            not stale,
            f"{len(stale)} stale row(s) for {sorted({mgr_display.get(s.manager_id) for s in stale})}"
            f" — still block the player from being drafted by ANYONE; clear at /admin/keepers"
            if stale else "",
        )

        future_picks = (
            db.query(FuturePick)
            .filter(FuturePick.league_id == league.id,
                    FuturePick.season_year == upcoming_season)
            .all()
        )
        display_names = set(by_person)
        mismatched = sorted({
            name for fp in future_picks
            for name in (fp.original_owner, fp.owner)
            if name not in display_names
        })
        check(
            f"future_picks ({len(future_picks)} rows) — every name matches a manager",
            not mismatched,
            f"unmatched names: {mismatched} — silently reverts that pick to its "
            f"original owner" if mismatched else f"{len(future_picks)} reassignments",
        )

        if goalie_teams_on(league.goalie_team_mode):
            clubs = db.query(PlTeam).filter_by(is_current_pl=True).count()
            check(
                "pl_teams has a current Premier League",
                clubs >= 20,
                f"{clubs} clubs flagged is_current_pl — run a players sync; the "
                f"goalie-team picker is empty without them",
            )
            keeperless = sorted(
                t.short_name for t in db.query(PlTeam).filter_by(is_current_pl=True)
                if not db.query(Player).filter(
                    Player.position == "GKP", Player.fpl_id.isnot(None),
                    Player.current_team == t.short_name).count()
            )
            check(
                "every club has at least one goalkeeper in the pool",
                not keeperless,
                f"{keeperless} — a club with no keepers is a draftable asset worth "
                f"zero points, and the join is on short_name" if keeperless else "",
            )
            drafted = (
                db.query(DraftPick)
                .filter(DraftPick.league_id == league.id,
                        DraftPick.season_year == upcoming_season,
                        DraftPick.draft_type == "main",
                        DraftPick.team_id.isnot(None))
                .count()
            )
            check(
                "goalie teams not yet drafted",
                drafted == 0,
                f"{drafted} already recorded for {upcoming_season} — expected 0 "
                f"before the draft" if drafted else "none, as expected",
            )

        proj_count = (
            db.query(PlayerProjection)
            .filter_by(season_year=upcoming_season)
            .count()
        )
        check(
            f"player_projection rows exist for {upcoming_season}",
            proj_count > 0,
            f"{proj_count} rows — 0 means /draft-prep shows 'no projections imported'"
            f" (board itself is unaffected)",
        )

        no_password = sorted(m.display for m in managers if not m.password_hash)
        check(
            "every manager has a password set",
            not no_password,
            f"missing: {no_password}" if no_password else "",
        )

        freshness = services.player_pool_freshness(db)
        check(
            "player pool freshness recorded",
            freshness["synced_at"] is not None,
            f"synced_at={freshness['synced_at']} live={freshness['live']} "
            f"historical={freshness['historical']}",
        )

    finally:
        db.close()

    print()
    if failures:
        print(f"{failures} check(s) FAILED — review before starting the draft.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
