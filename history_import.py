"""Import historical data from the Google-Sheet CSV exports in history/.

Reproducible + idempotent. Managers map by their populated display_name (person);
players map by an alias table (for sheet typos/abbreviations) then normalized
name match. Run a function with commit=False (default) to preview + report
unresolved before writing.
"""

import csv
import re
import unicodedata

from db import SessionLocal
from models import (
    FuturePick,
    HistoricalStanding,
    KeeperSeed,
    League,
    Manager,
    ManagerHonors,
    Player,
    SeasonHistory,
)
from settings import LEAGUE_ID

HISTORY_DIR = "history"
CURRENT_TEAMS = f"{HISTORY_DIR}/The Greatest FPL Draft League in the World - Current Teams.csv"
LEAGUE_HISTORY = f"{HISTORY_DIR}/The Greatest FPL Draft League in the World - League History.csv"
STANDINGS = f"{HISTORY_DIR}/The Greatest FPL Draft League in the World - League History - Standings.csv"
FUTURE_PICKS = f"{HISTORY_DIR}/The Greatest FPL Draft League in the World - Future Picks.csv"
DISCOVERY = f"{HISTORY_DIR}/The Greatest FPL Draft League in the World - Discovery Draft.csv"
CUP = f"{HISTORY_DIR}/The Greatest FPL Draft League in the World - The Cup.csv"

# Person-name abbreviations used in the discovery grid.
_PERSON_ALIAS = {"KF": "Kevin F", "KT": "Kevin T", "KS": "Kevin S"}


def _person(name: str) -> str:
    name = (name or "").strip()
    return _PERSON_ALIAS.get(name, name)

# Sheet player name -> our players.name (web_name). For accents/typos/abbrev that
# normalized matching can't bridge. Confirmed against the DB + the commissioner.
PLAYER_ALIAS = {
    "Salah": "M.Salah", "Alisson": "A.Becker", "Sa": "José Sá",
    "Bruno": "B.Fernandes", "Porro": "Pedro Porro", "Gyokores": "Gyökeres",
    "Diatike": "Diakité", "Ruben Dias": "Rúben", "Jimenez": "Raúl",
    "Guiu": "Marc Guiu", "Timber": "J.Timber", "Bizot": "M.Bizot",
    "Jackson": "N.Jackson", "Hojlund": "Højlund", "Ederson": "Ederson M.",
    "MGW": "Gibbs-White", "Savio": "Savinho", "Sanesi": "Senesi",
    "Verbuggen": "Verbruggen", "Matheus": "Matheus N.", "Odegaard": "Ødegaard",
    "Rodri": "Rodrigo", "Guessend": "Guessand", "Brantwaithe": "Branthwaite",
    "Pedro": "João Pedro",
}
# Ambiguous names with duplicate web_names -> pin by fpl_id.
PLAYER_ALIAS_FPL = {"Pedro Neto": 236}  # Chelsea winger (not the BOU keeper)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def parse_current_teams() -> dict:
    """Returns {person: [(position, player, years:int)]} from the Current Teams CSV."""
    rows = [r + [""] * 12 for r in csv.reader(open(CURRENT_TEAMS))]
    teams: dict = {}
    cur: dict = {}
    for r in rows:
        for c in (0, 4, 8):
            if r[c + 1].strip() == "Player":  # header row starts a manager block
                cur[c] = r[c].strip()
                teams.setdefault(cur[c], [])
            elif cur.get(c) and r[c].strip() and r[c + 1].strip():
                yrs = r[c + 2].strip()
                teams[cur[c]].append((r[c].strip(), r[c + 1].strip(), int(yrs) if yrs.isdigit() else 0))
    return {k: v for k, v in teams.items() if v}


def import_keeper_seeds(commit: bool = False) -> None:
    """Current Teams -> KeeperSeed.prior_years (= keeper years entering 25/26).
    Only players with years>=1 are seeded (years=0 = drafted fresh, no seed)."""
    db = SessionLocal()
    league = db.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one()
    mgr_by_person = {m.display_name: m for m in db.query(Manager).filter_by(league_id=league.id) if m.display_name}
    players = db.query(Player).all()
    by_norm = {}
    for p in players:
        by_norm.setdefault(_norm(p.name), p)
    by_fpl = {p.fpl_id: p for p in players}

    def resolve_player(name: str):
        if name in PLAYER_ALIAS_FPL:
            return by_fpl.get(PLAYER_ALIAS_FPL[name])
        target = PLAYER_ALIAS.get(name, name)
        return by_norm.get(_norm(target))

    teams = parse_current_teams()
    seeded, drafted, unresolved, no_manager = 0, 0, [], []
    for person, squad in teams.items():
        mgr = mgr_by_person.get(person)
        if not mgr:
            no_manager.append(person)
            continue
        for pos, pname, years in squad:
            player = resolve_player(pname)
            if not player:
                unresolved.append(f"{person}:{pname}")
                continue
            if years < 1:
                drafted += 1
                continue
            existing = db.query(KeeperSeed).filter_by(manager_id=mgr.id, player_id=player.id).one_or_none()
            if existing:
                existing.prior_years = years
            else:
                db.add(KeeperSeed(league_id=league.id, manager_id=mgr.id,
                                  player_id=player.id, prior_years=years, season_year=2025))
            seeded += 1

    print(f"seeded (years>=1): {seeded}")
    print(f"drafted this year (years=0, no seed): {drafted}")
    print(f"unmatched managers: {no_manager or 'none'}")
    print(f"unresolved players ({len(unresolved)}): {unresolved or 'none'}")
    if commit:
        db.commit()
        print("COMMITTED")
    else:
        db.rollback()
        print("(preview only — rolled back; pass commit=True to write)")
    db.close()


def import_league_history(commit: bool = False) -> None:
    """League History CSV -> season_history (year + winners) and manager_honors
    (career title/cup tally). Replaces existing rows for this league."""
    db = SessionLocal()
    league = db.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one()
    rows = [r + [""] * 8 for r in csv.reader(open(LEAGUE_HISTORY))][1:]  # skip header

    seasons, honors = [], []
    for r in rows:
        year = r[0].strip()
        if re.match(r"\d\d/\d\d", year):  # left half: a season result
            seasons.append((year, r[1].strip() or None, r[2].strip() or None, r[3].strip() or None))
        person = r[5].strip()
        if person:  # right half: a career tally row
            titles = int(r[6]) if r[6].strip().isdigit() else 0
            cups = int(r[7]) if r[7].strip().isdigit() else 0
            honors.append((person, titles, cups))

    if commit:
        db.query(SeasonHistory).filter_by(league_id=league.id).delete()
        db.query(ManagerHonors).filter_by(league_id=league.id).delete()
        for year, lw, cw, pw in seasons:
            db.add(SeasonHistory(league_id=league.id, year=year,
                                 league_winner=lw, cup_winner=cw, pup_winner=pw))
        for person, titles, cups in honors:
            db.add(ManagerHonors(league_id=league.id, manager_name=person,
                                 titles=titles, cups=cups))
        db.commit()
        print(f"COMMITTED: {len(seasons)} seasons, {len(honors)} honor tallies")
    else:
        print(f"seasons ({len(seasons)}):")
        for s in seasons:
            print("   ", s)
        print(f"honors ({len(honors)}):")
        for h in honors:
            print("   ", h)
        print("(preview only — pass commit=True to write)")
    db.close()


def _int(s):
    s = (s or "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


def import_standings_history(commit: bool = False) -> None:
    """Standings CSV -> historical_standings (per season, per rank). Tolerant of
    missing teams/stats (older seasons have manager-only rows)."""
    db = SessionLocal()
    league = db.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one()
    rows = [r + [""] * 8 for r in csv.reader(open(STANDINGS))]

    entries = []  # (year, rank, team, manager, w, d, l, pf, h2h)
    year = None
    for r in rows:
        if re.match(r"\d\d/\d\d", r[0]) and "Manager" in r[1]:
            year = r[0].strip()
            continue
        if year and r[0].strip().isdigit():
            cell = r[1].strip()
            if "\n" in cell:
                team, manager = cell.split("\n", 1)
                team, manager = team.strip(), manager.strip()
            else:
                team, manager = (None, cell or None)
            entries.append((year, int(r[0]), team, manager,
                            _int(r[2]), _int(r[3]), _int(r[4]), _int(r[5]), _int(r[6])))

    if commit:
        db.query(HistoricalStanding).filter_by(league_id=league.id).delete()
        for year, rank, team, mgr, w, d, l, pf, h2h in entries:
            db.add(HistoricalStanding(league_id=league.id, year=year, rank=rank,
                                      team_name=team, manager_name=mgr, wins=w, draws=d,
                                      losses=l, points_for=pf, h2h_points=h2h))
        db.commit()
        print(f"COMMITTED: {len(entries)} standings rows across "
              f"{len(set(e[0] for e in entries))} seasons")
    else:
        for e in entries:
            print("   ", e)
        print(f"({len(entries)} rows; preview only — pass commit=True to write)")
    db.close()


def import_future_picks(commit: bool = False) -> None:
    """Future Picks CSV -> future_picks. LEFT grid only (cols 1-10): row=round,
    column header=original owner, cell=current owner ('Own' = kept, skipped).
    The right grid and free-text note rows are ignored."""
    db = SessionLocal()
    league = db.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one()
    rows = [r + [""] * 22 for r in csv.reader(open(FUTURE_PICKS))]

    picks = []  # (year, round, original_owner, owner)
    headers = None  # cols 1-10 manager names for the current year block
    year = None
    for r in rows:
        if r[0].strip().isdigit() and len(r[0].strip()) == 4:  # year header (e.g. 2026)
            year = int(r[0].strip())
            headers = [r[c].strip() for c in range(1, 11)]
        elif year and r[0].strip().isdigit():  # round row
            rnd = int(r[0].strip())
            for i, c in enumerate(range(1, 11)):
                cell = r[c].strip()
                orig = headers[i] if headers else None
                if orig and cell and cell.lower() != "own":
                    picks.append((year, rnd, orig, cell))

    if commit:
        db.query(FuturePick).filter_by(league_id=league.id, draft_type="main").delete()
        for year, rnd, orig, owner in picks:
            db.add(FuturePick(league_id=league.id, season_year=year, draft_type="main",
                              round=rnd, original_owner=orig, owner=owner))
        db.commit()
        print(f"COMMITTED: {len(picks)} traded future picks across "
              f"{len(set(p[0] for p in picks))} seasons")
    else:
        for p in picks:
            print("   ", p)
        print(f"({len(picks)} traded picks; preview only — pass commit=True to write)")
    db.close()


def import_discovery_picks(commit: bool = False) -> None:
    """Discovery Draft CSV -> future_picks (draft_type='discovery'). Parses the
    ownership grid (rows: original-owner manager; columns: YEAR R1/R2; cell =
    current owner). Free-text trade descriptions and historical results are
    ignored here."""
    db = SessionLocal()
    league = db.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one()
    known = {m.display_name for m in db.query(Manager).filter_by(league_id=league.id) if m.display_name}
    rows = [r + [""] * 26 for r in csv.reader(open(DISCOVERY))]

    # locate the grid header (cells like "2026 R1") and its column -> (year, round)
    col_yr, mgr_col, header_idx = {}, None, None
    for i, r in enumerate(rows):
        yrcols = {c: v.strip() for c, v in enumerate(r) if re.match(r"\d{4} R\d", v.strip())}
        if len(yrcols) >= 6:
            header_idx = i
            for c, v in yrcols.items():
                y, rd = v.split(" R")
                col_yr[c] = (int(y), int(rd))
            mgr_col = min(col_yr) - 1  # manager column is just left of the year cols
            break

    picks = []  # (year, round, original_owner, owner)
    for r in rows[header_idx + 1:]:
        orig = _person(r[mgr_col])
        if orig not in known:
            continue
        for c, (y, rd) in col_yr.items():
            owner = _person(r[c])
            if owner and owner in known:
                picks.append((y, rd, orig, owner))

    if commit:
        db.query(FuturePick).filter_by(league_id=league.id, draft_type="discovery").delete()
        for y, rd, orig, owner in picks:
            db.add(FuturePick(league_id=league.id, season_year=y, draft_type="discovery",
                              round=rd, original_owner=orig, owner=owner))
        db.commit()
        print(f"COMMITTED: {len(picks)} discovery future picks")
    else:
        for p in picks:
            print("   ", p)
        print(f"({len(picks)} discovery picks; preview only)")
    db.close()


def _season_label(raw: str) -> str:
    m = re.match(r"(\d{4})/(\d{2,4})", raw.strip())
    return f"{m.group(1)[-2:]}/{m.group(2)[-2:]}" if m else raw.strip()


def import_discovery_results(commit: bool = False) -> None:
    """Discovery Draft CSV -> discovery_results (per-season actual picks). Player
    names kept as free text. Handles the variable Round-2 column offset."""
    db = SessionLocal()
    league = db.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one()
    from models import DiscoveryResult

    rows = [r + [""] * 12 for r in csv.reader(open(DISCOVERY))]
    results = []  # (season, round, pick_number, manager, player)
    season = None
    for r in rows:
        c0 = r[0].strip()
        if re.match(r"\d{4}/\d{2,4}", c0):
            season = _season_label(c0)
            continue
        if season and c0.isdigit():
            results.append((season, 1, int(c0), r[1].strip() or None, r[2].strip() or None))
            for j in range(3, len(r) - 2):  # find R2's pick-number cell (offset varies)
                if r[j].strip().isdigit():
                    results.append((season, 2, int(r[j]), r[j + 1].strip() or None, r[j + 2].strip() or None))
                    break

    if commit:
        db.query(DiscoveryResult).filter_by(league_id=league.id).delete()
        for season, rd, pn, mgr, pl in results:
            db.add(DiscoveryResult(league_id=league.id, season=season, round=rd,
                                   pick_number=pn, manager_name=mgr, player_name=pl))
        db.commit()
        print(f"COMMITTED: {len(results)} discovery picks across "
              f"{len(set(r[0] for r in results))} seasons")
    else:
        for r in results[:10]:
            print("   ", r)
        print(f"... ({len(results)} picks across {len(set(r[0] for r in results))} seasons; preview)")
    db.close()


def import_cups(commit: bool = False) -> None:
    """The Cup CSV -> cup_matches. Three round-column groups (R1 @col2, SF @col7,
    Final @col12), each: 'SEED - Manager', GW1, GW2, Total. Cup vs Pup split on
    the 'Pup Cup' marker. Tolerant of missing scores / partial years. Cup year N
    belongs to season (N-1)/N."""
    db = SessionLocal()
    league = db.query(League).filter_by(fpl_league_id=str(LEAGUE_ID)).one()
    from models import CupMatch

    rows = [r + [""] * 18 for r in csv.reader(open(CUP))]
    entries = []  # (season, bracket, round, slot, seed, manager, gw1, gw2, total)
    season, bracket, slot = None, "cup", {}
    for r in rows:
        msea = re.match(r"(\d{4}) Cup", r[0].strip())
        if msea:
            y = int(msea.group(1))
            season = f"{(y - 1) % 100:02d}/{y % 100:02d}"
            bracket, slot = "cup", {}
            continue
        if not season:
            continue
        if any("pup cup" in (c or "").strip().lower() for c in r):
            bracket = "pup"
        for rnd, lc in ((1, 1), (2, 6), (3, 11)):  # label cols (scores at lc+1..+3)
            mt = re.match(r"(\d+)\s*-\s*(.+)", r[lc].strip())
            if mt:
                k = (bracket, rnd)
                slot[k] = slot.get(k, 0) + 1
                entries.append((season, bracket, rnd, slot[k], int(mt.group(1)),
                                _person(mt.group(2)), _int(r[lc + 1]), _int(r[lc + 2]), _int(r[lc + 3])))

    if commit:
        db.query(CupMatch).filter_by(league_id=league.id).delete()
        for season, br, rnd, sl, seed, mgr, g1, g2, tot in entries:
            db.add(CupMatch(league_id=league.id, season=season, bracket=br, round=rnd,
                            slot=sl, seed=seed, manager_label=mgr, gw1=g1, gw2=g2, total=tot))
        db.commit()
        print(f"COMMITTED: {len(entries)} cup entries across "
              f"{len(set(e[0] for e in entries))} seasons")
    else:
        for e in entries[:14]:
            print("   ", e)
        print(f"... ({len(entries)} entries; preview)")
    db.close()


_IMPORTERS = {
    "keepers": import_keeper_seeds,
    "history": import_league_history,
    "standings": import_standings_history,
    "futurepicks": import_future_picks,
    "discovery": import_discovery_picks,
    "discoveryresults": import_discovery_results,
    "cups": import_cups,
}

if __name__ == "__main__":
    import sys
    commit = "--commit" in sys.argv
    target = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "keepers"
    _IMPORTERS[target](commit=commit)
