"""league phase lifecycle columns

Revision ID: f5d6e7a8b9c0
Revises: e4c5d6f7a8b9
Create Date: 2026-06-08 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5d6e7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'e4c5d6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leagues", sa.Column("phase", sa.String(), server_default="offseason", nullable=False))
    op.add_column("leagues", sa.Column("phase_set_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leagues", sa.Column("phase_manual", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("leagues", sa.Column("discovery_open", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("leagues", sa.Column("is_current", sa.Boolean(), server_default="false", nullable=False))
    # Existing single league (25/26) is the current season; the season is over
    # (GW38 done), so it starts in the offseason phase.
    op.execute("UPDATE leagues SET is_current = true, phase = 'offseason'")


def downgrade() -> None:
    op.drop_column("leagues", "is_current")
    op.drop_column("leagues", "discovery_open")
    op.drop_column("leagues", "phase_manual")
    op.drop_column("leagues", "phase_set_at")
    op.drop_column("leagues", "phase")
