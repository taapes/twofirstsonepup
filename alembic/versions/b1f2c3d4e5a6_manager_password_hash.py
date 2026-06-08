"""manager password_hash

Revision ID: b1f2c3d4e5a6
Revises: 50cce1efc367
Create Date: 2026-06-07 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1f2c3d4e5a6'
down_revision: Union[str, Sequence[str], None] = '50cce1efc367'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('managers', sa.Column('password_hash', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('managers', 'password_hash')
