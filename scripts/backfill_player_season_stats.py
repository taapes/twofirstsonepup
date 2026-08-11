"""Recover 2025/26 season statistics into player_season.

After the Aug-2026 recycled-id incident, scripts/restore_player_identity.py NULLed
every stat column on `players` — correctly, because FPL had reassigned element ids
and those columns held the WRONG player's numbers. Those NULLs were then captured
into player_season, so the 25/26 snapshot has identity but no statistics.

FPL still serves the real numbers. `element-summary/{id}` returns `history_past`,
one row per season, and each row carries `element_code` — the same permanent id we
backfilled into `players.code`. So the join is exact, with no name matching:

    history_past[season_name == "2025/26"].element_code  ==  players.code

    python scripts/backfill_player_season_stats.py            # dry run
    python scripts/backfill_player_season_stats.py --apply
    python scripts/backfill_player_season_stats.py --verify   # compare vs snapshot

Re-runnable: rows that already have total_points are skipped, so a run interrupted
partway through resumes where it left off.

WHY NOT just read bootstrap-static (one request instead of ~577)? Because right now
it happens to still serve LAST season's totals — a preseason artifact that resets to
zero at GW1. It is undocumented, ambiguous, and self-destructs. element-summary names
the season explicitly and keeps working afterwards. The single exception is
points_per_game, which history_past does not carry; the bootstrap's value IS the
25/26 PPG while we are still in preseason, so it is captured in the same run —
but only before SEASON_26_27_GW1, after which a re-run leaves it alone rather than
writing this season's number into last season's frozen snapshot.

NOT WRITTEN, deliberately:
  form                 rolling 30-day metric — meaningless for a finished season
                       (it reads 0.0 for every player right now)
  selected_by_percent  point-in-time ownership, not a season total
  price / status       already populated 841/841 from the identity snapshot
"""

# truststore must patch ssl before any HTTP client exists — this machine sits behind
# a TLS-inspecting corporate proxy (dev-only; Render has no proxy).
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover
    print("WARNING: truststore missing; FPL fetches will fail behind the corporate "
          "proxy with CERTIFICATE_VERIFY_FAILED. Run: uv pip install truststore")

import argparse  # noqa: E402
import datetime  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

from sqlalchemy import text  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal  # noqa: E402

BASE = "https://fantasy.premierleague.com/api"
SEASON = "2025/26"
FPL_LEAGUE_ID = "1754"
SNAPSHOT = "snapshots/pre-cleanup-20260810.json"

# history_past field -> player_season column
FROM_HISTORY = {
    "total_points": "total_points",
    "minutes": "minutes",
    "goals_scored": "goals_scored",
    "assists": "assists",
    "clean_sheets": "clean_sheets",
    "bonus": "bonus",
    "ict_index": "ict_index",
}
# A wrong-season pull (e.g. after the GW1 reset) would show a tiny top score.
MIN_PLAUSIBLE_TOP_SCORE = 100

# points_per_game is the one column not in history_past. The live bootstrap carries
# the 25/26 value ONLY until the 26/27 season starts, after which it resets and would
# silently write this season's PPG into last season's frozen snapshot. This script is
# advertised as re-runnable (to pick up rows a later code backfill matches), so a
# re-run after this date must degrade to leaving PPG NULL rather than lying.
SEASON_26_27_GW1 = datetime.date(2026, 8, 21)


def _get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def fetch_stats(elements, pause: float, capture_ppg: bool):
    """{element_code: {column: value}} for `SEASON`, from element-summary."""
    out, missing, failed = {}, [], []
    total = len(elements)
    for i, e in enumerate(elements, 1):
        try:
            hp = _get(f"{BASE}/element-summary/{e['id']}/", timeout=20).get(
                "history_past", []
            )
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            failed.append((e["web_name"], str(ex)[:60]))
            continue
        row = next((h for h in hp if h.get("season_name") == SEASON), None)
        if row is None:
            missing.append(e["web_name"])
            continue
        vals = {col: row.get(src) for src, col in FROM_HISTORY.items()}
        # points_per_game is not in history_past; only trust the bootstrap's value
        # while it still reflects last season (see SEASON_26_27_GW1).
        if capture_ppg:
            ppg = e.get("points_per_game")
            vals["points_per_game"] = ppg if ppg not in (None, "") else None
        out[e["code"]] = vals
        if i % 100 == 0 or i == total:
            print(f"  ...{i}/{total} fetched", flush=True)
        time.sleep(pause)
    return out, missing, failed


def verify(db, league_id):
    """Cross-check what's stored against the pre-incident snapshot (name+team)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        SNAPSHOT)
    if not os.path.exists(path):
        sys.exit(f"snapshot not found: {SNAPSHOT}")
    with open(path) as f:
        snap = json.load(f)["players"]
    snap_by = {}
    for p in snap:
        if p.get("total_points") is not None:
            snap_by.setdefault((p["name"], p["current_team"]), []).append(p)

    rows = db.execute(text(
        "select name, current_team, total_points, minutes, goals_scored, assists "
        "from player_season where league_id=:l and total_points is not null"
    ), {"l": league_id}).mappings().all()

    checked = agree = 0
    diffs = []
    for r in rows:
        cands = snap_by.get((r["name"], r["current_team"]))
        if not cands or len(cands) > 1:      # absent or ambiguous surname
            continue
        s = cands[0]
        checked += 1
        if (s["total_points"] == r["total_points"] and s["minutes"] == r["minutes"]):
            agree += 1
        else:
            diffs.append((r["name"], r["total_points"], s["total_points"],
                          r["minutes"], s["minutes"]))
    print(f"\ncross-check vs {SNAPSHOT} (unambiguous name+team matches):")
    print(f"  compared : {checked}")
    print(f"  agree    : {agree}  ({agree / checked:.1%})" if checked else "  agree: -")
    if diffs:
        print(f"  differ   : {len(diffs)}")
        print(f"    {'player':<18} {'ours':>6} {'snap':>6} {'ourMin':>7} {'snapMin':>8}")
        for d in diffs[:15]:
            print(f"    {d[0]:<18} {str(d[1]):>6} {str(d[2]):>6} {str(d[3]):>7} {str(d[4]):>8}")
    return checked, agree


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument("--verify", action="store_true",
                    help="only cross-check stored values against the snapshot")
    ap.add_argument("--pause", type=float, default=0.05,
                    help="seconds between FPL requests (default 0.05)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        league_id, season_year = db.execute(text(
            "select id, season_year from leagues where fpl_league_id=:f"
        ), {"f": FPL_LEAGUE_ID}).one()

        if args.verify:
            verify(db, league_id)
            return

        print(f"target: league {FPL_LEAGUE_ID} (season {season_year}) — {SEASON} stats\n")
        capture_ppg = datetime.date.today() < SEASON_26_27_GW1
        if not capture_ppg:
            print(f"NOTE: past {SEASON_26_27_GW1} — the bootstrap no longer reflects "
                  f"{SEASON}, so points_per_game will be left as-is rather than "
                  "overwritten with this season's value.\n")
        elements = _get(f"{BASE}/bootstrap-static/")["elements"]
        print(f"pool: {len(elements)} elements; fetching element-summary for each "
              f"(~{len(elements) * (args.pause + 0.35):.0f}s)")
        stats, missing, failed = fetch_stats(elements, args.pause, capture_ppg)
        print(f"\nfetched {SEASON} rows for {len(stats)} players "
              f"({len(missing)} had no {SEASON} season, {len(failed)} requests failed)")
        if failed:
            for name, err in failed[:5]:
                print(f"    FAILED {name}: {err}")

        targets = db.execute(text(
            "select ps.id, ps.name, p.code, ps.total_points is not null as done "
            "from player_season ps join players p on p.id = ps.player_id "
            "where ps.league_id = :l and p.code is not null"
        ), {"l": league_id}).mappings().all()

        todo, already, unmatched = [], 0, []
        for t in targets:
            if t["done"]:
                already += 1
                continue
            vals = stats.get(t["code"])
            if vals is None:
                unmatched.append(t["name"])
                continue
            todo.append((t["id"], t["name"], vals))

        total_rows = db.execute(text(
            "select count(*) from player_season where league_id=:l"), {"l": league_id}
        ).scalar_one()
        no_code = total_rows - len(targets)

        print(f"\nplayer_season rows           : {total_rows}")
        print(f"  with a code (linkable)     : {len(targets)}")
        print(f"  already populated (skipped): {already}")
        print(f"  to write                   : {len(todo)}")
        print(f"  linkable but no {SEASON} row: {len(unmatched)}")
        print(f"  no code — left blank       : {no_code}  (identity never matched; "
              "~44 left the PL, the rest the code backfill declined to guess)")

        if todo:
            top = sorted(todo, key=lambda r: -(r[2]["total_points"] or 0))[:10]
            print("\ntop 10 by 25/26 points (sanity — should look like a real season):")
            for _id, name, v in top:
                print(f"  {name:<18} {str(v['total_points']):>4} pts  "
                      f"{str(v['minutes']):>5} min  {str(v['goals_scored']):>3} G  "
                      f"{str(v['assists']):>3} A  ppg={v.get('points_per_game')}")

        # ---- sanity gates ----
        if not todo and not already:
            sys.exit("\nABORT: nothing matched — the join or the season filter is wrong.")
        if todo:
            best = max((v["total_points"] or 0) for _i, _n, v in todo)
            if best < MIN_PLAUSIBLE_TOP_SCORE:
                sys.exit(f"\nABORT: top score {best} is implausible for a full season. "
                         "This is what a GW1 reset looks like — refusing to write.")

        if not args.apply:
            print("\nDRY RUN — re-run with --apply to execute.")
            return

        cols = list(FROM_HISTORY.values()) + (["points_per_game"] if capture_ppg else [])
        sets = ", ".join(f"{c}=:{c}" for c in cols)
        for row_id, _name, vals in todo:
            db.execute(text(f"update player_season set {sets} where id=:id"),
                       {**vals, "id": row_id})
        db.commit()
        print(f"\nAPPLIED. {len(todo)} player_season rows given {SEASON} statistics.")
        verify(db, league_id)
    finally:
        db.close()


if __name__ == "__main__":
    main()
