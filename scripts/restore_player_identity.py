"""One-off repair: put 25/26 player identity back after the 26/27 pool overwrote it.

`players` is a global table, but FPL reassigns element ids every season. When the
Aug-2026 sync pulled the 26/27 bootstrap, `_upsert(..., {"fpl_id": ...})` rewrote
each existing row in place — so the UUID that was Gabriel became J.Timber, and
every `rosters` row (which points at players.id) started resolving to the wrong
player. Rosters and gameweek_points were never touched; only the names behind
them moved.

The pre-incident snapshot is the 25/26 truth. It predates the rich-stats columns,
so those can't be restored — they're cleared rather than left showing another
player's numbers.

    python scripts/restore_player_identity.py            # dry run
    python scripts/restore_player_identity.py --apply

Matching is by players.id (the UUID is stable; only the attributes were rewritten).
"""

import argparse
import datetime
import json
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal  # noqa: E402

BASELINE = "snapshots/baseline.json"

# Columns the snapshot can restore verbatim.
RESTORE = [
    "fpl_id", "name", "position", "current_team", "status", "price",
    "last_season_points", "fpl_added_date", "is_eligible",
]
# Season stats added to the schema after the snapshot was taken. They currently
# hold the *next* season's preseason numbers for the *wrong* player, which is
# worse than showing nothing.
CLEAR = [
    "form", "points_per_game", "total_points", "goals_scored", "assists",
    "clean_sheets", "bonus", "minutes", "ict_index", "selected_by_percent", "news",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument("--baseline", default=BASELINE)
    args = ap.parse_args()

    with open(args.baseline) as f:
        players = json.load(f)["players"]
    print(f"baseline: {len(players)} players\n")

    db = SessionLocal()
    try:
        current = {
            str(r.id): dict(r._mapping)
            for r in db.execute(text("select id, fpl_id, name, position, current_team "
                                     "from players"))
        }
        missing = [p for p in players if str(p["id"]) not in current]
        renamed = [p for p in players
                   if str(p["id"]) in current
                   and current[str(p["id"])]["name"] != p["name"]]

        print(f"rows to restore : {len(players) - len(missing)}")
        print(f"missing in DB   : {len(missing)}")
        print(f"names to correct: {len(renamed)}")
        for p in renamed[:10]:
            c = current[str(p["id"])]
            print(f"  {c['name']:<18} ({c['current_team']})  ->  "
                  f"{p['name']:<18} ({p['current_team']})")
        if len(renamed) > 10:
            print(f"  ... +{len(renamed) - 10} more")
        print(f"\nstat columns cleared (unrecoverable): {', '.join(CLEAR)}")

        if not args.apply:
            print("\nDRY RUN — re-run with --apply to execute.")
            return

        sets = ", ".join(f"{c}=:{c}" for c in RESTORE)
        sets += ", " + ", ".join(f"{c}=NULL" for c in CLEAR)
        for p in players:
            if str(p["id"]) not in current:
                continue
            params = {c: p.get(c) for c in RESTORE}
            if params.get("fpl_added_date"):
                params["fpl_added_date"] = datetime.date.fromisoformat(
                    params["fpl_added_date"]
                )
            params["id"] = p["id"]
            db.execute(text(f"update players set {sets} where id=:id"), params)
        db.commit()
        print(f"\nAPPLIED. Restored {len(players) - len(missing)} players "
              f"to their 25/26 identity.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
