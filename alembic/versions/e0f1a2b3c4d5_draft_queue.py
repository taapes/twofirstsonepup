"""draft queue

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-06-08 04:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'e0f1a2b3c4d5'
down_revision: Union[str, Sequence[str], None] = 'd9e0f1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "draft_queue",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column("draft_type", sa.String(), server_default="main", nullable=False),
        sa.Column("manager_id", UUID(as_uuid=True), sa.ForeignKey("managers.id"), nullable=False),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("rank", sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint("league_id", "season_year", "draft_type", "manager_id", "player_id",
                            name="uq_draftqueue_entry"),
    )
    op.create_index("ix_draft_queue_league_id", "draft_queue", ["league_id"])
    op.create_index("ix_draft_queue_manager_id", "draft_queue", ["manager_id"])


def downgrade() -> None:
    op.drop_index("ix_draft_queue_manager_id", table_name="draft_queue")
    op.drop_index("ix_draft_queue_league_id", table_name="draft_queue")
    op.drop_table("draft_queue")
