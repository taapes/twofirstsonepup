"""v2_roster_moves

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-03 00:20:00.000000

App-owned squad ledger for the v2 engine (append-only). Additive only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('v2_roster_moves',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('league_id', sa.UUID(), nullable=False),
    sa.Column('manager_id', sa.UUID(), nullable=False),
    sa.Column('player_id', sa.UUID(), nullable=False),
    sa.Column('gw_number', sa.Integer(), nullable=False),
    sa.Column('action', sa.String(), nullable=False),
    sa.Column('source', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['league_id'], ['leagues.id'], ),
    sa.ForeignKeyConstraint(['manager_id'], ['managers.id'], ),
    sa.ForeignKeyConstraint(['player_id'], ['players.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_v2_roster_moves_gw_number'), 'v2_roster_moves', ['gw_number'], unique=False)
    op.create_index(op.f('ix_v2_roster_moves_league_id'), 'v2_roster_moves', ['league_id'], unique=False)
    op.create_index(op.f('ix_v2_roster_moves_manager_id'), 'v2_roster_moves', ['manager_id'], unique=False)
    op.create_index(op.f('ix_v2_roster_moves_player_id'), 'v2_roster_moves', ['player_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_v2_roster_moves_player_id'), table_name='v2_roster_moves')
    op.drop_index(op.f('ix_v2_roster_moves_manager_id'), table_name='v2_roster_moves')
    op.drop_index(op.f('ix_v2_roster_moves_league_id'), table_name='v2_roster_moves')
    op.drop_index(op.f('ix_v2_roster_moves_gw_number'), table_name='v2_roster_moves')
    op.drop_table('v2_roster_moves')
