"""Import an outside analyst's season point projections from an xlsx.

A draft is prepared in the offseason, when `players` holds nothing but zeros — see
services.stats_season — so the only numbers worth ranking on are expected ones. This
loads a projection sheet into `player_projection`, matched to our STABLE player UUIDs.

    python scripts/import_projections.py                                   # dry run
    python scripts/import_projections.py --apply
    python scripts/import_projections.py --file <path> --season 2026 --apply
    python scripts/import_projections.py --aliases scripts/projection_aliases.json --apply
    python scripts/import_projections.py --prune --apply    # also drop rows the sheet no longer has

Re-runnable: upserts on (season_year, player_id), so a revised sheet overwrites in
place rather than duplicating.

Matching is CONSERVATIVE, for the same reason as scripts/backfill_player_code.py: a
false negative leaves a player without a projection (cosmetic), a false positive
attributes another human's numbers to a player the commissioner then drafts on.
Ambiguous and unmatched rows abort the run rather than being guessed.

Parsed with stdlib zipfile + ElementTree — an xlsx is a zip of XML, and a once-a-year
import does not justify adding openpyxl to a deployed requirements.txt.
"""

import argparse
import json
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_FILE = "docs/Analysis/26_27_predictions.xlsx"
DEFAULT_SEASON = 2026  # the 26/27 season
ALIASES = "scripts/projection_aliases.json"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# The header is checked EXACTLY against this. It is what makes reading cells by
# column letter trustworthy: if the sheet is ever re-exported with a column added,
# moved or renamed, the run aborts instead of writing DC values into `bonus`.
HEADERS = ["Name", "Team", "Pos", "FPL Price", "Mins", "G", "A", "CS",
           "Bonus", "DC", "YC", "FPL Pts", "Value"]

# Column letter -> field. "M" (Value) is deliberately absent: points-per-million is
# derived on read from points + price, so storing it would let it drift.
TEXT_COLUMNS = {"A": "raw_name", "B": "raw_team", "C": "raw_position"}
NUM_COLUMNS = {"D": "price", "E": "minutes", "F": "goals_scored", "G": "assists",
               "H": "clean_sheets", "I": "bonus", "J": "defensive_contributions",
               "K": "yellow_cards", "L": "points"}

# Kept as dicts, not inline conditionals, so the report can print how many rows each
# one fired on. A fixup that matches ZERO rows is the loudest available signal that
# the sheet's code set has changed under us.
TEAM_FIXES = {"BRI": "BHA"}  # the sheet writes Brighton BRI; FPL's short_name is BHA
POS_FIXES = {"GK": "GKP"}    # FPL's element_types.singular_name_short

MIN_MATCH_RATE = 0.95  # below this the match KEY is wrong, not the squad
PRICE_RANGE = (3.5, 20.0)  # second, independent catch for a shifted column
MAX_PRUNE_RATE = 0.10

# NFKD has no decomposition for these, so an ascii-ignore pass DELETES them outright
# and 'Ødegaard' becomes 'degaard'. Mapping them first is what takes this import from
# 562/566 to 566/566 (Ødegaard, Nørgaard, F.Kadıoğlu, Hjertø-Dahl).
_TRANSLIT = str.maketrans({"ø": "o", "đ": "d", "ı": "i", "ł": "l",
                           "æ": "ae", "ß": "ss", "þ": "th"})


def _norm(s: str) -> str:
    """A name -> its match key. The order is load-bearing: lower() FIRST (the
    translation table has lowercase keys only), translate SECOND, NFKD THIRD.

    Deliberately NOT the same function as history_import._norm, which is plain NFKD.
    Do not unify them: that one is load-bearing for keeper seeds that are already
    imported, and changing it would silently re-resolve them.
    """
    s = (s or "").lower().translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s)


# ---- xlsx (stdlib) --------------------------------------------------------
def read_sheet(path: str) -> list[dict]:
    """Rows of {column letter: text}. Keyed by LETTER, never by position: Excel omits
    empty cells entirely, so one blank CS in a re-export would shift a positional
    parse by a column and write DC into bonus — silent, plausible-looking corruption
    with no error anywhere."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            # Join the <t> runs: find() returns only the FIRST, so one bolded surname
            # in a re-export would truncate a name to a prefix that then matches
            # nothing and reads like a data problem rather than a parser bug.
            shared = ["".join(t.text or "" for t in si.iter(f"{NS}t"))
                      for si in root.findall(f"{NS}si")]
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    rows = []
    data = sheet.find(f"{NS}sheetData")
    for r in (data.findall(f"{NS}row") if data is not None else []):
        cells = {}
        for c in r.findall(f"{NS}c"):
            col = re.match(r"[A-Z]+", c.get("r") or "")
            if not col:
                continue
            if c.get("t") == "inlineStr":
                cells[col.group(0)] = "".join(t.text or "" for t in c.iter(f"{NS}t"))
                continue
            v = c.find(f"{NS}v")
            if v is None:
                continue
            cells[col.group(0)] = (shared[int(v.text)] if c.get("t") == "s" else v.text)
        rows.append(cells)
    return rows


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_rows(cells: list[dict]) -> tuple[list[dict], dict]:
    """Typed rows + a count of how many times each fixup fired. Raises on a header
    that isn't exactly HEADERS."""
    if not cells:
        raise ValueError("sheet is empty")
    header = [(cells[0].get(chr(ord("A") + i)) or "").strip()
              for i in range(len(HEADERS))]
    if header != HEADERS:
        raise ValueError(
            "unexpected header row — the column layout changed, so reading by column "
            f"letter is no longer safe.\n  expected: {HEADERS}\n  found:    {header}"
        )

    fixups = {"team": 0, "position": 0}
    out = []
    for raw in cells[1:]:
        if not (raw.get("A") or "").strip():
            continue  # blank/footer row
        row = {f: (raw.get(col) or "").strip() for col, f in TEXT_COLUMNS.items()}
        row.update({f: _num(raw.get(col)) for col, f in NUM_COLUMNS.items()})
        team = TEAM_FIXES.get(row["raw_team"], row["raw_team"])
        pos = POS_FIXES.get(row["raw_position"], row["raw_position"])
        fixups["team"] += team != row["raw_team"]
        fixups["position"] += pos != row["raw_position"]
        row["match_team"], row["match_position"] = team, pos
        out.append(row)
    return out, fixups


# ---- matching -------------------------------------------------------------
def build_index(pool) -> dict:
    """(normalized name, position, team) -> [player]. The compound key is not a
    nicety: the live pool has 15 duplicate `name` values and zero duplicate triples,
    so matching on name alone would attribute one player's projection to another."""
    idx = defaultdict(list)
    for p in pool:
        idx[(_norm(p["name"]), p["position"], p["current_team"])].append(p)
    return idx


def resolve(rows, idx, aliases=None):
    """-> (matched, unmatched, ambiguous). Never guesses."""
    aliases = aliases or {}
    by_norm_name = defaultdict(list)
    for (n, _pos, _team), ps in idx.items():
        by_norm_name[n].extend(ps)

    matched, unmatched, ambiguous = [], [], []
    for row in rows:
        alias = aliases.get(row["raw_name"])
        if alias:
            row = {**row, "player_id": alias}
            matched.append(row)
            continue
        cands = idx.get((_norm(row["raw_name"]), row["match_position"], row["match_team"]))
        if not cands:
            # near misses (same name at any club/position) so the alias writes itself
            row = {**row, "near": by_norm_name.get(_norm(row["raw_name"]), [])}
            unmatched.append(row)
        elif len(cands) > 1:
            ambiguous.append({**row, "candidates": cands})
        else:
            matched.append({**row, "player_id": str(cands[0]["id"])})
    return matched, unmatched, ambiguous


def upsert_projections(db, season_year: int, matched: list[dict]) -> dict:
    """Insert or update in place, keyed on (season_year, player_id). Returns counts.
    Inserts go through the ORM so players.id's Python-side uuid4 default applies."""
    from models import PlayerProjection

    fields = list(TEXT_COLUMNS.values()) + list(NUM_COLUMNS.values())
    existing = {
        str(r.player_id): r
        for r in db.query(PlayerProjection).filter_by(season_year=season_year)
    }
    counts = {"insert": 0, "update": 0, "identical": 0}
    for row in matched:
        cur = existing.get(row["player_id"])
        if cur is None:
            db.add(PlayerProjection(
                season_year=season_year, player_id=row["player_id"],
                **{f: row[f] for f in fields},
            ))
            counts["insert"] += 1
            continue
        changed = [f for f in fields if getattr(cur, f) != row[f]]
        if changed:
            for f in changed:
                setattr(cur, f, row[f])
            counts["update"] += 1
        else:
            counts["identical"] += 1
    counts["stale"] = len(set(existing) - {r["player_id"] for r in matched})
    return counts


def load_assets(db):
    """players.id -> which league assets reference it. Same idea as
    backfill_player_code.load_assets: it's how the report says which of the pool
    players WITHOUT a projection actually matter."""
    assets = defaultdict(list)
    for pid, n in db.execute(text("select player_id, count(*) from rosters group by 1")):
        assets[str(pid)].append(f"rostered({n} gws)")
    for tbl in ("keeper_seeds", "keeper_selections"):
        for (pid,) in db.execute(text(f"select distinct player_id from {tbl}")):
            assets[str(pid)].append(tbl)
    return assets


def _banner(title):
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--season", type=int, default=DEFAULT_SEASON)
    ap.add_argument("--aliases", default=ALIASES)
    ap.add_argument("--prune", action="store_true",
                    help="delete stored rows the sheet no longer contains")
    ap.add_argument("--allow-unmatched", action="store_true")
    ap.add_argument("--allow-ambiguous", action="store_true")
    args = ap.parse_args()

    # Imported here, not at module scope: db.py raises without DATABASE_URL, and the
    # parsing/matching functions above must stay importable by tests with no env.
    from db import SessionLocal

    rows, fixups = parse_rows(read_sheet(args.file))
    print(f"file={args.file}  season={args.season} ({args.season % 100:02d}/"
          f"{(args.season + 1) % 100:02d})")
    print(f"sheet: headers OK, {len(rows)} data rows")
    print("fixups: " + "   ".join(f"{k} {v} rows" for k, v in fixups.items()))
    if not rows:
        sys.exit("ABORT: no data rows")

    aliases = {}
    if os.path.exists(args.aliases):
        with open(args.aliases) as f:
            aliases = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    if aliases:
        print(f"aliases loaded: {len(aliases)}")

    db = SessionLocal()
    try:
        # fpl_id IS NOT NULL restricts to the LIVE pool. Of ~964 player rows the rest
        # are historical and carry stale clubs; including them manufactures false
        # positives against a player's FORMER club.
        pool = [dict(r) for r in db.execute(text(
            "select id, name, position, current_team, price from players "
            "where fpl_id is not null"
        )).mappings()]
        total = db.execute(text("select count(*) from players")).scalar()
        print(f"pool: {len(pool)} players with fpl_id ({total} total)\n")

        idx = build_index(pool)
        matched, unmatched, ambiguous = resolve(rows, idx, aliases)
        print(f"matched {len(matched)}   ambiguous {len(ambiguous)}   "
              f"unmatched {len(unmatched)}")

        if unmatched:
            _banner("UNMATCHED — no live-pool player on (name, position, team)")
            for r in unmatched:
                near = ", ".join(f"{p['name']}/{p['position']}/{p['current_team']}"
                                 for p in r["near"]) or "no near miss"
                print(f"  {r['raw_name']:<20} {r['raw_position']:<4} {r['raw_team']:<4}"
                      f"  near: {near}")
        if ambiguous:
            _banner("AMBIGUOUS — more than one candidate; never guessed")
            for r in ambiguous:
                print(f"  {r['raw_name']:<20} -> " + ", ".join(
                    str(p["id"]) for p in r["candidates"]))

        # ---- sanity gates: everything below aborts BEFORE any write ----
        if not matched:
            sys.exit("ABORT: nothing matched — the match key is wrong. Do NOT read "
                     "this as squad turnover.")
        rate = len(matched) / len(rows)
        if rate < MIN_MATCH_RATE:
            sys.exit(f"ABORT: match rate {rate:.1%} < {MIN_MATCH_RATE:.0%} — that is "
                     "systematic (team or position codes changed), not a few signings.")
        if ambiguous and not args.allow_ambiguous:
            sys.exit("ABORT: ambiguous rows (--allow-ambiguous to skip them).")
        if unmatched and not args.allow_unmatched:
            sys.exit("ABORT: unmatched rows — add them to "
                     f"{args.aliases} (raw name -> players.id) or --allow-unmatched.")
        bad_pts = [r for r in matched if r["points"] is None or r["points"] < 0]
        if bad_pts:
            sys.exit(f"ABORT: {len(bad_pts)} rows with missing/negative points, e.g. "
                     f"{bad_pts[0]['raw_name']} — a footer row or a shifted column.")
        bad_price = [r for r in matched if r["price"] is not None
                     and not (PRICE_RANGE[0] <= r["price"] <= PRICE_RANGE[1])]
        if bad_price:
            sys.exit(f"ABORT: {len(bad_price)} rows priced outside {PRICE_RANGE}, e.g. "
                     f"{bad_price[0]['raw_name']} {bad_price[0]['price']} — the price "
                     "column is probably not the price column.")
        dupes = [pid for pid, n in Counter(m["player_id"] for m in matched).items()
                 if n > 1]
        if dupes:
            sys.exit(f"ABORT: {len(dupes)} players matched by more than one sheet row; "
                     "that would violate the unique constraint mid-transaction.")

        # ---- report ----
        by_id = {str(p["id"]): p for p in pool}
        assets = load_assets(db)
        missing = sorted(set(by_id) - {m["player_id"] for m in matched},
                         key=lambda i: by_id[i]["name"])
        if missing:
            _banner(f"POOL PLAYERS WITH NO PROJECTION ({len(missing)})")
            for pid in missing:
                p = by_id[pid]
                a = ", ".join(assets.get(pid, [])) or "-"
                flag = "   <-- LEAGUE ASSET" if assets.get(pid) else ""
                print(f"  {p['name']:<18} {p['position'] or '?':<4} "
                      f"{p['current_team'] or '?':<4} assets=[{a}]{flag}")

        pdiff = [(m["raw_name"], m["price"], by_id[m["player_id"]]["price"] / 10)
                 for m in matched
                 if m["player_id"] in by_id and m["price"] is not None
                 and by_id[m["player_id"]]["price"] is not None
                 and abs(m["price"] - by_id[m["player_id"]]["price"] / 10) > 0.001]
        if pdiff:
            _banner(f"PRICE DISAGREEMENTS — sheet vs live players.price ({len(pdiff)})")
            for name, sheet_p, live_p in pdiff:
                print(f"  {name:<20} sheet {sheet_p:<6} live {live_p}")

        pts = sorted((m["points"] for m in matched), reverse=True)
        print(f"\ndistribution: {sum(1 for p in pts if p == 0)} rows project 0.0 pts; "
              f"max {pts[0]}; median {pts[len(pts) // 2]}")

        counts = upsert_projections(db, args.season, matched)
        print(f"writes: {counts['insert']} insert, {counts['update']} update, "
              f"{counts['identical']} identical, {counts['stale']} stale"
              f"{' (would prune)' if args.prune else ' (left alone; --prune to delete)'}")

        if args.prune and counts["stale"]:
            from models import PlayerProjection
            keep = {m["player_id"] for m in matched}
            stale = [r for r in db.query(PlayerProjection).filter_by(
                season_year=args.season) if str(r.player_id) not in keep]
            existing_n = counts["insert"] + counts["update"] + counts["identical"] \
                + counts["stale"]
            if len(stale) / max(existing_n, 1) > MAX_PRUNE_RATE:
                sys.exit(f"ABORT: --prune would delete {len(stale)} rows "
                         f"(> {MAX_PRUNE_RATE:.0%}) — that looks like a truncated "
                         "export, not a revision.")
            for r in stale:
                db.delete(r)

        if not args.apply:
            db.rollback()
            print("\nDRY RUN — re-run with --apply to execute.")
            return
        db.commit()
        print("\napplied.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
