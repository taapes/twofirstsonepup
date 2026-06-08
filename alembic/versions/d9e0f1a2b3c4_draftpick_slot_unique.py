"""draft_picks slot unique constraint

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-06-08 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd9e0f1a2b3c4'
down_revision: Union[str, Sequence[str], None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_draftpick_slot", "draft_picks",
        ["league_id", "season_year", "draft_type", "pick_number"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_draftpick_slot", "draft_picks", type_="unique")
