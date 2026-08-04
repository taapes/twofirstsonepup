"""v2_lineups

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-03 00:10:00.000000

App-owned weekly lineups for the v2 engine. Additive only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('v2_lineups',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('manager_id', sa.UUID(), nullable=False),
    sa.Column('gameweek_id', sa.UUID(), nullable=False),
    sa.Column('starters', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('bench', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['gameweek_id'], ['gameweeks.id'], ),
    sa.ForeignKeyConstraint(['manager_id'], ['managers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('manager_id', 'gameweek_id', name='uq_v2lineup_manager_gameweek')
    )
    op.create_index(op.f('ix_v2_lineups_gameweek_id'), 'v2_lineups', ['gameweek_id'], unique=False)
    op.create_index(op.f('ix_v2_lineups_manager_id'), 'v2_lineups', ['manager_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_v2_lineups_manager_id'), table_name='v2_lineups')
    op.drop_index(op.f('ix_v2_lineups_gameweek_id'), table_name='v2_lineups')
    op.drop_table('v2_lineups')
