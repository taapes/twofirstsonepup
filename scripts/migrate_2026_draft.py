"""One-off migration: move the 2026 draft onto the 26/27 league row.

The 2026 draft ran BEFORE the rollover, which is by design — `get_draft_board` and
`effective_keeper_selections` both filter on `league_id`, so drafting on a
freshly-created row would have handed everyone a full un-reduced board with kept
players still available. The cost of that choice is that the draft's rows landed on
the OUTGOING 25/26 row carrying `season_year=2026`, and the rollover then created the
26/27 row with no draft history of its own. This moves them to the row they describe.

    python scripts/migrate_2026_draft.py                      # dry run, prints a plan
    python scripts/migrate_2026_draft.py --match display      # dry run, by person name
    python scripts/migrate_2026_draft.py --match display --apply

BLOCKED AS OF 2026-08-18 — READ THIS FIRST. The default `--match entry` pairs the two
rows' managers on `fpl_manager_id`, which this codebase documents as the stable
identity. Production says otherwise: the 25/26 row holds entry ids 5520-268927, the
26/27 row holds 58528-58537 (a contiguous, freshly-issued block), and the overlap is
ZERO. FPL issued every manager a new entry for the new season. The dry run therefore
aborts, correctly, having written nothing.

The same field is what `advance_season` matches on, so its identity carry and its
keeper carry BOTH silently did nothing at the rollover: the 26/27 row has no
display_names, no password_hashes (every manager's login is broken) and no
KeeperSeeds (every keeper clock lost). Fixing that is a prerequisite for this
migration, not a consequence of it.

Once `display_name` is set on the 26/27 managers, re-run with `--match display`. The
mapping cannot be derived here — FPL team names changed too, and several of the ten
are genuinely ambiguous ("Le Roi De Coupe" -> "Le Féez Nuts"?), so a wrong guess would
hand one manager another's draft.

WHAT MOVES (league_id retargeted, manager FKs remapped — see manager_remap):
    draft_picks           season_year == YEAR, every draft_type
    keeper_selections     season_year == YEAR
    draft_lottery         ALL rows on the source (the table has no season column —
                          the lottery is a property of the row, and the source row's
                          lottery IS this draft's)
    draft_order_override  season_year == YEAR

WHAT DOES NOT MOVE, deliberately:
    future_picks   Season-agnostic by design: a standing multi-year outlook, read
                   cross-league by /picks. They belong to no single season.
    trades         A historical record of when something happened. `get_trades`
                   already attributes a trade to a season ON READ
                   (`services._trade_season_year`), so moving the rows would make the
                   storage and the display disagree.
    rosters, standings, gameweeks, players, transactions
                   FPL-canonical. This script never touches the canonical side of the
                   two-truths boundary.
    keeper_seeds   REPORT ONLY — see below.

KEEPER SEEDS ARE REPORTED, NEVER MOVED. `advance_season` already writes the carried
seed onto the new row (years - 1) at rollover, so moving the old ones would double
them up and clobber a correctly-decremented clock with a stale one. Instead this
verifies that carry happened and prints what drifted; fix any finding by hand at
/admin/keepers. A drift finding sets a nonzero exit even on a successful move, so it
cannot be scrolled past.

Safe to re-run: the move is a no-op once the source row has nothing left to move.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models  # noqa: F401,E402 — registers tables on Base.metadata
from db import SessionLocal  # noqa: E402
from models import (  # noqa: E402
    DraftLottery,
    DraftOrderOverride,
    DraftPick,
    KeeperSeed,
    KeeperSelection,
    League,
    Manager,
)

YEAR = 2026


class Abort(RuntimeError):
    """A precondition failed. Nothing has been written."""


# ---- resolution -------------------------------------------------------------
def resolve_rows(db, year=YEAR):
    """(source, target). Resolved explicitly and unambiguously, never guessed.

    The source is the row the draft was RUN on — season_year == year - 1. The target
    is the row it describes — season_year == year, and it must be the current one, or
    we would be migrating onto an archived season.
    """
    src = db.query(League).filter(League.season_year == year - 1).all()
    tgt = db.query(League).filter(League.season_year == year).all()
    if len(src) != 1:
        raise Abort(
            f"expected exactly one league row with season_year={year - 1}, found "
            f"{len(src)}: {[l.fpl_league_id for l in src]}"
        )
    if len(tgt) != 1:
        raise Abort(
            f"expected exactly one league row with season_year={year}, found "
            f"{len(tgt)}: {[l.fpl_league_id for l in tgt]}"
        )
    source, target = src[0], tgt[0]
    if source.id == target.id:
        raise Abort("source and target are the same row")
    if not target.is_current:
        raise Abort(
            f"target row (fpl {target.fpl_league_id}, season {target.season_year}) is "
            "not is_current — refusing to migrate onto an archived season"
        )
    return source, target


def _pairing_index(db, source, target, match):
    """(old managers by id, target managers by identity key, key fn, label fn).

    Shared by `manager_remap` and `reconcile_seeds` so the two cannot pair managers
    by different rules — which is exactly what went wrong: reconcile_seeds kept its
    own fpl_manager_id lookup after the entry ids turned out to be reissued, and
    reported all 49 correctly-carried seeds as orphans.
    """
    old_by_id = {m.id: m for m in db.query(Manager).filter_by(league_id=source.id)}
    target_mgrs = db.query(Manager).filter_by(league_id=target.id).all()

    if match == "entry":
        key = lambda m: m.fpl_manager_id                              # noqa: E731
        label = lambda m: f"entry {m.fpl_manager_id}"                 # noqa: E731
    elif match == "display":
        blank = [m.name for m in target_mgrs if not (m.display_name or "").strip()]
        if blank:
            raise Abort(
                "--match display needs a display_name on every target manager; "
                f"{len(blank)} are blank: {', '.join(sorted(blank))}"
            )
        key = lambda m: (m.display_name or "").strip().casefold()     # noqa: E731
        label = lambda m: f"display {m.display_name!r}"               # noqa: E731
    else:
        raise Abort(f"unknown --match {match!r}")

    new_by_key = {}
    for m in target_mgrs:
        k = key(m)
        if k in new_by_key:
            raise Abort(f"target row has two managers with the same {match} key {k!r}")
        new_by_key[k] = m
    return old_by_id, new_by_key, key, label


def manager_remap(db, source, target, referenced_ids, match="entry"):
    """old manager uuid -> new manager uuid, bridged on a stable identity.

    `managers` has one row per manager PER SEASON, so every moved row's manager FK
    points at a 25/26 row that means the same person as a 26/27 row. Anything without
    a counterpart aborts: silently leaving the old FK would point a 26/27 pick at a
    manager on another league row, which every reader would render as a stranger.

    `match="entry"` uses `fpl_manager_id`, which the codebase treats as the stable
    identity. **It is not.** Verified against production on 2026-08-18: the 25/26 row
    holds entry ids 5520-268927 and the 26/27 row holds 58528-58537, a contiguous
    freshly-issued block, with ZERO overlap — FPL issued every manager a new entry for
    the new season. That is also why `advance_season`'s identity and keeper carries
    both silently did nothing (they match on the same field), leaving the 26/27 row
    with no display names, no password hashes and no keeper seeds.

    `match="display"` uses `display_name`, the league-CUSTOM person name, which is the
    only identity the app owns rather than borrows. It requires the commissioner to
    have set those names on the target row first — which is a prerequisite for
    restoring logins anyway, and is not something this script should guess at: the FPL
    team names changed too ("Le Roi De Coupe" -> ?), so several of the ten are not
    mechanically derivable.
    """
    old_by_id, new_by_key, key, label = _pairing_index(db, source, target, match)

    remap, missing = {}, []
    for mid in referenced_ids:
        if mid is None:
            continue
        om = old_by_id.get(mid)
        if om is None:
            missing.append(f"manager {mid} is not on the source row")
            continue
        nm = new_by_key.get(key(om))
        if nm is None:
            missing.append(f"{om.display} ({label(om)}) has no counterpart on the target row")
            continue
        remap[mid] = nm.id
    if missing:
        hint = ""
        # Judge "systemic" on whether ANY source manager pairs with ANY target one,
        # not on how many happened to be referenced by the rows being moved — a
        # single referenced manager failing looks identical otherwise.
        overlap = {key(m) for m in old_by_id.values()} & set(new_by_key)
        if match == "entry" and not overlap:
            hint = (
                "\n\nEVERY manager failed to match, which means FPL issued new entry "
                "ids for the new season rather than carrying them over. `fpl_manager_id` "
                "is therefore not a stable identity across seasons.\n"
                "Before re-running: set `display_name` on each manager of the target "
                "row (this also restores logins, which are broken for the same reason), "
                "then re-run with --match display."
            )
        raise Abort("manager remap incomplete:\n    " + "\n    ".join(missing) + hint)
    return remap, old_by_id, new_by_key


# ---- the movable sets -------------------------------------------------------
def collect(db, source, year=YEAR):
    picks = (
        db.query(DraftPick)
        .filter_by(league_id=source.id, season_year=year)
        .order_by(DraftPick.draft_type, DraftPick.pick_number)
        .all()
    )
    selections = (
        db.query(KeeperSelection).filter_by(league_id=source.id, season_year=year).all()
    )
    # No season column on this table: the lottery belongs to the league ROW, and the
    # source row's lottery is the one that set this draft's round-1 order.
    lottery = db.query(DraftLottery).filter_by(league_id=source.id).all()
    overrides = (
        db.query(DraftOrderOverride)
        .filter_by(league_id=source.id, season_year=year)
        .all()
    )
    return {
        "draft_picks": picks,
        "keeper_selections": selections,
        "draft_lottery": lottery,
        "draft_order_override": overrides,
    }


def check_collisions(db, target, batches, remap, year=YEAR):
    """Every unique constraint the move could violate, checked BEFORE any write.

    A mid-transaction IntegrityError would roll back cleanly, but the message names a
    constraint rather than the rows, and diagnosing it against production is exactly
    the situation to avoid.
    """
    problems = []

    # uq_draftpick_slot (league_id, season_year, draft_type, pick_number)
    taken = {
        (dp.draft_type, dp.pick_number)
        for dp in db.query(DraftPick).filter_by(league_id=target.id, season_year=year)
    }
    for dp in batches["draft_picks"]:
        if (dp.draft_type, dp.pick_number) in taken:
            problems.append(
                f"draft_picks: target already has {dp.draft_type} pick "
                f"#{dp.pick_number} for {year}"
            )

    # uq_keeper_sel_mgr_player_season (manager_id, player_id, season_year) — NOT
    # league-scoped, so the collision is against the REMAPPED manager.
    sel_taken = {
        (ks.manager_id, ks.player_id)
        for ks in db.query(KeeperSelection).filter_by(
            league_id=target.id, season_year=year
        )
    }
    for ks in batches["keeper_selections"]:
        if (remap.get(ks.manager_id), ks.player_id) in sel_taken:
            problems.append(
                f"keeper_selections: target already has manager "
                f"{remap.get(ks.manager_id)} / player {ks.player_id} for {year}"
            )

    # draft_lottery has no unique constraint, but two rows for one manager on the
    # target would silently double an entry in the round-1 order.
    lot_taken = {
        dl.manager_id for dl in db.query(DraftLottery).filter_by(league_id=target.id)
    }
    for dl in batches["draft_lottery"]:
        if remap.get(dl.manager_id) in lot_taken:
            problems.append(
                f"draft_lottery: target already has a row for manager "
                f"{remap.get(dl.manager_id)}"
            )

    # uq_draft_order_override_slot (league_id, season_year, draft_type, round, position)
    ov_taken = {
        (o.draft_type, o.round, o.position)
        for o in db.query(DraftOrderOverride).filter_by(
            league_id=target.id, season_year=year
        )
    }
    for o in batches["draft_order_override"]:
        if (o.draft_type, o.round, o.position) in ov_taken:
            problems.append(
                f"draft_order_override: target already has {o.draft_type} r{o.round} "
                f"pos {o.position} for {year}"
            )

    if problems:
        raise Abort("target row already holds conflicting rows:\n    "
                    + "\n    ".join(problems))


# ---- keeper seed reconciliation (report only) -------------------------------
def reconcile_seeds(db, source, target, year=YEAR, match="entry"):
    """Did `advance_season`'s keeper carry actually happen? Report, never fix.

    The expectation: for every keeper SELECTION for `year` (the players actually
    kept), the target row should hold a KeeperSeed for the remapped manager and the
    same player, with `years_remaining` one less than the source seed's — that is the
    clock ticking. A source seed whose player was NOT kept is expected to have no
    counterpart and is not a finding.
    """
    # Pairs managers by the SAME rule the move does. It used to keep its own
    # fpl_manager_id lookup, which silently paired nothing once FPL reissued the entry
    # ids — every correctly-carried seed was then reported as an orphan.
    old_by_id, new_by_key, key, _label = _pairing_index(db, source, target, match)
    src_seeds = {
        (s.manager_id, s.player_id): s
        for s in db.query(KeeperSeed).filter_by(league_id=source.id)
        if s.player_id is not None
    }
    tgt_seeds: dict = {}
    dupes = []
    for s in db.query(KeeperSeed).filter_by(league_id=target.id):
        if s.player_id is None:
            continue
        seed_key = (s.manager_id, s.player_id)
        if seed_key in tgt_seeds:
            dupes.append(seed_key)
        tgt_seeds[seed_key] = s

    # A selection whose player the manager no longer holds is NOT expected to have a
    # carried seed: advance_season deliberately writes nothing rather than invent a
    # clock for a player he traded away. Reporting it as "missing" every run is a
    # false alarm, and a report that cries wolf is one nobody reads.
    import services  # local: keeps this script importable without a DB configured
    held = services._derive_keeper_status(db, source)

    missing, mismatched, orphaned, not_expected = [], [], [], []
    expected_keys = set()
    for ks in db.query(KeeperSelection).filter_by(league_id=source.id, season_year=year):
        if ks.player_id is None:      # a kept goalie team carries on its own clock
            continue
        if held.get(ks.manager_id, {}).get(ks.player_id) is None:
            om0 = old_by_id.get(ks.manager_id)
            not_expected.append(
                f"{om0.display if om0 else ks.manager_id} / player {ks.player_id}: "
                "no longer held (traded away), so no seed is due")
            continue
        om = old_by_id.get(ks.manager_id)
        nm = new_by_key.get(key(om)) if om else None
        if nm is None:
            continue
        who = om.display if om else str(ks.manager_id)
        # NOT `key` — that name is the pairing FUNCTION above, and rebinding it here
        # made the second loop iteration call a tuple.
        pair_key = (nm.id, ks.player_id)
        expected_keys.add(pair_key)
        tgt = tgt_seeds.get(pair_key)
        if tgt is None:
            missing.append(f"{who} / player {ks.player_id}: no carried seed on target")
            continue
        src = src_seeds.get((ks.manager_id, ks.player_id))
        if src is not None and tgt.years_remaining != max(src.years_remaining - 1, 0):
            mismatched.append(
                f"{who} / player {ks.player_id}: target has "
                f"{tgt.years_remaining}, expected {max(src.years_remaining - 1, 0)} "
                f"(source {src.years_remaining} - 1)"
            )

    for seed_key in tgt_seeds:
        if seed_key not in expected_keys:
            orphaned.append(
                f"manager {seed_key[0]} / player {seed_key[1]}: "
                f"seed with no {year} selection")

    return {
        "source_seeds": len(src_seeds),
        "target_seeds": len(tgt_seeds),
        "expected": len(expected_keys),
        "missing": missing,
        "mismatched": mismatched,
        "orphaned": orphaned,
        "not_expected": not_expected,
        "duplicated": [f"manager {a} / player {b}" for a, b in dupes],
    }


# ---- the move ---------------------------------------------------------------
def apply_move(batches, target, remap):
    """Retarget league_id and remap every manager FK. Caller owns the transaction."""
    moved = {}
    for name, rows in batches.items():
        for row in rows:
            row.league_id = target.id
            if getattr(row, "manager_id", None) is not None:
                row.manager_id = remap[row.manager_id]
        moved[name] = len(rows)
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="execute (default is a dry run that writes nothing)")
    ap.add_argument("--year", type=int, default=YEAR)
    ap.add_argument("--match", choices=("entry", "display"), default="entry",
                    help="identity used to pair managers across the two rows "
                         "(default: entry = fpl_manager_id)")
    args = ap.parse_args()
    year = args.year

    db = SessionLocal()
    try:
        source, target = resolve_rows(db, year)
        print(f"source : {source.name!r}  fpl={source.fpl_league_id}  "
              f"season_year={source.season_year}  locked={source.sync_locked}")
        print(f"target : {target.name!r}  fpl={target.fpl_league_id}  "
              f"season_year={target.season_year}  current={target.is_current}")
        print(f"moving : rows describing the {year} draft\n")

        batches = collect(db, source, year)
        referenced = {
            getattr(r, "manager_id", None) for rows in batches.values() for r in rows
        }
        remap, old_by_id, _new = manager_remap(db, source, target, referenced,
                                               match=args.match)

        print("row counts to move:")
        for name, rows in batches.items():
            extra = ""
            if name == "draft_picks":
                kinds: dict = {}
                for r in rows:
                    kinds[r.draft_type] = kinds.get(r.draft_type, 0) + 1
                extra = "  " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
            print(f"    {name:<22} {len(rows):>4}{extra}")
        print()

        print(f"manager remap via {args.match} ({len(remap)} referenced):")
        for old_id, new_id in sorted(remap.items(), key=lambda kv: old_by_id[kv[0]].display):
            om = old_by_id[old_id]
            print(f"    {om.display:<14} entry {om.fpl_manager_id:<8} {old_id} -> {new_id}")
        print()

        check_collisions(db, target, batches, remap, year)
        print("collision check: clear\n")

        rec = reconcile_seeds(db, source, target, year, match=args.match)
        print(f"keeper seed reconciliation (REPORT ONLY — nothing is written):")
        print(f"    seeds on source        {rec['source_seeds']}")
        print(f"    seeds on target        {rec['target_seeds']}")
        print(f"    expected carried seeds {rec['expected']}  "
              f"(one per {year} keeper selection)")
        drift = False
        if rec["not_expected"]:
            print(f"    not due ({len(rec['not_expected'])}, traded away after "
                  "submitting — this is correct, not drift):")
            for line in rec["not_expected"]:
                print(f"        {line}")
        for label in ("missing", "mismatched", "duplicated", "orphaned"):
            rows = rec[label]
            if not rows:
                continue
            drift = True
            print(f"    {label} ({len(rows)}):")
            for line in rows[:20]:
                print(f"        {line}")
            if len(rows) > 20:
                print(f"        ... and {len(rows) - 20} more")
        if not drift:
            print("    no drift")
        print()

        if not args.apply:
            print("DRY RUN — nothing written. Re-run with --apply to execute.")
            return 1 if drift else 0

        moved = apply_move(batches, target, remap)
        db.commit()
        print("APPLIED (one transaction):")
        for name, n in moved.items():
            print(f"    {name:<22} {n:>4} moved")
        if drift:
            print("\nSeed drift was reported above and NOT fixed — "
                  "correct it by hand at /admin/keepers.")
        return 1 if drift else 0

    except Abort as e:
        db.rollback()
        print(f"ABORTED: {e}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
