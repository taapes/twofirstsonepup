"""SQLAlchemy models for the FPL Draft Keeper League.

PK convention (see CLAUDE.md / docs/requirements.md):
  - Every table uses a UUID primary key, including `gameweeks` (option C:
    surrogate UUID id + integer `number` 1-38 + league scope), so the schema
    supports multiple seasons in one database.
  - `players.id` is UUID; `players.fpl_id` is the unique external FPL integer id.
  - All foreign keys are DB-level and match their target's PK type (UUID).

ORM relationships are intentionally omitted for now; we model the foreign keys
only. Relationships can be layered in when we build the rules engine.
"""

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fpl_league_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    season_year: Mapped[int] = mapped_column(Integer)
    draft_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)


class Manager(Base):
    __tablename__ = "managers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    # FPL exposes two ids per member: the `entry_id` (the team entry, used for
    # /entry/{id}/... fetches) and the `league_entry` id (used by the standings
    # and matches blocks). We store both so standings can join back to a manager.
    fpl_manager_id: Mapped[str] = mapped_column(String, index=True)  # entry_id
    fpl_league_entry_id: Mapped[str | None] = mapped_column(
        String, index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String, nullable=True)


class Player(Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fpl_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    position: Mapped[str | None] = mapped_column(String, nullable=True)
    current_team: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    fpl_added_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    is_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )


class Gameweek(Base):
    __tablename__ = "gameweeks"
    __table_args__ = (UniqueConstraint("league_id", "number", name="uq_gameweek_league_number"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    number: Mapped[int] = mapped_column(Integer)  # 1-38
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    start_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    is_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class Roster(Base):
    __tablename__ = "rosters"

    id: Mapped[uuid.UUID] = _uuid_pk()
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    gameweek_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gameweeks.id"), index=True
    )
    source: Mapped[str | None] = mapped_column(String, nullable=True)  # drafted/waiver/trade
    keeper_years: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    original_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_keeper: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_discovery: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    gameweek_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gameweeks.id"), nullable=True, index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    type: Mapped[str | None] = mapped_column(String, nullable=True)  # waiver/free_agent/trade
    action: Mapped[str | None] = mapped_column(String, nullable=True)  # add/drop
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = _uuid_pk()
    date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    from_manager: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id")
    )
    to_manager: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id")
    )
    # Nullable: pick-for-pick trades have no player.
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), nullable=True
    )
    draft_pick: Mapped[str | None] = mapped_column(String, nullable=True)  # e.g. "2026-R3"
    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)


class InjuryList(Base):
    __tablename__ = "injury_list"

    id: Mapped[uuid.UUID] = _uuid_pk()
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    # start_gw / end_gw are gameweek NUMBERS (1-38), not FK rows.
    start_gw: Mapped[int] = mapped_column(Integer)
    end_gw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replacement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), nullable=True
    )
    status: Mapped[str | None] = mapped_column(String, nullable=True)  # active/returned/waived


class KeeperException(Base):
    __tablename__ = "keeper_exceptions"

    id: Mapped[uuid.UUID] = _uuid_pk()  # surrogate PK added (doc omitted one)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    validated_gw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class DraftPick(Base):
    __tablename__ = "draft_picks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    round: Mapped[int] = mapped_column(Integer)
    pick_number: Mapped[int] = mapped_column(Integer)
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), nullable=True
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    source: Mapped[str | None] = mapped_column(String, nullable=True)  # draft/keeper/discovery


class DraftLottery(Base):
    __tablename__ = "draft_lottery"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    pick_result: Mapped[int | None] = mapped_column(Integer, nullable=True)


class GameweekPoints(Base):
    __tablename__ = "gameweek_points"
    __table_args__ = (
        UniqueConstraint("manager_id", "gameweek_id", name="uq_gwpoints_manager_gameweek"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()  # surrogate PK added (doc omitted one)
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    gameweek_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gameweeks.id"), index=True
    )
    total_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    player_points: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String)  # "Cup" / "Pup Cup"
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    start_gw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_gw: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TournamentMatch(Base):
    __tablename__ = "tournament_matches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tournament_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tournaments.id"), index=True
    )
    round: Mapped[int] = mapped_column(Integer)  # 1 = QF, 2 = SF, ...
    manager_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id")
    )
    manager_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id")
    )
    score_a: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_b: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), nullable=True
    )


class CommissionerAlert(Base):
    __tablename__ = "commissioner_alerts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    message: Mapped[str] = mapped_column(Text)  # markdown / HTML
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Standing(Base):
    """Precomputed standings snapshot from the FPL Draft league details payload.
    This league uses head-to-head scoring: `total` is H2H league points, while
    `points_for`/`points_against` are cumulative FPL points. One row per manager,
    upserted each sync (the API only returns current standings)."""

    __tablename__ = "standings"
    __table_args__ = (
        UniqueConstraint("league_id", "manager_id", name="uq_standing_league_manager"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_sort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)  # H2H points
    points_for: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points_against: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matches_played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matches_won: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matches_drawn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matches_lost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Match(Base):
    """Regular-season head-to-head match (one per manager-pairing per gameweek),
    from the league details `matches` block. Lets standings be reconstructed
    historically. `winner_id` is computed from points (the API leaves
    winning_league_entry null). Distinct from `tournament_matches` (cups)."""

    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint(
            "gameweek_id",
            "home_manager_id",
            "away_manager_id",
            name="uq_match_gw_home_away",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    gameweek_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gameweeks.id"), index=True
    )
    home_manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    away_manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    home_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), nullable=True
    )
    finished: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class SyncLog(Base):
    """Audit trail for /admin/sync runs. Not FPL-canonical and not league-custom
    truth — operational metadata so we can see when a sync ran and whether it
    succeeded. One row per sync sub-task (players / league / rosters)."""

    __tablename__ = "sync_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String, index=True)  # players/league/rosters
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
