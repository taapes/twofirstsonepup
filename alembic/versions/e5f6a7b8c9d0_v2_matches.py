"""v2_matches

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-03 00:40:00.000000

App-owned H2H schedule for the v2 engine. Additive only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('v2_matches',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('league_id', sa.UUID(), nullable=False),
    sa.Column('gw_number', sa.Integer(), nullable=False),
    sa.Column('home_manager_id', sa.UUID(), nullable=False),
    sa.Column('away_manager_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['away_manager_id'], ['managers.id'], ),
    sa.ForeignKeyConstraint(['home_manager_id'], ['managers.id'], ),
    sa.ForeignKeyConstraint(['league_id'], ['leagues.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('league_id', 'gw_number', 'home_manager_id', 'away_manager_id', name='uq_v2match_gw_pair')
    )
    op.create_index(op.f('ix_v2_matches_gw_number'), 'v2_matches', ['gw_number'], unique=False)
    op.create_index(op.f('ix_v2_matches_league_id'), 'v2_matches', ['league_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_v2_matches_league_id'), table_name='v2_matches')
    op.drop_index(op.f('ix_v2_matches_gw_number'), table_name='v2_matches')
    op.drop_table('v2_matches')
