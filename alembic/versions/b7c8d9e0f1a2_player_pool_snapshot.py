"""player pool snapshot

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-06-08 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_pool_snapshot",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("fpl_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("league_id", "fpl_id", name="uq_pool_snapshot_league_fpl"),
    )
    op.create_index("ix_player_pool_snapshot_league_id", "player_pool_snapshot", ["league_id"])
    op.create_index("ix_player_pool_snapshot_fpl_id", "player_pool_snapshot", ["fpl_id"])


def downgrade() -> None:
    op.drop_index("ix_player_pool_snapshot_fpl_id", table_name="player_pool_snapshot")
    op.drop_index("ix_player_pool_snapshot_league_id", table_name="player_pool_snapshot")
    op.drop_table("player_pool_snapshot")
