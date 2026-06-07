"""Read-only query helpers serving PRECOMPUTED data from our tables.

Per the architecture (CLAUDE.md), these never call the FPL API — they read the
synced/normalized rows. Shared by the JSON API (api.py) and the homepage
(main.py) so both render the same data.
"""

from sqlalchemy.orm import Session

from models import (
    Gameweek,
    GameweekPoints,
    InjuryList,
    League,
    Manager,
    Player,
    Roster,
    Standing,
)
from rules import longest_zero_minute_run, ANTI_TANKING_MIN_RUN


def resolve_league(db: Session, league_key: str) -> League | None:
    """Look up a league by its FPL league id (the public, stable identifier)."""
    return db.query(League).filter_by(fpl_league_id=str(league_key)).one_or_none()


def latest_gameweek(db: Session, league: League) -> Gameweek | None:
    return (
        db.query(Gameweek)
        .filter_by(league_id=league.id)
        .order_by(Gameweek.number.desc())
        .first()
    )


def get_standings(db: Session, league: League) -> list[dict]:
    rows = (
        db.query(Standing, Manager)
        .join(Manager, Manager.id == Standing.manager_id)
        .filter(Standing.league_id == league.id)
        .order_by(Standing.rank_sort.asc().nulls_last(), Standing.rank.asc())
        .all()
    )
    return [
        {
            "rank": s.rank,
            "last_rank": s.last_rank,
            "manager": m.name,
            "total": s.total,
            "points_for": s.points_for,
            "points_against": s.points_against,
            "matches_won": s.matches_won,
            "matches_drawn": s.matches_drawn,
            "matches_lost": s.matches_lost,
        }
        for s, m in rows
    ]


def get_rosters(db: Session, league: League) -> list[dict]:
    """Current rosters (latest synced gameweek), grouped by manager."""
    gw = latest_gameweek(db, league)
    managers = (
        db.query(Manager).filter_by(league_id=league.id).order_by(Manager.name).all()
    )
    out = []
    for m in managers:
        players = []
        if gw is not None:
            players = (
                db.query(Player)
                .join(Roster, Roster.player_id == Player.id)
                .filter(Roster.manager_id == m.id, Roster.gameweek_id == gw.id)
                .order_by(Player.position, Player.name)
                .all()
            )
        out.append(
            {
                "manager": m.name,
                "players": [
                    {"name": p.name, "position": p.position, "team": p.current_team}
                    for p in players
                ],
            }
        )
    return out


def get_injury_list(db: Session, league: League) -> list[dict]:
    """Active injury-list entries for the league (admin-managed; may be empty)."""
    rows = (
        db.query(InjuryList, Manager, Player)
        .join(Manager, Manager.id == InjuryList.manager_id)
        .join(Player, Player.id == InjuryList.player_id)
        .filter(Manager.league_id == league.id, InjuryList.status == "active")
        .all()
    )
    return [
        {
            "manager": m.name,
            "player": p.name,
            "start_gw": il.start_gw,
            "end_gw": il.end_gw,
            "status": il.status,
        }
        for il, m, p in rows
    ]


def get_infractions(db: Session, league: League) -> list[dict]:
    """Anti-tanking infractions for the latest synced gameweek (precomputed read)."""
    gw = latest_gameweek(db, league)
    if gw is None:
        return []
    rows = (
        db.query(GameweekPoints, Manager)
        .join(Manager, Manager.id == GameweekPoints.manager_id)
        .filter(Manager.league_id == league.id, GameweekPoints.gameweek_id == gw.id)
        .all()
    )
    infractions = []
    for gp, m in rows:
        run = longest_zero_minute_run(gp.player_points or [])
        if run >= ANTI_TANKING_MIN_RUN:
            infractions.append(
                {
                    "manager": m.name,
                    "gameweek": gw.number,
                    "consecutive_zero_minute": run,
                    "rule": f"{ANTI_TANKING_MIN_RUN}+ consecutive 0-minute starters",
                }
            )
    return infractions
