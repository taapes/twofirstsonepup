"""player_projection — imported point projections for a season

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-08-11

An outside analyst's projected season totals, so the Players tab can rank on
EXPECTED points. It matters because a draft is prepared in the offseason, when
`players` holds nothing but zeros for months after a rollover.

Keyed on (season_year, player_id), NOT league_id: projections are needed to prepare
for a draft, which happens BEFORE the rollover creates that season's league row
(today `leagues` holds only the 25/26 row). And NOT fpl_id: FPL reassigns element
ids every season, so an fpl_id key would silently re-point every row at a different
player at rollover.

`price` is in £m as the source writes it (15.5) — deliberately NOT tenths like
players.price, because it is that model's number, not FPL's. Every projected stat is
Float, minutes included: they are expected values, not counts.

HAND-WRITTEN, and it must stay that way. `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in main's Base.metadata.
"""

import sqlalchemy as sa
from alembic import op

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "player_projection" in insp.get_table_names():
        return
    op.create_table(
        "player_projection",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.UUID(), nullable=False),
        sa.Column("raw_name", sa.String(), nullable=False),
        sa.Column("raw_team", sa.String(), nullable=True),
        sa.Column("raw_position", sa.String(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("minutes", sa.Float(), nullable=True),
        sa.Column("goals_scored", sa.Float(), nullable=True),
        sa.Column("assists", sa.Float(), nullable=True),
        sa.Column("clean_sheets", sa.Float(), nullable=True),
        sa.Column("bonus", sa.Float(), nullable=True),
        sa.Column("defensive_contributions", sa.Float(), nullable=True),
        sa.Column("yellow_cards", sa.Float(), nullable=True),
        sa.Column("points", sa.Float(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "season_year", "player_id", name="uq_player_projection_season_player"
        ),
    )
    op.create_index("ix_player_projection_player_id", "player_projection", ["player_id"])
    op.create_index("ix_player_projection_season", "player_projection", ["season_year"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "player_projection" in insp.get_table_names():
        op.drop_table("player_projection")
