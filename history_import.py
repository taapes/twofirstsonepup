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
from models import KeeperSeed, League, Manager, ManagerHonors, Player, SeasonHistory
from settings import LEAGUE_ID

HISTORY_DIR = "history"
CURRENT_TEAMS = f"{HISTORY_DIR}/The Greatest FPL Draft League in the World - Current Teams.csv"
LEAGUE_HISTORY = f"{HISTORY_DIR}/The Greatest FPL Draft League in the World - League History.csv"

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


if __name__ == "__main__":
    import sys
    commit = "--commit" in sys.argv
    target = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "keepers"
    if target == "history":
        import_league_history(commit=commit)
    else:
        import_keeper_seeds(commit=commit)
