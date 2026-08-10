"""league sync_locked (freeze finished seasons against the FPL feed)

Revision ID: a7b8c9d0e1f2
Revises: e5f6a7b8c9d0
Create Date: 2026-08-10

FPL recycles numeric league ids between seasons, so a finished season's
`fpl_league_id` can start resolving to a different league entirely. `sync_locked`
freezes the row: sync skips it rather than merging a stranger's data into our
history.
"""

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "leagues",
        sa.Column(
            "sync_locked", sa.Boolean(), nullable=False, server_default="false"
        ),
    )


def downgrade() -> None:
    op.drop_column("leagues", "sync_locked")
