"""league discovery_done flag

Revision ID: a6b7c8d9e0f1
Revises: f5d6e7a8b9c0
Create Date: 2026-06-08 02:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6b7c8d9e0f1'
down_revision: Union[str, Sequence[str], None] = 'f5d6e7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leagues", sa.Column("discovery_done", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("leagues", "discovery_done")
