"""Admin (commissioner) write endpoints. All require the admin token.

Establishes the write pattern reused by future league-rule admin (keepers,
trades, cups): resolve league -> call a service that enforces rules -> map a
RuleViolation to HTTP 400.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import services
from auth import require_admin
from db import get_db
from rules import RuleViolation

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _league(db: Session, league_key: str):
    league = services.resolve_league(db, league_key)
    if not league:
        raise HTTPException(status_code=404, detail="league not found")
    return league


class PlaceOnILRequest(BaseModel):
    fpl_manager_id: str
    injured_fpl_id: int
    replacement_fpl_id: int
    start_gw: int


class ReturnFromILRequest(BaseModel):
    return_gw: int
    via: str = "manual"  # "manual" | "waiver"


class ScoreCupRoundRequest(BaseModel):
    round: int  # 1=QF/play-in, 2=SF, 3=Final
    gw1: int
    gw2: int


@router.post("/leagues/{league_key}/injury-list")
def place_on_il(
    league_key: str, body: PlaceOnILRequest, db: Session = Depends(get_db)
):
    league = _league(db, league_key)
    try:
        return services.place_on_il(
            db,
            league,
            fpl_manager_id=body.fpl_manager_id,
            injured_fpl_id=body.injured_fpl_id,
            replacement_fpl_id=body.replacement_fpl_id,
            start_gw=body.start_gw,
        )
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leagues/{league_key}/injury-list/{il_id}/return")
def return_from_il(
    league_key: str,
    il_id: str,
    body: ReturnFromILRequest,
    db: Session = Depends(get_db),
):
    league = _league(db, league_key)
    try:
        return services.return_from_il(db, league, il_id, body.return_gw, body.via)
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leagues/{league_key}/cups/generate")
def generate_cups(league_key: str, db: Session = Depends(get_db)):
    """Seed from GW28 standings and create the Cup + Pup Cup first-round matches."""
    league = _league(db, league_key)
    try:
        return services.generate_cups(db, league)
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leagues/{league_key}/cups/score-round")
def score_cup_round(
    league_key: str, body: ScoreCupRoundRequest, db: Session = Depends(get_db)
):
    """Auto-score a round from its two gameweeks and advance the bracket."""
    league = _league(db, league_key)
    try:
        return services.score_cup_round(db, league, body.round, body.gw1, body.gw2)
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))
