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
    FuturePick,
    KeeperSelection,
    League,
    Manager,
    PlayerProjection,
    Standing,
)

UPCOMING_SEASON = 2026

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
            .filter_by(league_id=league.id, season_year=UPCOMING_SEASON)
            .all()
        )
        submitted_mgr_ids = {s.manager_id for s in selections}
        submitters = sorted(m.display for m in managers if m.id in submitted_mgr_ids)
        non_submitters = sorted(m.display for m in managers if m.id not in submitted_mgr_ids)
        check(
            f"keeper selections submitted for {UPCOMING_SEASON}",
            not non_submitters,
            f"submitted: {submitters or '(none)'} | NOT submitted: "
            f"{non_submitters or '(none)'} — a non-submitter gets a 15-round board",
        )

        effective = services.effective_keeper_selections(db, league, UPCOMING_SEASON)
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
                    FuturePick.season_year == UPCOMING_SEASON)
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

        proj_count = (
            db.query(PlayerProjection)
            .filter_by(season_year=UPCOMING_SEASON)
            .count()
        )
        check(
            f"player_projection rows exist for {UPCOMING_SEASON}",
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
