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


class KeeperSeedRequest(BaseModel):
    fpl_manager_id: str
    player_fpl_id: int
    years_remaining: int


class SubmitKeepersRequest(BaseModel):
    fpl_manager_id: str
    keeper_fpl_ids: list[int]
    season_year: int
    discovery_fpl_id: int | None = None


class SetDraftOrderRequest(BaseModel):
    fpl_manager_ids: list[str]  # round-1 pick order


class TradePickRequest(BaseModel):
    from_fpl: str
    to_fpl: str
    original_fpl: str  # whose pick slot it originally is
    round: int
    season_year: int
    draft_type: str = "main"


class TradePlayerRequest(BaseModel):
    from_fpl: str
    to_fpl: str
    player_fpl_id: int


class RecordPickRequest(BaseModel):
    season_year: int
    pick_number: int
    owner_fpl: str
    player_fpl_id: int
    draft_type: str = "main"
    round: int = 0


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


@router.post("/leagues/{league_key}/keeper-seeds")
def set_keeper_seed(
    league_key: str, body: KeeperSeedRequest, db: Session = Depends(get_db)
):
    """Option-B bootstrap: set a player's prior keeper-years for a manager."""
    league = _league(db, league_key)
    try:
        return services.set_keeper_seed(
            db,
            league,
            fpl_manager_id=body.fpl_manager_id,
            player_fpl_id=body.player_fpl_id,
            years_remaining=body.years_remaining,
        )
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leagues/{league_key}/keepers")
def submit_keepers(
    league_key: str, body: SubmitKeepersRequest, db: Session = Depends(get_db)
):
    """Validate + persist a manager's keeper selection (caps + eligibility)."""
    league = _league(db, league_key)
    try:
        return services.submit_keepers(
            db,
            league,
            fpl_manager_id=body.fpl_manager_id,
            keeper_fpl_ids=body.keeper_fpl_ids,
            season_year=body.season_year,
            discovery_fpl_id=body.discovery_fpl_id,
        )
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leagues/{league_key}/draft/order")
def set_draft_order(
    league_key: str, body: SetDraftOrderRequest, db: Session = Depends(get_db)
):
    """Set the round-1 pick order (commissioner provides the lottery result)."""
    league = _league(db, league_key)
    try:
        return services.set_draft_order(db, league, body.fpl_manager_ids)
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leagues/{league_key}/draft/trade-pick")
def trade_pick(league_key: str, body: TradePickRequest, db: Session = Depends(get_db)):
    """Record a draft-pick trade (reassigns that slot's owner on the board)."""
    league = _league(db, league_key)
    try:
        return services.trade_pick(
            db, league, from_fpl=body.from_fpl, to_fpl=body.to_fpl,
            original_fpl=body.original_fpl, round=body.round,
            season_year=body.season_year, draft_type=body.draft_type,
        )
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leagues/{league_key}/draft/trade-player")
def trade_player(league_key: str, body: TradePlayerRequest, db: Session = Depends(get_db)):
    """Record a commissioner-entered player trade (outside the FPL feed)."""
    league = _league(db, league_key)
    try:
        return services.trade_player(
            db, league, from_fpl=body.from_fpl, to_fpl=body.to_fpl,
            player_fpl_id=body.player_fpl_id,
        )
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/leagues/{league_key}/draft/record-pick")
def record_pick(league_key: str, body: RecordPickRequest, db: Session = Depends(get_db)):
    """Record a selection made at a board slot (live)."""
    league = _league(db, league_key)
    try:
        return services.record_pick(
            db, league, season_year=body.season_year, pick_number=body.pick_number,
            owner_fpl=body.owner_fpl, player_fpl_id=body.player_fpl_id,
            draft_type=body.draft_type, round=body.round,
        )
    except RuleViolation as e:
        raise HTTPException(status_code=400, detail=str(e))
