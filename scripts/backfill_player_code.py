"""Backfill players.code — FPL's PERMANENT player id — by matching our rows
against the live bootstrap.

`players.fpl_id` is only a slot number that FPL recycles every season; `code` is
the stable identity that lets sync_players match a row to the same human forever.
Existing rows predate the column, so they need one matching pass.

    python scripts/backfill_player_code.py                                  # dry run
    python scripts/backfill_player_code.py --apply
    python scripts/backfill_player_code.py --overrides scripts/player_code_overrides.json --apply

Re-runnable: only ever writes where code IS NULL, so running again later (as the
feed grows from ~577 toward ~841 during the season) just fills in more.

Matching is deliberately CONSERVATIVE. A false negative leaves code NULL (sync
creates a duplicate row later, recoverable). A false positive writes another
human's permanent id onto a row that 12 FK columns point at — unrecoverable, and
exactly the hijack this whole change exists to prevent. So: never guess.
"""

# truststore must patch ssl before any HTTP client is constructed — this machine
# sits behind a TLS-inspecting corporate proxy (dev-only; Render has no proxy).
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover
    print("WARNING: truststore missing; the FPL fetch will fail behind the corporate "
          "proxy with CERTIFICATE_VERIFY_FAILED. Run: uv pip install truststore")

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import urllib.request  # noqa: E402
from collections import defaultdict  # noqa: E402

from sqlalchemy import text  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal  # noqa: E402

BOOTSTRAP = "https://fantasy.premierleague.com/api/bootstrap-static/"
OVERRIDES = "scripts/player_code_overrides.json"
POS_FALLBACK = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}  # sync._POSITION_FALLBACK


def fetch_feed():
    """Feed elements carry INT codes for position/team, not the strings we store,
    so build the same lookups sync_players builds."""
    with urllib.request.urlopen(BOOTSTRAP, timeout=30) as r:
        feed = json.load(r)
    pos = {
        t["id"]: t.get("singular_name_short") or POS_FALLBACK.get(t["id"])
        for t in feed.get("element_types", [])
    }
    team = {
        t["id"]: t.get("short_name") or t.get("name") for t in feed.get("teams", [])
    }
    return [
        {
            "code": e["code"],
            "web_name": e["web_name"],
            "position": pos.get(e["element_type"]),
            "team": team.get(e["team"]),
        }
        for e in feed.get("elements", [])
    ]


def load_assets(db):
    """players.id -> which league assets reference it (why a miss matters)."""
    assets = defaultdict(list)
    for pid, n in db.execute(text(
        "select player_id, count(*) from rosters group by 1"
    )):
        assets[str(pid)].append(f"rostered({n} gws)")
    for tbl in ("keeper_seeds", "keeper_selections"):
        for (pid,) in db.execute(text(f"select distinct player_id from {tbl}")):
            assets[str(pid)].append(tbl)
    return assets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument("--overrides", default=OVERRIDES)
    args = ap.parse_args()

    elements = fetch_feed()
    print(f"feed: {len(elements)} elements\n")

    overrides, reviewed_null = {}, set()
    if os.path.exists(args.overrides):
        with open(args.overrides) as f:
            raw = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        # A null value means "reviewed, deliberately left without a code" — the row
        # is a different human or genuinely gone. It clears the hard stop without
        # weakening it for rows nobody has looked at.
        overrides = {k: v for k, v in raw.items() if v is not None}
        reviewed_null = {k for k, v in raw.items() if v is None}
    print(f"overrides loaded: {len(overrides)} assigned, {len(reviewed_null)} reviewed-as-null")

    by_key = defaultdict(list)          # (web_name, position) -> [element]
    by_name = defaultdict(list)         # web_name -> [element]
    for e in elements:
        by_key[(e["web_name"], e["position"])].append(e)
        by_name[e["web_name"]].append(e)

    db = SessionLocal()
    try:
        rows = db.execute(text(
            "select id, fpl_id, code, name, position, current_team from players"
        )).mappings().all()
        assets = load_assets(db)
        todo = [r for r in rows if r["code"] is None]
        print(f"players: {len(rows)} total, {len(todo)} without a code\n")

        proposals, ambiguous, unmatched = {}, [], []
        for r in todo:
            pid = str(r["id"])
            if pid in overrides or pid in reviewed_null:
                continue                       # overrides applied first, below
            cands = by_key.get((r["name"], r["position"]), [])
            if len(cands) == 1:
                proposals[pid] = cands[0]["code"]
            elif len(cands) > 1:
                exact = [c for c in cands if c["team"] == r["current_team"]]
                if len(exact) == 1:
                    proposals[pid] = exact[0]["code"]
                else:
                    ambiguous.append((r, cands))
            else:
                unmatched.append(r)

        # --- overrides win, and knock out any auto-proposal for the same code ---
        for pid, code in overrides.items():
            proposals[pid] = code
        override_codes = set(overrides.values())
        for pid in [p for p, c in proposals.items()
                    if c in override_codes and p not in overrides]:
            del proposals[pid]

        # --- duplicate code claims -----------------------------------------
        rows_by_id = {str(r["id"]): r for r in rows}
        feed_by_code = {e["code"]: e for e in elements}
        claimed = defaultdict(list)
        for pid, code in proposals.items():
            claimed[code].append(pid)

        dropped = []
        for code, pids in claimed.items():
            if len(pids) == 1:
                continue
            fe = feed_by_code.get(code, {})
            keep = [p for p in pids
                    if rows_by_id[p]["current_team"] == fe.get("team")]
            if len(keep) == 1:
                for p in pids:
                    if p != keep[0]:
                        del proposals[p]
                        dropped.append((code, p, "club tiebreak"))
            else:
                for p in pids:
                    del proposals[p]
                    dropped.append((code, p, "unresolved"))

        # ---------------- output ----------------
        def label(pid):
            r = rows_by_id[pid]
            a = ", ".join(assets.get(pid, [])) or "-"
            return (f"{r['name']:<18} {str(r['position']):<4} {str(r['current_team']):<4} "
                    f"assets=[{a}]")

        print(f"unique matches: {len(proposals)}   ambiguous: {len(ambiguous)}   "
              f"dropped (dup code): {len(dropped)}   no match: {len(unmatched)}\n")

        blockers = []

        # Split league assets that missed into "still in the feed under a different
        # position" (a human must decide — FPL reclassifies element_type between
        # seasons) and "not in the feed at all" (departed the PL; code NULL is
        # correct and harmless, they will never sync again).
        needs_call = [r for r in unmatched
                      if str(r["id"]) in assets and by_name.get(r["name"])
                      and str(r["id"]) not in overrides]
        departed = [r for r in unmatched
                    if str(r["id"]) in assets and not by_name.get(r["name"])]

        print("=" * 72)
        print("NEEDS REVIEW — league asset still in the feed, but not under our position")
        print("=" * 72)
        if not needs_call:
            print("  (none)")
        for r in needs_call:
            pid = str(r["id"])
            if pid not in reviewed_null:
                blockers.append(pid)
            print(f"  {pid}  {label(pid)}")
            for c in by_name[r["name"]]:
                same = ("team MATCHES current_team  <-- likely same human"
                        if c["team"] == r["current_team"] else "different club — could be someone else")
                print(f"      code {c['code']:<7} {c['web_name']:<18} "
                      f"{c['position']}/{c['team']}  ({same})")

        print(f"\nleague assets absent from the feed entirely "
              f"(departed the PL — code stays NULL, harmless): {len(departed)}")
        print("  " + ", ".join(sorted(r["name"] for r in departed)))

        print()
        print("=" * 72)
        print("DUPLICATE CODE CLAIM — dropped")
        print("=" * 72)
        if not dropped:
            print("  (none)")
        for code, pid, why in dropped:
            fe = feed_by_code.get(code, {})
            flag = " <-- LEAGUE ASSET" if pid in assets else ""
            if pid in assets and why == "unresolved" and pid not in reviewed_null:
                blockers.append(pid)
            print(f"  code={code} feed={fe.get('web_name')}/{fe.get('position')}/{fe.get('team')}"
                  f" [{why}]{flag}\n      {pid}  {label(pid)}")

        if ambiguous:
            print("\nAMBIGUOUS — needs an override")
            for r, cands in ambiguous:
                pid = str(r["id"])
                if pid in assets and pid not in reviewed_null:
                    blockers.append(pid)
                print(f"  {pid}  {label(pid)}  ({len(cands)} candidates)")

        no_asset = len([r for r in unmatched if str(r["id"]) not in assets])
        print(f"\nno match and not a league asset (harmless, code stays NULL): {no_asset}")

        # ---------------- gates ----------------
        if not proposals:
            sys.exit("\nABORT: zero matches — the match key is wrong. Do not apply.")
        if len(unmatched) > 0.75 * len(todo):
            sys.exit(f"\nABORT: {len(unmatched)}/{len(todo)} unmatched — the match key "
                     "looks wrong. Do not interpret this as players leaving the PL.")
        if blockers:
            print(f"\n{'=' * 72}\nHARD STOP: {len(set(blockers))} league asset(s) "
                  f"(rostered / keeper) would be left with code IS NULL.\nAdd them to "
                  f"{args.overrides} as {{\"<players.id>\": <code>}} and re-run.\n{'=' * 72}")
            sys.exit(1)

        if not args.apply:
            print("\nDRY RUN — re-run with --apply to execute.")
            return

        n = 0
        for pid, code in proposals.items():
            n += db.execute(
                text("update players set code=:c where id=:i and code is null"),
                {"c": code, "i": pid},
            ).rowcount
        db.commit()
        print(f"\nAPPLIED. {n} players given a code.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
