"""One-off repair: restore manager identity + keeper clocks lost at the 26/27 rollover.

`advance_season` carries display_name, password_hash and keeper seeds across a
rollover by matching `managers.fpl_manager_id` — FPL's entry_id, which this codebase
treats as the stable cross-season identity. At the 26/27 rollover it wasn't: FPL
issued all ten managers brand-new entries (25/26: 5520-268927; 26/27: a contiguous
58528-58537 block), overlap ZERO. Every carry therefore matched nothing and
`continue`d, silently, leaving the new row with:

    display_name    NULL x10
    password_hash   NULL x10   -> every manager's login broken
    keeper_seeds    0 rows     -> every keeper clock lost (152 on the old row)

    python scripts/repair_rollover_identity.py            # dry run, prints a plan
    python scripts/repair_rollover_identity.py --apply    # execute in ONE transaction

THE MAPPING IS COMMISSIONER-SUPPLIED, NOT DERIVED. FPL team names changed too, so
only six of the ten are recognisable from the data ("Fighting Franckes", "Pep's
Scraps", "Sid Hefty(+III)", the emoji run, "Booyaka", "João"). The other four were
confirmed by the commissioner on 2026-08-19 and are recorded below. Nothing here
guesses: a name absent from MAPPING aborts the run.

Order matters. Identity has to land before `scripts/migrate_2026_draft.py --match
display` can pair the rows at all, and before the keeper carry can find its target
manager.

Idempotent: re-running fills only what is still blank and recomputes seeds in place.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models  # noqa: F401,E402 — registers tables on Base.metadata
import services  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import KeeperSeed, KeeperSelection, League, Manager  # noqa: E402

# 26/27 FPL team name -> the person (25/26 display_name).
# Confirmed by the commissioner 2026-08-19. Six were legible from the team names;
# Kerkez/Le Féez/Smashers/Woofs were not and were supplied directly.
MAPPING = {
    "Booyaka Boys": "Gaby",
    "Culver City HS🐶☕️🤴": "Tucker",
    "Fighting Franckes": "Kevin F",
    "João’s Absolute Dogs": "John",
    "Kerkez du Soleil": "Steve",
    "Le Féez Nuts": "Mark",
    "Pep’s Scraps": "Michael",
    "Sid Hefty": "Kevin T",
    "Smashers de Puppies": "Scott",
    "Woofs for Roefs": "Kevin S",
}


class Abort(RuntimeError):
    """A precondition failed. Nothing has been written."""


def resolve_rows(db):
    cur = db.query(League).filter_by(is_current=True).one_or_none()
    if cur is None:
        raise Abort("no current league row")
    prior = (
        db.query(League)
        .filter(League.season_year == (cur.season_year or 0) - 1)
        .one_or_none()
    )
    if prior is None:
        raise Abort(f"no league row for season {(cur.season_year or 0) - 1}")
    return prior, cur


def pair_managers(db, old_league, new_league):
    """[(old Manager, new Manager, person)] using the commissioner's mapping.

    Both directions are checked: an unmapped new team aborts, and so does a person
    the old row doesn't have. A partial repair is worse than none — it would leave
    some rows pairing on display_name and others not, which the draft migration would
    then silently half-apply.
    """
    old_by_person = {}
    for m in db.query(Manager).filter_by(league_id=old_league.id):
        person = (m.display_name or "").strip()
        if not person:
            raise Abort(f"old row manager {m.name!r} has no display_name to carry")
        if person in old_by_person:
            raise Abort(f"old row has two managers called {person!r}")
        old_by_person[person] = m

    pairs, problems = [], []
    new_mgrs = db.query(Manager).filter_by(league_id=new_league.id).all()
    for nm in new_mgrs:
        person = MAPPING.get(nm.name)
        if person is None:
            problems.append(f"new team {nm.name!r} is not in MAPPING")
            continue
        om = old_by_person.get(person)
        if om is None:
            problems.append(f"MAPPING sends {nm.name!r} to {person!r}, absent from the old row")
            continue
        pairs.append((om, nm, person))

    seen = [p for _o, _n, p in pairs]
    for person in set(seen):
        if seen.count(person) > 1:
            problems.append(f"{person!r} is mapped from more than one new team")
    unused = set(old_by_person) - set(seen)
    if unused:
        problems.append(f"old-row managers with no new counterpart: {sorted(unused)}")
    if problems:
        raise Abort("mapping is not a clean pairing:\n    " + "\n    ".join(problems))
    return pairs


def plan_identity(pairs):
    """What identity fields would change. Only ever FILLS blanks, never overwrites —
    re-running must not clobber a name or password set since."""
    todo = []
    for om, nm, person in pairs:
        fields = []
        if not (nm.display_name or "").strip():
            fields.append(("display_name", person))
        if om.password_hash and not nm.password_hash:
            fields.append(("password_hash", "<carried>"))
        if fields:
            todo.append((om, nm, person, fields))
    return todo


def plan_seeds(db, old_league, new_league, pairs):
    """The keeper carry `advance_season` should have done: for every player kept for
    the new season, a seed on the new row with the clock ticked down one.

    Deliberately re-derives from `_derive_keeper_status` on the OLD row rather than
    copying the old seed row, because that is exactly what advance_season does — the
    derived value already folds in trades, drops and commissioner overrides, whereas
    the raw seed is only the value the clock STARTED that season at.
    """
    new_by_old_id = {om.id: nm for om, nm, _p in pairs}
    person_by_old_id = {om.id: p for om, _n, p in pairs}
    status = services._derive_keeper_status(db, old_league)
    clubs = services._derive_gk_team_keeper_status(db, old_league)

    writes, skipped = [], []
    for ks in db.query(KeeperSelection).filter_by(
        league_id=old_league.id, season_year=new_league.season_year
    ):
        nm = new_by_old_id.get(ks.manager_id)
        person = person_by_old_id.get(ks.manager_id, "?")
        if nm is None:
            skipped.append(f"{person}: selection for a manager outside the mapping")
            continue
        if ks.team_id is not None:
            club = clubs.get(ks.manager_id)
            if club is None or club["team_id"] != ks.team_id:
                skipped.append(f"{person}: kept club no longer resolves")
                continue
            writes.append((nm, None, ks.team_id, max(club["years_remaining"] - 1, 0),
                           person, "club"))
            continue
        derived = status.get(ks.manager_id, {}).get(ks.player_id)
        if derived is None:
            # Traded away after submitting: advance_season deliberately writes nothing
            # rather than invent a fresh clock for a player the manager doesn't hold.
            skipped.append(f"{person}: {ks.player_id} no longer held (traded away)")
            continue
        writes.append((nm, ks.player_id, None,
                       max(derived["years_remaining"] - 1, 0), person,
                       derived.get("player", "?")))
    return writes, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="execute (default is a dry run that writes nothing)")
    ap.add_argument("--skip-seeds", action="store_true",
                    help="repair identity only, leave keeper seeds alone")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        old_league, new_league = resolve_rows(db)
        print(f"old : {old_league.name!r} fpl={old_league.fpl_league_id} "
              f"season={old_league.season_year}")
        print(f"new : {new_league.name!r} fpl={new_league.fpl_league_id} "
              f"season={new_league.season_year}\n")

        pairs = pair_managers(db, old_league, new_league)
        print(f"manager pairing ({len(pairs)}), commissioner-supplied:")
        for om, nm, person in sorted(pairs, key=lambda t: t[2]):
            print(f"    {person:<9} {om.name!r} (entry {om.fpl_manager_id})"
                  f"  ->  {nm.name!r} (entry {nm.fpl_manager_id})")
        print()

        ident = plan_identity(pairs)
        print(f"identity to restore ({len(ident)} managers):")
        for _om, nm, person, fields in sorted(ident, key=lambda t: t[2]):
            what = ", ".join(f"{k}={v}" for k, v in fields)
            print(f"    {person:<9} {nm.name!r}: {what}")
        if not ident:
            print("    nothing to do")
        print()

        writes, skipped = ([], [])
        if not args.skip_seeds:
            writes, skipped = plan_seeds(db, old_league, new_league, pairs)
            existing = db.query(KeeperSeed).filter_by(league_id=new_league.id).count()
            print(f"keeper seeds to write ({len(writes)}; new row currently has "
                  f"{existing}):")
            per_person: dict = {}
            for _nm, _pid, _tid, yrs, person, label in writes:
                per_person.setdefault(person, []).append(f"{label} ({yrs}y)")
            for person in sorted(per_person):
                print(f"    {person:<9} {', '.join(sorted(per_person[person]))}")
            if skipped:
                print(f"    not carried ({len(skipped)}):")
                for line in skipped[:20]:
                    print(f"        {line}")
                if len(skipped) > 20:
                    print(f"        ... and {len(skipped) - 20} more")
            print()

        if not args.apply:
            print("DRY RUN — nothing written. Re-run with --apply to execute.")
            return 0

        for _om, nm, person, fields in ident:
            for key, _v in fields:
                if key == "display_name":
                    nm.display_name = person
                elif key == "password_hash":
                    om = next(o for o, n, _p in pairs if n.id == nm.id)
                    nm.password_hash = om.password_hash
        for nm, pid, tid, yrs, _person, _label in writes:
            q = db.query(KeeperSeed).filter_by(manager_id=nm.id)
            seed = (q.filter_by(player_id=pid).one_or_none() if pid is not None
                    else q.filter_by(team_id=tid).one_or_none())
            if seed:
                seed.years_remaining = yrs
                seed.league_id = new_league.id
                seed.season_year = new_league.season_year
            else:
                db.add(KeeperSeed(
                    league_id=new_league.id, manager_id=nm.id, player_id=pid,
                    team_id=tid, years_remaining=yrs,
                    season_year=new_league.season_year,
                ))
        db.commit()
        print(f"APPLIED: {len(ident)} managers repaired, {len(writes)} seeds written.")
        return 0

    except Abort as e:
        db.rollback()
        print(f"ABORTED: {e}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
