"""player rich stats

Revision ID: c2a3b4d5e6f7
Revises: b1f2c3d4e5a6
Create Date: 2026-06-07 23:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2a3b4d5e6f7'
down_revision: Union[str, Sequence[str], None] = 'b1f2c3d4e5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = [
    ("form", sa.String()),
    ("points_per_game", sa.String()),
    ("total_points", sa.Integer()),
    ("goals_scored", sa.Integer()),
    ("assists", sa.Integer()),
    ("clean_sheets", sa.Integer()),
    ("bonus", sa.Integer()),
    ("minutes", sa.Integer()),
    ("ict_index", sa.String()),
    ("selected_by_percent", sa.String()),
    ("news", sa.Text()),
]


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("players", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in reversed(_COLUMNS):
        op.drop_column("players", name)
