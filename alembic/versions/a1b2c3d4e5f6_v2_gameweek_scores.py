"""v2_gameweek_scores

Revision ID: a1b2c3d4e5f6
Revises: fc891ac1df16
Create Date: 2026-08-03 00:00:00.000000

Isolated table for the v2 in-app scoring engine (dual-run). Additive only —
does not touch the FPL-sourced gameweek_points.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'fc891ac1df16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('v2_gameweek_scores',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('manager_id', sa.UUID(), nullable=False),
    sa.Column('gameweek_id', sa.UUID(), nullable=False),
    sa.Column('total', sa.Integer(), nullable=True),
    sa.Column('breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('team_goals', sa.Integer(), nullable=True),
    sa.Column('team_assists', sa.Integer(), nullable=True),
    sa.Column('team_clean_sheets', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['gameweek_id'], ['gameweeks.id'], ),
    sa.ForeignKeyConstraint(['manager_id'], ['managers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('manager_id', 'gameweek_id', name='uq_v2score_manager_gameweek')
    )
    op.create_index(op.f('ix_v2_gameweek_scores_gameweek_id'), 'v2_gameweek_scores', ['gameweek_id'], unique=False)
    op.create_index(op.f('ix_v2_gameweek_scores_manager_id'), 'v2_gameweek_scores', ['manager_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_v2_gameweek_scores_manager_id'), table_name='v2_gameweek_scores')
    op.drop_index(op.f('ix_v2_gameweek_scores_gameweek_id'), table_name='v2_gameweek_scores')
    op.drop_table('v2_gameweek_scores')
