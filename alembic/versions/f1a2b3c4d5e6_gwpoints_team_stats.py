"""gameweek_points team tiebreak stats

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
Create Date: 2026-06-08 05:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e0f1a2b3c4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("gameweek_points", sa.Column("team_goals", sa.Integer(), nullable=True))
    op.add_column("gameweek_points", sa.Column("team_assists", sa.Integer(), nullable=True))
    op.add_column("gameweek_points", sa.Column("team_clean_sheets", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("gameweek_points", "team_clean_sheets")
    op.drop_column("gameweek_points", "team_assists")
    op.drop_column("gameweek_points", "team_goals")
