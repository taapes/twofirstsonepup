"""fines and tanking flag clears

Revision ID: e4c5d6f7a8b9
Revises: d3b4c5e6f7a8
Create Date: 2026-06-08 00:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'e4c5d6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'd3b4c5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("manager_id", UUID(as_uuid=True), sa.ForeignKey("managers.id"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("gameweek", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_fines_league_id", "fines", ["league_id"])
    op.create_index("ix_fines_manager_id", "fines", ["manager_id"])

    op.create_table(
        "tanking_flag_clears",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("manager_id", UUID(as_uuid=True), sa.ForeignKey("managers.id"), nullable=False),
        sa.Column("window", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("league_id", "manager_id", "window", name="uq_flag_clear"),
    )
    op.create_index("ix_tanking_flag_clears_league_id", "tanking_flag_clears", ["league_id"])
    op.create_index("ix_tanking_flag_clears_manager_id", "tanking_flag_clears", ["manager_id"])


def downgrade() -> None:
    op.drop_index("ix_tanking_flag_clears_manager_id", table_name="tanking_flag_clears")
    op.drop_index("ix_tanking_flag_clears_league_id", table_name="tanking_flag_clears")
    op.drop_table("tanking_flag_clears")
    op.drop_index("ix_fines_manager_id", table_name="fines")
    op.drop_index("ix_fines_league_id", table_name="fines")
    op.drop_table("fines")
