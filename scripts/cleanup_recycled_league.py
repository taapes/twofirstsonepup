"""One-off repair: undo the Aug-2026 sync of a recycled FPL league id.

FPL handed our finished 25/26 league id (1754) to an unrelated league
('Rottehulen'). Three nightly syncs upserted that feed into our season row,
which (a) added the stranger's managers/standings/fixtures and (b) overwrote our
league name, season year, draft date and gameweek calendar.

Our own data survived — this only removes what the foreign feed added and puts
the overwritten scalars back from the pre-incident snapshot.

    python scripts/cleanup_recycled_league.py            # dry run, prints a plan
    python scripts/cleanup_recycled_league.py --apply    # execute in one txn

The set of legitimate managers is taken from the snapshot, not guessed.
"""

import argparse
import datetime
import json
import os
import sys

from sqlalchemy import inspect, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models  # noqa: F401,E402 — registers tables on Base.metadata
from db import Base, SessionLocal, engine  # noqa: E402

BASELINE = "snapshots/baseline.json"
FPL_LEAGUE_ID = "1754"


def load_baseline(path):
    with open(path) as f:
        data = json.load(f)
    league = next(
        r for r in data["leagues"] if str(r["fpl_league_id"]) == FPL_LEAGUE_ID
    )
    keep = {str(m["fpl_manager_id"]) for m in data["managers"]}
    gws = {int(g["number"]): g for g in data["gameweeks"]}
    return league, keep, gws


def manager_referencing_tables():
    """Every (table, column) with a FK to managers.id, so nothing is orphaned."""
    insp = inspect(engine)
    out = []
    for t in Base.metadata.sorted_tables:
        for fk in t.foreign_keys:
            if fk.column.table.name == "managers":
                out.append((t.name, fk.parent.name))
    del insp
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument("--baseline", default=BASELINE)
    args = ap.parse_args()

    league_row, keep_ids, gws = load_baseline(args.baseline)
    print(f"baseline: league '{league_row['name']}' "
          f"({league_row['season_year']}), {len(keep_ids)} managers, {len(gws)} GWs\n")

    db = SessionLocal()
    try:
        lid = db.execute(
            text("select id from leagues where fpl_league_id=:f"),
            {"f": FPL_LEAGUE_ID},
        ).scalar_one()

        errant = db.execute(
            text("select id, fpl_manager_id, name from managers "
                 "where league_id=:l and fpl_manager_id not in :keep"),
            {"l": lid, "keep": tuple(keep_ids)},
        ).all()
        errant_ids = [r.id for r in errant]
        print(f"errant managers ({len(errant)}):")
        for r in errant:
            print(f"  - {r.fpl_manager_id:>8}  {r.name!r}")

        if not errant_ids:
            print("\nnothing to remove.")
        else:
            print("\nrows referencing them:")
            for tbl, col in manager_referencing_tables():
                n = db.execute(
                    text(f"select count(*) from {tbl} where {col} in :ids"),
                    {"ids": tuple(errant_ids)},
                ).scalar_one()
                if n:
                    print(f"  {tbl}.{col}: {n}")

        # scalars the foreign feed overwrote
        cur = db.execute(
            text("select name, season_year, draft_date, sync_locked "
                 "from leagues where id=:l"), {"l": lid},
        ).one()
        print(f"\nleague row:\n  now:  {cur.name!r} {cur.season_year} "
              f"{cur.draft_date} sync_locked={cur.sync_locked}\n"
              f"  ->    {league_row['name']!r} {league_row['season_year']} "
              f"{league_row['draft_date']} sync_locked=True")

        bad_gws = db.execute(
            text("select number, start_date, end_date from gameweeks "
                 "where league_id=:l order by number"), {"l": lid},
        ).all()
        drift = [
            g for g in bad_gws
            if g.start_date and gws.get(g.number)
            and g.start_date.isoformat() != gws[g.number]["start_date"]
        ]
        print(f"\ngameweeks with drifted dates: {len(drift)} of {len(bad_gws)}")
        for g in drift[:3]:
            print(f"  GW{g.number}: {g.start_date} -> {gws[g.number]['start_date']}")
        if len(drift) > 3:
            print(f"  ... +{len(drift) - 3} more")

        # PL fixtures are league-scoped and were replaced wholesale with next
        # season's. The 25/26 ones predate the snapshot so they can't be restored;
        # the next season's belong to the next season's league row, not this one.
        stale_fixtures = db.execute(
            text("select count(*) from fixtures where league_id=:l "
                 "and kickoff_time >= :cut"),
            {"l": lid, "cut": f"{league_row['season_year'] + 1}-07-01"},
        ).scalar_one()
        print(f"\nnext-season fixtures filed under this season: {stale_fixtures}")

        if not args.apply:
            print("\nDRY RUN — re-run with --apply to execute.")
            return

        # ---- apply, one transaction ----
        if errant_ids:
            for tbl, col in manager_referencing_tables():
                db.execute(
                    text(f"delete from {tbl} where {col} in :ids"),
                    {"ids": tuple(errant_ids)},
                )
            db.execute(
                text("delete from managers where id in :ids"),
                {"ids": tuple(errant_ids)},
            )
        db.execute(
            text("update leagues set name=:n, season_year=:y, draft_date=:d, "
                 "sync_locked=true where id=:l"),
            {"n": league_row["name"], "y": league_row["season_year"],
             "d": datetime.date.fromisoformat(league_row["draft_date"]), "l": lid},
        )
        for num, g in gws.items():
            db.execute(
                text("update gameweeks set start_date=:s, end_date=:e "
                     "where league_id=:l and number=:n"),
                {"s": g["start_date"], "e": g["end_date"], "l": lid, "n": num},
            )
        db.execute(
            text("delete from fixtures where league_id=:l and kickoff_time >= :cut"),
            {"l": lid, "cut": f"{league_row['season_year'] + 1}-07-01"},
        )
        db.commit()
        print("\nAPPLIED. Season 2025 is frozen (sync_locked=true).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
