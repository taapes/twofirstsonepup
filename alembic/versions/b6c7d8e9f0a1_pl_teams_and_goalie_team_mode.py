"""pl_teams + leagues.goalie_team_mode — a Premier League club as an ownable asset

Revision ID: b6c7d8e9f0a1
Revises: f5a6b7c8d9e0
Create Date: 2026-08-15

From 2026 the league drafts a CLUB instead of individual goalkeepers: a manager takes
one Premier League club and owns every keeper at it. A club had no representation at
all before this — it existed only as a free-text short name on `players.current_team`,
`player_season.current_team` and `fixtures.home_team`/`away_team`, all written from
bootstrap's `teams[]` array, whose numeric ids sync built and then threw away.

`pl_teams.code` is the identity, NOT `fpl_id`. FPL's `teams[].id` is the alphabetical
1-20 index WITHIN a season and is reassigned every August as clubs are promoted and
relegated — the same trap as `players.fpl_id`, one table over. Keying a goalie-team
pick on it would silently re-point every historical pick at a different club. `code`
is permanent. It is UNIQUE and NOT NULL for that reason: a row without a stable
identity would be re-inserted on the next sync.

The table is global (no `league_id`) and FPL-canonical, written only by sync — a club
is FPL truth of the same class as a player. Rows are never deleted; a relegated club
keeps its row and loses `is_current_pl`, and `last_seen_at` makes a stale pool
diagnosable (after a June rollover bootstrap still lists LAST season's twenty clubs
for weeks).

`leagues.goalie_team_mode` defaults to 'off', so this migration changes NO behaviour.
It is per-season because `leagues` is per-season, and that is the point:
`services.get_draft_board` regenerates its slot list on every read with no season
parameter, so a global 15 -> 14 pick count would retroactively truncate every archived
board at /season/{fpl_league_id}. Values: off | redraft | keeper.

HAND-WRITTEN, and it must stay that way. `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in main's Base.metadata.
"""

import sqlalchemy as sa
from alembic import op

revision = "b6c7d8e9f0a1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    if "pl_teams" not in insp.get_table_names():
        op.create_table(
            "pl_teams",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("code", sa.Integer(), nullable=False),
            sa.Column("fpl_id", sa.Integer(), nullable=True),
            sa.Column("short_name", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column(
                "is_current_pl",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        # One unique INDEX, no separate UniqueConstraint — the same shape
        # `ix_players_code` uses, and enough on its own. Declaring both leaves two
        # identical btrees on one column.
        op.create_index("ix_pl_teams_code", "pl_teams", ["code"], unique=True)
        op.create_index("ix_pl_teams_short_name", "pl_teams", ["short_name"])

    # Deliberately no data backfill. Seeding twenty clubs by hand fabricates canonical
    # data on the FPL side of the two-truths boundary; the first players sync after
    # this migration populates the table from the real feed.

    cols = {c["name"] for c in insp.get_columns("leagues")}
    if "goalie_team_mode" not in cols:
        op.add_column(
            "leagues",
            sa.Column(
                "goalie_team_mode",
                sa.String(),
                server_default="off",
                nullable=False,
            ),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())

    cols = {c["name"] for c in insp.get_columns("leagues")}
    if "goalie_team_mode" in cols:
        op.drop_column("leagues", "goalie_team_mode")

    if "pl_teams" in insp.get_table_names():
        op.drop_table("pl_teams")
