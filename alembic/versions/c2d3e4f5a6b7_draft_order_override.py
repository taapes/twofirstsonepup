"""draft_order_override — commissioner-set pick order for rounds 2+

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-08-11

Rounds 2+ are derived from the final standings, but the commissioner sometimes
needs to override that. One row per position in an ordered list of managers;
`round IS NULL` is the base order used by every round from 2 on, and a row with a
round beats the base for that round. Round 1 keeps its own order in draft_lottery.

Note the unique constraint treats NULL `round` as distinct per Postgres semantics,
which is fine here: the base list is written as a delete-then-insert of the whole
list, never a per-row upsert.
"""

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "draft_order_override" in insp.get_table_names():
        return
    op.create_table(
        "draft_order_override",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("league_id", sa.UUID(), nullable=False),
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column("draft_type", sa.String(), server_default="main", nullable=False),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("manager_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["manager_id"], ["managers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "league_id", "season_year", "draft_type", "round", "position",
            name="uq_draft_order_override_slot",
        ),
    )
    op.create_index(
        "ix_draft_order_override_league_id", "draft_order_override", ["league_id"]
    )
    op.create_index(
        "ix_draft_order_override_season_year", "draft_order_override", ["season_year"]
    )
    op.create_index(
        "ix_draft_order_override_manager_id", "draft_order_override", ["manager_id"]
    )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "draft_order_override" in insp.get_table_names():
        op.drop_table("draft_order_override")
