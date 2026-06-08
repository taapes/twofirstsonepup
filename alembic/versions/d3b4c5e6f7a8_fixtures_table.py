"""fixtures table

Revision ID: d3b4c5e6f7a8
Revises: c2a3b4d5e6f7
Create Date: 2026-06-08 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'd3b4c5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'c2a3b4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fixtures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("fpl_fixture_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.Integer(), nullable=True),
        sa.Column("kickoff_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("home_team", sa.String(), nullable=True),
        sa.Column("away_team", sa.String(), nullable=True),
        sa.Column("home_difficulty", sa.Integer(), nullable=True),
        sa.Column("away_difficulty", sa.Integer(), nullable=True),
        sa.Column("finished", sa.Boolean(), server_default="false", nullable=False),
        sa.UniqueConstraint("league_id", "fpl_fixture_id", name="uq_fixture_league_fplid"),
    )
    op.create_index("ix_fixtures_league_id", "fixtures", ["league_id"])
    op.create_index("ix_fixtures_fpl_fixture_id", "fixtures", ["fpl_fixture_id"])
    op.create_index("ix_fixtures_event", "fixtures", ["event"])


def downgrade() -> None:
    op.drop_index("ix_fixtures_event", table_name="fixtures")
    op.drop_index("ix_fixtures_fpl_fixture_id", table_name="fixtures")
    op.drop_index("ix_fixtures_league_id", table_name="fixtures")
    op.drop_table("fixtures")
