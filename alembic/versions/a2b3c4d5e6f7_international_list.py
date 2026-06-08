"""international list

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-08 05:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "international_list",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("manager_id", UUID(as_uuid=True), sa.ForeignKey("managers.id"), nullable=False),
        sa.Column("start_gw", sa.Integer(), nullable=False),
        sa.Column("end_gw", sa.Integer(), nullable=True),
        sa.Column("replacement_id", UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=True),
        sa.Column("tournament", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
    )
    op.create_index("ix_international_list_player_id", "international_list", ["player_id"])
    op.create_index("ix_international_list_manager_id", "international_list", ["manager_id"])


def downgrade() -> None:
    op.drop_index("ix_international_list_manager_id", table_name="international_list")
    op.drop_index("ix_international_list_player_id", table_name="international_list")
    op.drop_table("international_list")
