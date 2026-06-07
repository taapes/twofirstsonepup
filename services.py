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
from rules import (
    ANTI_TANKING_MIN_WEEKS,
    ANTI_TANKING_MIN_ZERO_PLAYERS,
    MIN_IL_STAY_GWS,
    RuleViolation,
    il_can_return,
    il_same_position,
    tanking_windows,
    zero_minute_count,
)


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


def _window_label(window: list[int]) -> str:
    """[10, 11, 12] -> 'GW10–12'."""
    return f"GW{window[0]}–{window[-1]}"


def get_infractions(db: Session, league: League) -> list[dict]:
    """Anti-tanking infractions across all synced gameweeks (precomputed read).

    Flags a manager when >=3 of their rostered players posted 0 minutes in each
    of >=3 consecutive gameweeks. Reads minutes from gameweek_points.player_points.
    """
    rows = (
        db.query(GameweekPoints, Gameweek, Manager)
        .join(Gameweek, Gameweek.id == GameweekPoints.gameweek_id)
        .join(Manager, Manager.id == GameweekPoints.manager_id)
        .filter(Manager.league_id == league.id)
        .all()
    )
    # manager id -> {"name": str, "counts": {gw_number: zero_minute_count}}
    per_manager: dict = {}
    for gp, gw, m in rows:
        entry = per_manager.setdefault(m.id, {"name": m.name, "counts": {}})
        entry["counts"][gw.number] = zero_minute_count(gp.player_points or [])

    infractions = []
    for info in per_manager.values():
        windows = tanking_windows(info["counts"])
        if windows:
            infractions.append(
                {
                    "manager": info["name"],
                    "windows": [_window_label(w) for w in windows],
                    "rule": (
                        f"{ANTI_TANKING_MIN_ZERO_PLAYERS}+ rostered players with "
                        f"0 minutes for {ANTI_TANKING_MIN_WEEKS}+ straight GWs"
                    ),
                }
            )
    return sorted(infractions, key=lambda i: i["manager"])


# ---- injury list (admin-managed writes) ----
def _resolve_manager(db: Session, league: League, fpl_manager_id: str) -> Manager:
    m = (
        db.query(Manager)
        .filter_by(league_id=league.id, fpl_manager_id=str(fpl_manager_id))
        .one_or_none()
    )
    if not m:
        raise RuleViolation(f"manager {fpl_manager_id} not found in league")
    return m


def _resolve_player(db: Session, fpl_id: int) -> Player:
    p = db.query(Player).filter_by(fpl_id=fpl_id).one_or_none()
    if not p:
        raise RuleViolation(f"player {fpl_id} not found")
    return p


def _il_to_dict(entry: InjuryList, injured: Player, replacement: Player | None) -> dict:
    return {
        "id": str(entry.id),
        "player": injured.name,
        "position": injured.position,
        "replacement": replacement.name if replacement else None,
        "start_gw": entry.start_gw,
        "end_gw": entry.end_gw,
        "status": entry.status,
    }


def place_on_il(
    db: Session,
    league: League,
    *,
    fpl_manager_id: str,
    injured_fpl_id: int,
    replacement_fpl_id: int,
    start_gw: int,
) -> dict:
    """Place a manager's injured player on the IL with a same-position replacement.

    Enforces: one active IL player per manager; replacement same position.
    """
    manager = _resolve_manager(db, league, fpl_manager_id)
    injured = _resolve_player(db, injured_fpl_id)
    replacement = _resolve_player(db, replacement_fpl_id)

    existing = (
        db.query(InjuryList)
        .filter_by(manager_id=manager.id, status="active")
        .first()
    )
    if existing:
        raise RuleViolation("manager already has an active injury-list player")
    if not il_same_position(injured.position, replacement.position):
        raise RuleViolation(
            f"replacement is {replacement.position}, must match injured "
            f"player's position {injured.position}"
        )

    entry = InjuryList(
        player_id=injured.id,
        manager_id=manager.id,
        start_gw=start_gw,
        replacement_id=replacement.id,
        status="active",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _il_to_dict(entry, injured, replacement)


def return_from_il(
    db: Session, league: League, il_id: str, return_gw: int, via: str = "manual"
) -> dict:
    """Return an active IL player. Enforces the minimum-stay rule (a return at or
    after the season's last GW is automatic). `via='waiver'` marks a waiver return.
    """
    entry = db.get(InjuryList, il_id)
    if not entry:
        raise RuleViolation("injury-list entry not found")
    if entry.status != "active":
        raise RuleViolation(f"injury-list entry is already '{entry.status}'")
    if not il_can_return(entry.start_gw, return_gw):
        raise RuleViolation(
            f"minimum {MIN_IL_STAY_GWS}-GW stay not met "
            f"(placed GW{entry.start_gw}, return GW{return_gw})"
        )

    entry.end_gw = return_gw
    entry.status = "waived" if via == "waiver" else "returned"
    db.commit()
    db.refresh(entry)
    injured = db.get(Player, entry.player_id)
    replacement = db.get(Player, entry.replacement_id) if entry.replacement_id else None
    return _il_to_dict(entry, injured, replacement)
