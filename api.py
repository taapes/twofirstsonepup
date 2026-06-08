"""Read-only public API (v1). Serves precomputed data from our tables only."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import services
from db import get_db

router = APIRouter(prefix="/v1")


def _league(db: Session, league_key: str):
    league = services.resolve_league(db, league_key)
    if not league:
        raise HTTPException(status_code=404, detail="league not found")
    return league


@router.get("/leagues/{league_key}/standings")
def standings(league_key: str, db: Session = Depends(get_db)):
    return services.get_standings(db, _league(db, league_key))


@router.get("/leagues/{league_key}/rosters")
def rosters(league_key: str, db: Session = Depends(get_db)):
    return services.get_rosters(db, _league(db, league_key))


@router.get("/leagues/{league_key}/injury-list")
def injury_list(league_key: str, db: Session = Depends(get_db)):
    return services.get_injury_list(db, _league(db, league_key))


@router.get("/leagues/{league_key}/infractions")
def infractions(league_key: str, db: Session = Depends(get_db)):
    return services.get_infractions(db, _league(db, league_key))


@router.get("/leagues/{league_key}/cups")
def cups(league_key: str, db: Session = Depends(get_db)):
    return services.get_cups(db, _league(db, league_key))


@router.get("/leagues/{league_key}/payouts")
def payouts(league_key: str, db: Session = Depends(get_db)):
    return services.get_payouts(db, _league(db, league_key))


@router.get("/leagues/{league_key}/keepers")
def keepers(league_key: str, db: Session = Depends(get_db)):
    return services.get_keepers(db, _league(db, league_key))


@router.get("/leagues/{league_key}/keeper-selections/{season_year}")
def keeper_selections(league_key: str, season_year: int, db: Session = Depends(get_db)):
    return services.get_keeper_selections(db, _league(db, league_key), season_year)


@router.get("/leagues/{league_key}/draft/{season_year}")
def draft_board(
    league_key: str, season_year: int, draft_type: str = "main",
    db: Session = Depends(get_db),
):
    return services.get_draft_board(db, _league(db, league_key), season_year, draft_type)


@router.get("/leagues/{league_key}/players")
def players(
    league_key: str,
    q: str | None = None,
    position: str | None = None,
    available_year: int | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """Search the player pool by name/position; pass available_year to show only
    players still draftable (not kept or already drafted that season)."""
    return services.search_players(
        db, _league(db, league_key), q=q, position=position,
        available_year=available_year, limit=limit,
    )
