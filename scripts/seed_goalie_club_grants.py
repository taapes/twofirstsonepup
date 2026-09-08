"""Seed `goalie_club_grants` — the 2026-only house rule where each manager holds two
Premier League clubs' worth of goalkeeper rights, not two individual keepers.

WHY THIS EXISTS. Nothing about the draft recorded this: the 26/27 draft used ordinary
individual GKP picks (`goalie_team_mode` correctly stays 'off' — squad shape is
untouched, still 15 players / 2 GKP). So there is no `DraftPick.team_id` to derive
ownership from, unlike the older single-club `keeper`/`redraft` modes. The concrete
problem this closes: a real-world transfer at an owned club forces a manual FPL trade
to keep a manager's roster in sync (FPL has no concept of "own a club"), and that trade
is indistinguishable from a real one — `services.classify_goalkeeper_continuity_trades`
— until this table exists.

WHAT THE INFERENCE IS. Each manager's CURRENT two `GKP`-slot roster clubs. On the real
26/27 data this partitions all 20 Premier League clubs with zero duplicates — a strong
prior, not a certainty, and specifically not proof for a manager whose ownership has
already changed hands more than once (a club can look briefly wrong mid-transition,
the way Gaby's Villa slot passed through Sanchez before landing on Suzuki). Reviewed
by the commissioner before anything is written — this NEVER auto-applies.

Usage:
    python scripts/seed_goalie_club_grants.py
    python scripts/seed_goalie_club_grants.py --league 11818
    python scripts/seed_goalie_club_grants.py --apply

Dry-run by default, like the other scripts here. Idempotent: re-running with --apply
after a correction just leaves the already-correct grants alone and fills in the rest
(services.set_goalie_club_grant refuses to double-grant a club, so re-pointing one that
was set wrong needs a manual fix first — see that function's docstring).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import League, Manager, PlTeam  # noqa: E402
from rules import RuleViolation  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", help="fpl_league_id (default: the current league)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        if args.league:
            league = db.query(League).filter_by(fpl_league_id=args.league).one_or_none()
        else:
            league = db.query(League).filter_by(is_current=True).first()
        if league is None:
            print("!! no such league", file=sys.stderr)
            return 2
        print(f"target league: {league.season_year} ({league.fpl_league_id}) "
              f"{league.name!r}")

        rows = services.inferred_goalie_club_grants(db, league)
        if not rows:
            print("!! nothing inferred — no current-gameweek GKP rosters found",
                  file=sys.stderr)
            return 2

        by_club = {}
        for r in rows:
            by_club.setdefault(r["team_name"], []).append(r["manager_name"])
        dupes = {c: ms for c, ms in by_club.items() if len(ms) > 1}
        print(f"\n{len(rows)} club(s) inferred, covering {len(by_club)} distinct club(s):")
        for r in sorted(rows, key=lambda r: (r["manager_name"], r["team_name"])):
            print(f"  {r['manager_name']:12s} -> {r['team_name']}")
        if dupes:
            print("\n!! REFUSING: more than one manager inferred for the same club — "
                  "this needs a human to sort out, not a script:", file=sys.stderr)
            for c, ms in dupes.items():
                print(f"     {c}: {ms}", file=sys.stderr)
            return 2

        print("\nThis is an INFERENCE from current rosters, not a record of the "
              "actual draft-day agreement. Confirm every row above is right before "
              "applying — a wrong entry moves a real keeper-clock decision onto the "
              "wrong manager with nothing downstream to flag it.")

        if not args.apply:
            print("\n(dry run — pass --apply to write)")
            return 0

        applied, skipped = 0, 0
        for r in rows:
            team = db.get(PlTeam, r["team_id"])
            manager = db.get(Manager, r["manager_id"])
            try:
                services.set_goalie_club_grant(
                    db, league, fpl_manager_id=manager.fpl_manager_id,
                    team_code=team.code, season_year=league.season_year,
                )
                applied += 1
            except RuleViolation as e:
                print(f"  skipped {r['manager_name']} / {r['team_name']}: {e}")
                skipped += 1
        print(f"\napplied {applied}, skipped {skipped}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
