"""side payouts

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-08 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "side_payouts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False),
        sa.Column("manager_id", UUID(as_uuid=True), sa.ForeignKey("managers.id"), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("gameweek", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_side_payouts_league_id", "side_payouts", ["league_id"])


def downgrade() -> None:
    op.drop_index("ix_side_payouts_league_id", table_name="side_payouts")
    op.drop_table("side_payouts")
