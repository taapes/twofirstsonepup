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


@router.get("/leagues/{league_key}/flags")
def flags(league_key: str, db: Session = Depends(get_db)):
    return services.get_flags(db, _league(db, league_key))


@router.get("/leagues/{league_key}/infractions")  # back-compat alias
def infractions(league_key: str, db: Session = Depends(get_db)):
    return services.get_flags(db, _league(db, league_key))


@router.get("/leagues/{league_key}/cups")
def cups(league_key: str, db: Session = Depends(get_db)):
    return services.get_cups(db, _league(db, league_key))


@router.get("/leagues/{league_key}/payouts")
def payouts(league_key: str, db: Session = Depends(get_db)):
    return services.get_payouts(db, _league(db, league_key))


@router.get("/leagues/{league_key}/keepers")
def keepers(league_key: str, db: Session = Depends(get_db)):
    """Keeper ELIGIBILITY per manager. No viewer is passed on purpose: /v1 is exempt
    from the login gate, so there is nobody to scope to — the `kept` flags come back
    False until keeper selections are revealed (see rules.keepers_revealed)."""
    return services.get_keepers(db, _league(db, league_key))


@router.get("/leagues/{league_key}/keeper-selections/{season_year}")
def keeper_selections(league_key: str, season_year: int, db: Session = Depends(get_db)):
    """Empty until keeper selections are revealed — this endpoint is unauthenticated
    and returns exactly the thing that is private while they're still editable."""
    return services.get_keeper_selections(db, _league(db, league_key), season_year)


@router.get("/leagues/{league_key}/draft/{season_year}")
def draft_board(
    league_key: str, season_year: int, draft_type: str = "main",
    db: Session = Depends(get_db),
):
    return services.get_draft_board(db, _league(db, league_key), season_year, draft_type)


@router.get("/leagues/{league_key}/history")
def history(league_key: str, db: Session = Depends(get_db)):
    return services.get_history(db, _league(db, league_key))


@router.get("/leagues/{league_key}/future-picks")
def future_picks(league_key: str, db: Session = Depends(get_db)):
    return services.get_future_picks(db, _league(db, league_key))


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
    players still draftable (not kept or already drafted that season).

    Kept players are NOT filtered out until keeper selections are revealed, and that
    is deliberate: this endpoint is unauthenticated, so filtering them would let
    anyone enumerate the league's keepers by diffing with and without
    available_year. Draft picks are public and still filtered."""
    league = _league(db, league_key)
    return services.search_players(
        db, league, q=q, position=position, available_year=available_year,
        # passed explicitly rather than left to the redacting default, so the filter
        # comes BACK once selections are public instead of staying off forever
        kept_all=services.keepers_revealed(league),
        limit=limit,
    )
