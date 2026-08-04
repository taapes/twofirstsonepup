"""v2_waivers

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-03 00:30:00.000000

Waiver priority state + blind claims for the v2 engine. Additive only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('v2_waiver_state',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('league_id', sa.UUID(), nullable=False),
    sa.Column('manager_id', sa.UUID(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['league_id'], ['leagues.id'], ),
    sa.ForeignKeyConstraint(['manager_id'], ['managers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('league_id', 'manager_id', name='uq_v2waiver_league_manager')
    )
    op.create_index(op.f('ix_v2_waiver_state_league_id'), 'v2_waiver_state', ['league_id'], unique=False)
    op.create_index(op.f('ix_v2_waiver_state_manager_id'), 'v2_waiver_state', ['manager_id'], unique=False)

    op.create_table('v2_waiver_claims',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('league_id', sa.UUID(), nullable=False),
    sa.Column('manager_id', sa.UUID(), nullable=False),
    sa.Column('gw_number', sa.Integer(), nullable=False),
    sa.Column('add_player_id', sa.UUID(), nullable=False),
    sa.Column('drop_player_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('reason', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['add_player_id'], ['players.id'], ),
    sa.ForeignKeyConstraint(['drop_player_id'], ['players.id'], ),
    sa.ForeignKeyConstraint(['league_id'], ['leagues.id'], ),
    sa.ForeignKeyConstraint(['manager_id'], ['managers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_v2_waiver_claims_gw_number'), 'v2_waiver_claims', ['gw_number'], unique=False)
    op.create_index(op.f('ix_v2_waiver_claims_league_id'), 'v2_waiver_claims', ['league_id'], unique=False)
    op.create_index(op.f('ix_v2_waiver_claims_manager_id'), 'v2_waiver_claims', ['manager_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_v2_waiver_claims_manager_id'), table_name='v2_waiver_claims')
    op.drop_index(op.f('ix_v2_waiver_claims_league_id'), table_name='v2_waiver_claims')
    op.drop_index(op.f('ix_v2_waiver_claims_gw_number'), table_name='v2_waiver_claims')
    op.drop_table('v2_waiver_claims')
    op.drop_index(op.f('ix_v2_waiver_state_manager_id'), table_name='v2_waiver_state')
    op.drop_index(op.f('ix_v2_waiver_state_league_id'), table_name='v2_waiver_state')
    op.drop_table('v2_waiver_state')
