"""player ineligibility

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-06-08 03:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_ineligibility",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("fpl_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("league_id", "fpl_id", name="uq_ineligibility_league_fpl"),
    )
    op.create_index("ix_player_ineligibility_league_id", "player_ineligibility", ["league_id"])
    op.create_index("ix_player_ineligibility_fpl_id", "player_ineligibility", ["fpl_id"])


def downgrade() -> None:
    op.drop_index("ix_player_ineligibility_fpl_id", table_name="player_ineligibility")
    op.drop_index("ix_player_ineligibility_league_id", table_name="player_ineligibility")
    op.drop_table("player_ineligibility")
