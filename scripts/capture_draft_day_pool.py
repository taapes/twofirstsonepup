"""Seed a season's draft-day player pool from a pre-draft snapshot.

WHY THIS EXISTS. `services.snapshot_player_pool` captures the set of element ids that
existed on draft day, and `services.flag_ineligible` flags any later non-defender
arrival as ineligible. But `snapshot_player_pool`'s only caller is the last statement
of the rollover route, and the 26/27 rollover ran 2026-08-17/18 — BEFORE the NameError
that had always broken that function was fixed on 08-20. So 26/27 has an empty pool,
`flag_ineligible` returns 0 on it by design, and the rule has been silently switched
off all season. Nothing re-runs the capture until the 2027 rollover.

WHY NOT JUST CALL snapshot_player_pool TODAY. It seeds from the CURRENT `players`
table. The 26/27 draft was 2026-08-16 and 23 players have been added since, so a
capture today would record those 23 as draft-day-eligible — permanently unflaggable.
Fourteen of them are non-defenders, i.e. exactly the players the rule exists to catch.

WHAT THIS DOES INSTEAD. Reads the pool out of a pre-draft snapshot file, which is the
real draft-day fact, and writes those ids to `player_pool_snapshot`.

ELEMENT-ID SAFETY. FPL reassigns element ids every August, and this draft ran before
the rollover, so a snapshot's `fpl_id` is not automatically today's `fpl_id`. This
script therefore refuses to run unless the snapshot's (fpl_id -> code) mapping agrees
with the database's for every shared id — `code` being FPL's permanent player id. On
the 26/27 data all 587 agree, but the check is the point: it fails closed if a future
caller points it at a snapshot from the wrong side of a rollover.

Usage:
    python scripts/capture_draft_day_pool.py snapshots/pre-draft-start-20260816.json
    python scripts/capture_draft_day_pool.py <file> --apply
    python scripts/capture_draft_day_pool.py <file> --league 11818 --apply

Dry-run by default, like the other scripts here. Idempotent: re-running adds nothing.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal  # noqa: E402
from models import League, Player, PlayerPoolSnapshot  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot", help="path to a PRE-DRAFT snapshot json")
    ap.add_argument("--league", help="fpl_league_id (default: the current league)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    with open(args.snapshot) as fh:
        snap = json.load(fh)
    if "players" not in snap:
        print(f"!! {args.snapshot} has no 'players' table", file=sys.stderr)
        return 2

    snap_rows = [p for p in snap["players"] if p.get("fpl_id") is not None]
    snap_fid_to_code = {p["fpl_id"]: p.get("code") for p in snap_rows}
    print(f"snapshot: {args.snapshot}")
    print(f"  player rows with an fpl_id: {len(snap_rows)}")

    db = SessionLocal()
    try:
        if args.league:
            league = db.query(League).filter_by(fpl_league_id=args.league).one_or_none()
        else:
            league = db.query(League).filter_by(is_current=True).first()
        if league is None:
            print("!! no such league", file=sys.stderr)
            return 2
        print(f"  target league: {league.season_year} ({league.fpl_league_id}) {league.name!r}")

        # --- the id-space guard -------------------------------------------------
        today = {
            fid: code
            for fid, code in db.query(Player.fpl_id, Player.code).filter(
                Player.fpl_id.isnot(None)
            )
        }
        shared = set(snap_fid_to_code) & set(today)
        mismatched = [f for f in shared if snap_fid_to_code[f] != today[f]]
        print(f"  element-id check: {len(shared)} shared ids, {len(mismatched)} mismatched")
        if mismatched:
            print(
                "!! REFUSING: this snapshot is from a different element-id generation "
                f"({len(mismatched)} ids now mean a different player). Seeding it would "
                "record the wrong pool.",
                file=sys.stderr,
            )
            return 1
        if not shared:
            print("!! REFUSING: no overlap with the current pool at all.", file=sys.stderr)
            return 1

        have = {
            fid
            for (fid,) in db.query(PlayerPoolSnapshot.fpl_id).filter_by(league_id=league.id)
        }
        todo = sorted(set(snap_fid_to_code) - have)
        print(f"  already recorded: {len(have)}")
        print(f"  to write:         {len(todo)}")

        # What the rule will say once the pool exists.
        pool = set(snap_fid_to_code)
        would_flag = [
            p
            for p in db.query(Player).filter(Player.fpl_id.isnot(None))
            if p.fpl_id not in pool and (p.position or "").upper() != "DEF"
        ]
        print(f"\n  post-draft non-defenders that become flaggable: {len(would_flag)}")
        for p in sorted(would_flag, key=lambda x: ((x.position or ""), x.name)):
            print(f"    {(p.position or '?'):<4} {p.name}")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply.")
            return 0

        for fid in todo:
            db.add(PlayerPoolSnapshot(league_id=league.id, fpl_id=fid))
        db.commit()
        print(f"\nWROTE {len(todo)} pool row(s) for {league.season_year}.")
        print("The ineligible-player rule fires on the next full sync "
              "(or run services.flag_ineligible now).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
