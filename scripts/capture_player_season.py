"""One-off: freeze the completed 25/26 season's player identity into player_season.

`players` is global and mutable — it always holds whatever season synced last. The
25/26 identity currently sitting there was restored by scripts/restore_player_identity.py
after the Aug-2026 incident, and it is the LAST copy: once a 26/27 sync runs,
`players` moves on and nothing else records who wore what shirt in 25/26.

    python scripts/capture_player_season.py            # dry run
    python scripts/capture_player_season.py --apply

MUST run before the 26/27 rollover unfreezes a league (which un-gates sync_players).

NOTE ON STATS: the rich stat columns (form, points_per_game, total_points,
goals_scored, assists, clean_sheets, bonus, minutes, ict_index,
selected_by_percent, news) are ALL NULL for every row, on purpose —
restore_player_identity.py cleared them because after the incident they held the
*wrong player's* 26/27 numbers, and showing nothing beats showing someone else's.
841 rows with a fully NULL stat block is the CORRECT outcome; it matches what the
site renders today. Do NOT "repair" them from a live FPL feed: that feed is keyed
on reassigned element ids, so it would write another player's numbers permanently
into a frozen snapshot — the exact bug this whole change exists to prevent.
"""

import argparse
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal  # noqa: E402
from models import PlayerSeason  # noqa: E402

FPL_LEAGUE_ID = "1754"  # 25/26

# Everything player_season stores, in players' own column names.
COLUMNS = [
    "fpl_id", "name", "position", "current_team", "price", "status", "news",
    "total_points", "goals_scored", "assists", "clean_sheets", "bonus",
    "minutes", "form", "points_per_game", "ict_index", "selected_by_percent",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument("--league", default=FPL_LEAGUE_ID, help="fpl_league_id to capture")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        row = db.execute(
            text("select id, season_year, sync_locked from leagues "
                 "where fpl_league_id=:f"),
            {"f": args.league},
        ).one_or_none()
        if row is None:
            sys.exit(f"no league with fpl_league_id={args.league}")
        league_id, season_year, sync_locked = row
        if not sync_locked:
            sys.exit(
                f"league {args.league} ({season_year}) is NOT sync_locked. This script "
                "captures a FINISHED season; a live one is maintained by sync_players."
            )

        already = db.execute(
            text("select count(*) from player_season where league_id=:l"),
            {"l": league_id},
        ).scalar_one()
        if already:
            sys.exit(
                f"player_season already has {already} rows for league {args.league}. "
                "Refusing to double-capture — delete them first if this is a re-run."
            )

        players = db.execute(
            text(f"select id as player_id, {', '.join(COLUMNS)} from players "
                 "where fpl_id is not null")
        ).mappings().all()

        print(f"league {args.league} (season {season_year}), sync_locked=True")
        print(f"players to capture: {len(players)}\n")
        for p in players[:5]:
            print(f"  {p['name']:<20} {str(p['position']):<4} {p['current_team']}")
        if len(players) > 5:
            print(f"  ... +{len(players) - 5} more")

        stats = ["total_points", "minutes", "form", "news"]
        filled = {c: sum(1 for p in players if p[c] is not None) for c in stats}
        print(f"\nstat columns populated (expected to be 0 — see module docstring):")
        for c, n in filled.items():
            print(f"  {c:<16} {n}")

        if not args.apply:
            print("\nDRY RUN — re-run with --apply to execute.")
            return

        # Insert through the ORM so players.id's Python-side uuid4 default applies
        # (player_season.id has no server default — see the migration).
        for p in players:
            db.add(PlayerSeason(league_id=league_id, **dict(p)))
        db.commit()
        print(f"\nAPPLIED. Captured {len(players)} player_season rows for "
              f"league {args.league} (season {season_year}).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
