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
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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
    # Freeze against the FPL feed. FPL recycles numeric league ids between
    # seasons, so a finished season's `fpl_league_id` can start resolving to a
    # STRANGER'S league — syncing it would merge their teams into our history.
    # Set when the season ends (GW38 / rollover); sync then skips this row.
    sync_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # League lifecycle phase (drives feature availability + sync cadence). Macro
    # phase only: offseason | draft | preseason | in_season. In-season sub-states
    # (discovery / post-deadline / cup) are the `discovery_open` flag + values
    # derived from date/GW so they can't contradict the calendar.
    phase: Mapped[str] = mapped_column(
        String, nullable=False, server_default="offseason"
    )
    phase_set_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When true, the admin has pinned the phase: auto-advance during sync is skipped.
    phase_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # In-season toggle for the discovery draft window (auto-on Oct 1; admin-off).
    discovery_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # Set once the admin confirms the discovery draft is complete, so the Oct 1
    # auto-open won't re-open it for the rest of the season.
    discovery_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # The active season's league row (one True per franchise). Rollover flips it so
    # no env redeploy is needed; resolve_league falls back to the env when unset.
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # Goalie-team rule (see rules.GOALIE_TEAM_MODES): off | redraft | keeper.
    # 'off' is every pre-2026 row and the historical archive — squad shape and the
    # draft are exactly as they were. It is per-season BECAUSE `leagues` is per-season:
    # changing the rule at a rollover must not rewrite an archived season's board.
    goalie_team_mode: Mapped[str] = mapped_column(
        String, nullable=False, server_default="off"
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
    # Partial unique index: many departed players may sit at fpl_id NULL
    # simultaneously, while every live element id stays unique.
    __table_args__ = (
        Index(
            "uq_players_fpl_id_live",
            "fpl_id",
            unique=True,
            postgresql_where=text("fpl_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # FPL's PERMANENT player id (stable across seasons). `fpl_id` below is only
    # that player's element id in the CURRENT season — FPL reassigns those every
    # year, so `code` is what makes players.id mean one human forever.
    code: Mapped[int | None] = mapped_column(
        Integer, unique=True, index=True, nullable=True
    )
    # Current season's element id. Nullable: a player who leaves the PL keeps no
    # slot, and their old id gets reassigned to somebody else.
    fpl_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # FPL's `web_name` — the SHORT form ("Woltemade", "Gabriel"), not a full name.
    name: Mapped[str] = mapped_column(String)
    # FPL's `first_name + " " + second_name`. Canonical, written by sync_players;
    # nullable because it only populates on the first sync after this column landed.
    # `name` above was all we kept, which is why matching a discovery pick's
    # free-text label needed this: a manager writes "Nick Woltemade" and the pool
    # says "Woltemade". Display still uses `name` everywhere.
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
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


class PlTeam(Base):
    """A Premier League club — FPL-canonical, global, written only by sync.

    Clubs used to exist here only as a free-text short name on `players.current_team`,
    because nothing owned a club. The goalie-team rule makes a club an ownable asset,
    so it needs a row and a stable id.

    Identity is `code`, FPL's PERMANENT team code — never `fpl_id`. `teams[].id` in
    bootstrap-static is the alphabetical 1-20 index WITHIN a season and is reassigned
    every August as clubs go up and down, exactly like `players.fpl_id`; keying on it
    would re-point every historical goalie-team pick at a different club. Same lesson,
    second table.

    `short_name` ('MCI') is the join key to `players.current_team` and
    `fixtures.home_team`/`away_team`, which are written from this same payload in the
    same sync, so the two are consistent by construction.

    Rows are never deleted. A relegated club keeps its row and loses `is_current_pl`;
    its `code` comes back on promotion and reuses the row. `last_seen_at` is what makes
    a stale pool diagnosable — after a June rollover bootstrap still lists LAST
    season's twenty clubs for weeks.
    """

    __tablename__ = "pl_teams"

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    # This season's teams[].id (1-20). Informational only — never an identity.
    fpl_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    short_name: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    is_current_pl: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    # A traded goalie team. One row moves the CLUB, never one row per goalkeeper:
    # expanding it would give the receiver three independently-labelled keepers with
    # three separate clocks, and the roster-seeded ownership overlay would refuse all
    # of them anyway (a club has no `rosters` row to seed from).
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pl_teams.id"), nullable=True
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
    # Set when the commissioner corrects this row by hand. sync_trades then leaves it
    # alone: its reconciliation matches on an exact (player, from, to) triple, so a
    # corrected direction would otherwise come back as a DUPLICATE on the next sync,
    # and its upsert would rewrite event_gw straight back to whatever the feed says.
    manually_edited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # The ONLY reliable ordering for trades: `date` is NULL on every
    # commissioner-entered row and the PK is a random uuid4. Two readers depend on
    # chronology — pick_ownership ("latest reassignment wins") and player_ownership
    # (a trade and a trade-back must resolve to where the player actually ended up,
    # which no amount of graph-walking can determine without a time).
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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


class InternationalList(Base):
    """A player away at a national-team cup (AFCON / Asia Cup), temporarily replaced.
    Mirrors the InjuryList but with no minimum stay — return when the nation is
    eliminated. The same-position requirement DOES apply, same as the IL: this
    docstring used to claim otherwise while services.place_on_intl enforced it, and
    the code (and CLAUDE.md) were the ones telling the truth. Preserves keeper
    eligibility while out (covered like the IL in the keeper-drop derivation). One
    active entry per manager; one replacement for the whole absence.

    Goalkeepers are out of scope entirely once the goalie-team rule is on — you own
    every keeper at your club, so the only legal same-position replacement is one you
    already have."""

    __tablename__ = "international_list"

    id: Mapped[uuid.UUID] = _uuid_pk()
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    start_gw: Mapped[int] = mapped_column(Integer)
    end_gw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replacement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), nullable=True
    )
    tournament: Mapped[str | None] = mapped_column(String, nullable=True)  # AFCON / Asia Cup
    status: Mapped[str | None] = mapped_column(String, nullable=True)  # active/returned


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
    """The commissioner's correction of a player's keeper facts, per (manager, player).

    Started life as the Current Teams import ("Option B"): `years_remaining` = how many
    more seasons the player may be kept (0 = maxed), as of entering the next selection.
    It now also carries `acquisition`, because the derivation gets both wrong in the same
    breath — `rules.keeper_status` treats any unexplained gap in a manager's tenure as a
    drop, which both relabels the player 'waiver' (eating one of only two waiver keeper
    slots) and caps their clock. A missing injury-list record is enough to trigger it.

    NULL `acquisition` means no override — use whatever the roster history implies.
    """

    __tablename__ = "keeper_seeds"
    __table_args__ = (
        UniqueConstraint("manager_id", "player_id", name="uq_keeper_seed_mgr_player"),
        CheckConstraint(
            "num_nonnulls(player_id, team_id) = 1",
            name="ck_keeper_seed_player_or_team",
        ),
        Index(
            "uq_keeper_seed_mgr_team",
            "manager_id", "team_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True, nullable=True
    )
    # A goalie team's carried clock. Unused in the rule's first season — no club has
    # any history yet — but the rollover needs somewhere to write from year two.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pl_teams.id"), nullable=True
    )
    years_remaining: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    season_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 'draft' | 'waiver' | 'trade' | 'discovery' (rules.KEEPER_ACQUISITIONS), or NULL
    # to use the derived value. A seed always WINS over the derivation, including over
    # the 'discovery' label synthesized from a linked discovery pick.
    acquisition: Mapped[str | None] = mapped_column(String, nullable=True)


class KeeperSelection(Base):
    """A manager's chosen keepers for an upcoming season (submitted pre-draft).
    Validated against eligibility + caps before persisting. One row per kept
    player per season; `is_discovery` marks the bonus 6th (discovery) keeper."""

    __tablename__ = "keeper_selections"
    __table_args__ = (
        UniqueConstraint(
            "manager_id", "player_id", "season_year", name="uq_keeper_sel_mgr_player_season"
        ),
        CheckConstraint(
            "num_nonnulls(player_id, team_id) = 1",
            name="ck_keeper_sel_player_or_team",
        ),
        # A manager keeps at most one goalie team, and a club is kept by at most one
        # manager. Partial, because Postgres counts NULLs as distinct and every
        # ordinary player row would otherwise register as its own club.
        Index(
            "uq_keeper_sel_one_team_per_manager",
            "league_id", "manager_id", "season_year",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
        Index(
            "uq_keeper_sel_team_once",
            "league_id", "season_year", "team_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True, nullable=True
    )
    # A kept goalie team. Mutually exclusive with player_id (see the CHECK).
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pl_teams.id"), nullable=True
    )
    season_year: Mapped[int] = mapped_column(Integer, index=True)
    is_discovery: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class DraftPick(Base):
    """An actual selection made during a draft. The board (slot order/ownership)
    is computed on read from the draft order + keepers + pick trades; this table
    records picks as they're made live. manager_id = the picking (owning) manager.

    A pick names exactly one thing, and which column is set says what kind it is:
    `player_id` a player, `player_label` a free-text discovery pick (someone not yet
    in the Premier League), `team_id` a goalie team. The CHECK is deliberately narrow
    — "a team pick carries nothing else" — rather than the num_nonnulls(...) = 1 that
    would be the honest invariant, because existing discovery rows are not provably
    one-of and the migration has to apply to live data.
    """

    __tablename__ = "draft_picks"
    __table_args__ = (
        UniqueConstraint(
            "league_id", "season_year", "draft_type", "pick_number",
            name="uq_draftpick_slot",
        ),
        CheckConstraint(
            "team_id IS NULL OR (player_id IS NULL AND player_label IS NULL)",
            name="ck_draftpick_team_or_player",
        ),
        # One goalie team per manager per draft, and a club goes exactly once. Both
        # are PARTIAL: a plain UNIQUE is useless here because Postgres treats NULLs
        # as distinct, so every ordinary player pick would count as its own "club".
        Index(
            "uq_draftpick_one_team_per_manager",
            "league_id", "season_year", "draft_type", "manager_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
        Index(
            "uq_draftpick_team_once",
            "league_id", "season_year", "draft_type", "team_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
    )

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
    # Discovery-draft picks are players NOT yet in the league (future PL arrivals), so
    # they're recorded as a free-text name rather than a players FK.
    player_label: Mapped[str | None] = mapped_column(String, nullable=True)
    # A goalie team: the manager drafted this Premier League club and owns every
    # keeper at it. Mutually exclusive with player_id/player_label (see the CHECK).
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pl_teams.id"), nullable=True
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    source: Mapped[str | None] = mapped_column(String, nullable=True)  # draft/keeper/discovery


class DiscoveryMatchSuggestion(Base):
    """A PROPOSED match between a free-text discovery pick and a real player.

    A suggestion is not a link and must never become one on its own. Only
    `services.link_discovery_pick` writes `DraftPick.player_id`, and only an admin
    calls it — see that docstring for why: `players.name` is FPL's short web_name
    while managers type full names, so even a 1.0 score can be the wrong human, and a
    wrong link silently hands one manager another's keeper on a four-year clock. This
    table is just the queue of things worth asking the commissioner about.

    UNIQUE (draft_pick_id, player_id) is what makes the daily matcher idempotent: it
    upserts rather than duplicating, and a pair the commissioner already REJECTED is
    still present, so it is never proposed a second time.

    `ondelete="CASCADE"` — the only cascade in this schema, deliberately. A suggestion
    is wholly derived from its pick and regenerable at any time, so it has no meaning
    once the pick is gone; the alternative is `delete_draft_pick` failing on an FK to
    a table written years after it.
    """

    __tablename__ = "discovery_match_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "draft_pick_id", "player_id", name="uq_discovery_suggestion_pick_player"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    draft_pick_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft_picks.id", ondelete="CASCADE"),
        index=True,
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    # 0.0-1.0; 1.0 means the normalized names are identical, which is still only a
    # suggestion. Ranked descending in the dashboard.
    score: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # Which tier produced it: 'exact' | 'strong' | 'close'. Kept so a bad rule can be
    # identified from the data rather than guessed at.
    method: Mapped[str] = mapped_column(String)
    # 'pending' | 'confirmed' | 'rejected'. Rejected rows are KEPT on purpose.
    status: Mapped[str] = mapped_column(String, server_default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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


class DraftOrderOverride(Base):
    """Commissioner-set pick order for rounds 2+, overriding reverse standings.

    Rounds 2+ are normally derived from the (adjusted) final standings, but the
    commissioner sometimes needs to say otherwise. One row per position in an
    ordered list of managers:

      round IS NULL -> the base order used by EVERY round from 2 on
      round = N     -> that round only, beating the base

    Round 1 is not expressible here — it keeps its own lottery order in
    DraftLottery. Storing a list (rather than per-slot assignments) is what lets
    the same table serve a whole-order shift, a single round, and moving one
    manager within a round.
    """

    __tablename__ = "draft_order_override"
    __table_args__ = (
        UniqueConstraint(
            "league_id", "season_year", "draft_type", "round", "position",
            name="uq_draft_order_override_slot",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    season_year: Mapped[int] = mapped_column(Integer, index=True)
    draft_type: Mapped[str] = mapped_column(String, server_default="main")
    # NULL = the base order for all of rounds 2+
    round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer)  # 1..N within this order
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )


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
    # Team totals for the GW (sum over the manager's squad), used as cup tiebreakers
    # (goals, then assists, then clean sheets) over a match's two gameweeks.
    team_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_assists: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_clean_sheets: Mapped[int | None] = mapped_column(Integer, nullable=True)


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


class Fine(Base):
    """A commissioner-issued fine against a manager (league-custom). Fines reduce a
    manager's net winnings; the league winner collects the pool of all fines."""

    __tablename__ = "fines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # dollars
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    gameweek: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TankingFlagClear(Base):
    """A commissioner dismissal of a specific anti-tanking flag (manager + GW
    window). Cleared flags are hidden from the public homepage but kept as a record;
    if the underlying window changes on re-sync the flag re-appears for review."""

    __tablename__ = "tanking_flag_clears"
    __table_args__ = (
        UniqueConstraint("league_id", "manager_id", "window", name="uq_flag_clear"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    window: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "GW10–12"
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DraftQueue(Base):
    """A manager's ranked autodraft queue for a draft (main or discovery). If they're
    absent on the clock, the admin approves the queue → the top still-available player
    is picked. One row per queued player; `rank` is the order (lower = higher priority).

    A goalie team is queued in the SAME list (`team_id` instead of `player_id`), not a
    table of its own: `rank` is one ordering across everything a manager wants, and a
    manager whose last slot must be a club needs that club rankable among the players.
    """

    __tablename__ = "draft_queue"
    __table_args__ = (
        # Kept as-is for players. Postgres treats NULLs as distinct, so this no longer
        # constrains anything once player_id is nullable — hence the partial index
        # below for clubs, which is the half a plain UNIQUE cannot express.
        UniqueConstraint(
            "league_id", "season_year", "draft_type", "manager_id", "player_id",
            name="uq_draftqueue_entry",
        ),
        Index(
            "uq_draftqueue_team_entry",
            "league_id", "season_year", "draft_type", "manager_id", "team_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
        CheckConstraint(
            "num_nonnulls(player_id, team_id) = 1",
            name="ck_draftqueue_player_or_team",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    season_year: Mapped[int] = mapped_column(Integer, index=True)
    draft_type: Mapped[str] = mapped_column(String, server_default="main")  # main/discovery
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), nullable=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pl_teams.id"), nullable=True
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class SidePayout(Base):
    """A commissioner-entered side-pot credit/debit outside the main pot — e.g. the
    weekly-entry pool winner or a team-sale-clause payment. Folded into a manager's
    overall winnings. Amount may be negative (an entry/owing)."""

    __tablename__ = "side_payouts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    manager_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("managers.id"), index=True
    )
    label: Mapped[str] = mapped_column(String)  # e.g. "Weekly winner GW10" / "Team-sale clause"
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # dollars (+/-)
    gameweek: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlayerPoolSnapshot(Base):
    """The set of player fpl_ids that existed in the pool at a season's draft (captured
    at the Preseason rollover). FPL has no reliable 'added date', so this snapshot is
    how we detect mid-season additions: a synced player NOT in the snapshot was added
    after the draft (ineligible unless a defender — see player_ineligibility)."""

    __tablename__ = "player_pool_snapshot"
    __table_args__ = (
        UniqueConstraint("league_id", "fpl_id", name="uq_pool_snapshot_league_fpl"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    fpl_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlayerSeason(Base):
    """Per-season snapshot of a player's identity and stats.

    `players` is global and mutable — it always holds the CURRENT season. This table
    freezes what each player was during a given season, so historical rosters render
    the right name, club and numbers. Written by sync_players on every run in which a
    league is BOTH is_current AND not sync_locked.

    Carries BOTH ids on purpose:
      - player_id -> resolve a stable UUID (rosters, keepers) to season identity
      - fpl_id    -> resolve that season's element id (gameweek_points.player_points,
                     v2 lineups) back to a player
    """

    __tablename__ = "player_season"
    __table_args__ = (
        UniqueConstraint("league_id", "fpl_id", name="uq_player_season_league_fpl"),
        Index("ix_player_season_league_player", "league_id", "player_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    fpl_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String)
    position: Mapped[str | None] = mapped_column(String, nullable=True)
    current_team: Mapped[str | None] = mapped_column(String, nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    news: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_scored: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assists: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clean_sheets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bonus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    form: Mapped[str | None] = mapped_column(String, nullable=True)
    points_per_game: Mapped[str | None] = mapped_column(String, nullable=True)
    ict_index: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_by_percent: Mapped[str | None] = mapped_column(String, nullable=True)


class PlayerIneligibility(Base):
    """A player ruled ineligible for a season (league-custom; we never mutate the
    global canonical Player row). Currently: a non-defender added to FPL after the
    season's draft (not in the player_pool_snapshot)."""

    __tablename__ = "player_ineligibility"
    __table_args__ = (
        UniqueConstraint("league_id", "fpl_id", name="uq_ineligibility_league_fpl"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    fpl_id: Mapped[int] = mapped_column(Integer, index=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlayerProjection(Base):
    """An outside analyst's projected season totals for a player.

    League-custom truth: nothing here comes from the FPL API, and sync never touches
    it. It exists because a draft is prepared in the offseason, when `players` holds
    nothing but zeros — see stats_season — so the only numbers worth ranking on are
    expected ones.

    Scoped by (season_year, player_id). Three deliberate choices:

      - NOT league_id. Projections are needed BEFORE advance_season creates that
        season's league row; today `leagues` has only the 25/26 row. A season is the
        natural scope anyway, so this table survives a rollover untouched.
      - NOT fpl_id. FPL reassigns element ids every season, so an fpl_id key would
        silently re-point every projection at a different human at rollover — the
        incident class documented in CLAUDE.md.
      - No `value` column. Points per million is DERIVED on read from points + price,
        so it can never drift from the two numbers either side of it.

    raw_name/raw_team/raw_position keep the sheet's identity verbatim ('BRI', 'GK').
    current_team is part of the import's match key and sync rewrites it on deadline
    day, so without these a later re-import failure can't be diagnosed.
    """

    __tablename__ = "player_projection"
    __table_args__ = (
        UniqueConstraint(
            "season_year", "player_id", name="uq_player_projection_season_player"
        ),
        Index("ix_player_projection_season", "season_year"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    season_year: Mapped[int] = mapped_column(Integer)  # 2026 == the 26/27 season
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id"), index=True
    )
    raw_name: Mapped[str] = mapped_column(String)
    raw_team: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_position: Mapped[str | None] = mapped_column(String, nullable=True)
    # The SOURCE's price, in £m as the sheet writes it (15.5) — NOT tenths like
    # players.price. Kept separate because the sheet disagrees with the live pool for
    # some players, and points/price must stay consistent with what was modelled.
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Projected totals. Float for all of them, minutes included: these are expected
    # values, not counts (a projection reads G=27.5).
    minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    goals_scored: Mapped[float | None] = mapped_column(Float, nullable=True)
    assists: Mapped[float | None] = mapped_column(Float, nullable=True)
    clean_sheets: Mapped[float | None] = mapped_column(Float, nullable=True)
    bonus: Mapped[float | None] = mapped_column(Float, nullable=True)
    defensive_contributions: Mapped[float | None] = mapped_column(Float, nullable=True)
    yellow_cards: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The headline number and the reason the table exists. Everything else is
    # nullable because another source may not publish every component.
    points: Mapped[float] = mapped_column(Float)
    imported_at: Mapped[datetime.datetime] = mapped_column(
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


class AuditLog(Base):
    """Append-only trail of every action that changes a team's state — trades,
    keepers, drafts, IL/international list, tanking-flag clears, standings/fines/
    side-pots, cups, phase/season transitions, password resets. League-custom
    truth (commissioner oversight), never mutated after insert. The acting
    identity (`actor`/`actor_kind`) is captured from the request via a ContextVar
    set by middleware; `manager_ids` lists the affected teams for per-team
    filtering; `details` keeps the raw params."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    actor: Mapped[str] = mapped_column(String, nullable=False)  # "Tucker", "admin", "system (sync)"
    actor_kind: Mapped[str] = mapped_column(String, nullable=False)  # manager|admin|system
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)  # e.g. "il.place"
    summary: Mapped[str] = mapped_column(Text, nullable=False)  # human-readable one-liner
    manager_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # affected manager UUIDs
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # raw params
