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
    # When true, public edits (draft picks, trades) are frozen; commissioner can
    # still write. Toggled from the admin tools.
    writes_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # Separate freeze for keeper selections (e.g. after the keeper deadline).
    keepers_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


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
    name: Mapped[str] = mapped_column(String)  # FPL team name (synced; changes YoY)
    # League-custom person/display name (e.g. "Kevin T"). Stable across seasons;
    # sync never overwrites it. The stable identity for historical/manager views.
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    # Per-manager UI login password (league-custom). NULL = not set yet -> the
    # manager sets one on first login; an admin reset clears it back to NULL.
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    @property
    def display(self) -> str:
        return self.display_name or self.name


class Player(Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fpl_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    position: Mapped[str | None] = mapped_column(String, nullable=True)
    current_team: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # now_cost (x10)
    last_season_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fpl_added_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    is_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    # Rich season stats from the classic FPL bootstrap (canonical; overwritten each
    # sync). Decimal-ish fields kept as strings exactly as FPL returns them.
    form: Mapped[str | None] = mapped_column(String, nullable=True)
    points_per_game: Mapped[str | None] = mapped_column(String, nullable=True)
    total_points: Mapped[int | None] = mapped_column(Integer, nullable=True)  # this season
    goals_scored: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assists: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clean_sheets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bonus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ict_index: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_by_percent: Mapped[str | None] = mapped_column(String, nullable=True)
    news: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class Fixture(Base):
    """A real-life Premier League match (canonical, from the classic FPL fixtures
    feed). Lets us show each rostered player's upcoming opponent + difficulty.
    Teams stored as short names (e.g. 'MCI') to join against players.current_team."""

    __tablename__ = "fixtures"
    __table_args__ = (
        UniqueConstraint("league_id", "fpl_fixture_id", name="uq_fixture_league_fplid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    fpl_fixture_id: Mapped[int] = mapped_column(Integer, index=True)
    event: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # GW number
    kickoff_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    home_team: Mapped[str | None] = mapped_column(String, nullable=True)
    away_team: Mapped[str | None] = mapped_column(String, nullable=True)
    home_difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


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
    # Gameweek (FPL event) the trade processed in — aligns trades with roster
    # diffs so a traded-away player isn't mistaken for a drop.
    event_gw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fpl_trade_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    draft_pick: Mapped[str | None] = mapped_column(String, nullable=True)  # human label
    # Structured pick-trade fields (commissioner-entered; not in the FPL feed).
    # When set, this row moves a draft pick rather than a player: the slot
    # (season, draft_type, round) originally owned by pick_original_manager moves
    # from_manager -> to_manager. The draft board applies the latest such move.
    pick_season_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pick_draft_type: Mapped[str | None] = mapped_column(String, nullable=True)
    pick_round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pick_original_manager: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), nullable=True
    )
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


class KeeperSeed(Base):
    """Imported keeper state per (manager, player) from the Current Teams sheet:
    `years_remaining` = how many more seasons the player may be kept (0 = maxed,
    can't keep), as of entering the next selection. One row per (manager, player)."""

    __tablename__ = "keeper_seeds"
    __table_args__ = (
        UniqueConstraint("manager_id", "player_id", name="uq_keeper_seed_mgr_player"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    years_remaining: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    season_year: Mapped[int | None] = mapped_column(Integer, nullable=True)


class KeeperSelection(Base):
    """A manager's chosen keepers for an upcoming season (submitted pre-draft).
    Validated against eligibility + caps before persisting. One row per kept
    player per season; `is_discovery` marks the bonus 6th (discovery) keeper."""

    __tablename__ = "keeper_selections"
    __table_args__ = (
        UniqueConstraint(
            "manager_id", "player_id", "season_year", name="uq_keeper_sel_mgr_player_season"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    season_year: Mapped[int] = mapped_column(Integer, index=True)
    is_discovery: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class DraftPick(Base):
    """An actual selection made during a draft. The board (slot order/ownership)
    is computed on read from the draft order + keepers + pick trades; this table
    records picks as they're made live. manager_id = the picking (owning) manager."""

    __tablename__ = "draft_picks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    season_year: Mapped[int] = mapped_column(Integer, index=True, server_default="0")
    draft_type: Mapped[str] = mapped_column(String, server_default="main")  # main/discovery
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


class SeasonHistory(Base):
    """Historical season results (one row per season), imported from the league
    sheet. Winners are stored as person names (text, not FKs) since some are past
    members no longer in the league."""

    __tablename__ = "season_history"
    __table_args__ = (
        UniqueConstraint("league_id", "year", name="uq_season_history_league_year"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    year: Mapped[str] = mapped_column(String)  # e.g. "25/26"
    league_winner: Mapped[str | None] = mapped_column(String, nullable=True)
    cup_winner: Mapped[str | None] = mapped_column(String, nullable=True)
    pup_winner: Mapped[str | None] = mapped_column(String, nullable=True)


class TradeNote(Base):
    """Free-text historical trade (from the Trades sheet) that can't be normalized
    — picks, players, and conditionals as written. Shown as text beneath the
    structured trades."""

    __tablename__ = "trade_notes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    season: Mapped[str] = mapped_column(String, index=True)  # "25/26"
    manager_a: Mapped[str | None] = mapped_column(String, nullable=True)
    gives_a: Mapped[str | None] = mapped_column(Text, nullable=True)
    manager_b: Mapped[str | None] = mapped_column(String, nullable=True)
    gives_b: Mapped[str | None] = mapped_column(Text, nullable=True)


class CupMatch(Base):
    """Historical cup/pup-cup bracket entries (one row per team per round), parsed
    from the (inconsistent, free-text) Cup sheet. Manager kept as a text label;
    scores may be missing. `slot` preserves matchup pairing order within a round."""

    __tablename__ = "cup_matches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    season: Mapped[str] = mapped_column(String, index=True)  # "25/26"
    bracket: Mapped[str] = mapped_column(String)  # "cup" | "pup"
    round: Mapped[int] = mapped_column(Integer)  # 1=R1, 2=SF, 3=Final
    slot: Mapped[int] = mapped_column(Integer)  # order within (bracket, round)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manager_label: Mapped[str | None] = mapped_column(String, nullable=True)
    gw1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gw2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DiscoveryResult(Base):
    """Historical discovery-draft results (per season). Player names are free text
    (historical, not linked to the FPL player table). Manager is a person name."""

    __tablename__ = "discovery_results"
    __table_args__ = (
        UniqueConstraint("league_id", "season", "pick_number", name="uq_discovery_result"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    season: Mapped[str] = mapped_column(String, index=True)  # "25/26"
    round: Mapped[int] = mapped_column(Integer)
    pick_number: Mapped[int] = mapped_column(Integer)
    manager_name: Mapped[str | None] = mapped_column(String, nullable=True)
    player_name: Mapped[str | None] = mapped_column(String, nullable=True)


class FuturePick(Base):
    """Future draft-pick ownership imported from the Future Picks sheet (left grid
    only). One row per pick that has changed hands: original owner -> current
    owner, by person name. 'Own' (kept) cells are not stored."""

    __tablename__ = "future_picks"
    __table_args__ = (
        UniqueConstraint(
            "league_id", "season_year", "draft_type", "round", "original_owner",
            name="uq_future_pick",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    season_year: Mapped[int] = mapped_column(Integer, index=True)
    draft_type: Mapped[str] = mapped_column(String, server_default="main")
    round: Mapped[int] = mapped_column(Integer)
    original_owner: Mapped[str] = mapped_column(String)  # person name
    owner: Mapped[str] = mapped_column(String)  # person who now owns it
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class HistoricalStanding(Base):
    """Per-season final standings imported from the sheet. Team/stats may be
    absent for older seasons (manager-only rows). Manager stored as person text."""

    __tablename__ = "historical_standings"
    __table_args__ = (
        UniqueConstraint("league_id", "year", "rank", name="uq_hist_standing_year_rank"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    year: Mapped[str] = mapped_column(String, index=True)
    rank: Mapped[int] = mapped_column(Integer)
    team_name: Mapped[str | None] = mapped_column(String, nullable=True)
    manager_name: Mapped[str | None] = mapped_column(String, nullable=True)
    wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draws: Mapped[int | None] = mapped_column(Integer, nullable=True)
    losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points_for: Mapped[int | None] = mapped_column(Integer, nullable=True)
    h2h_points: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ManagerHonors(Base):
    """Career title/cup tally per person, imported from the sheet. Manually
    maintained there (predates the per-season rows we have), so stored as-is."""

    __tablename__ = "manager_honors"
    __table_args__ = (
        UniqueConstraint("league_id", "manager_name", name="uq_honors_league_manager"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_name: Mapped[str] = mapped_column(String)  # person name
    titles: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cups: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class StandingAdjustment(Base):
    """Commissioner standings adjustment, stored as a RELATIVE delta (e.g. a -3
    H2H / -10 total deduction). Deltas accumulate and are applied on top of the
    live synced standings at read time, so they persist as standings update. Also
    the evidence trail."""

    __tablename__ = "standing_adjustments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    total_delta: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    points_for_delta: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    gameweek: Mapped[int | None] = mapped_column(Integer, nullable=True)  # when applied
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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
