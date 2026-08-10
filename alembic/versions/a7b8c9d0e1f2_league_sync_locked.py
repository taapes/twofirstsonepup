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
# On `main` this chains straight off the audit-log head. The v2/in-app-league
# branch carries the same revision id chained after its v2_* migrations instead,
# so prod (already stamped a7b8c9d0e1f2 by the v2 chain) resolves either way and
# `alembic upgrade head` is a no-op here. Reconcile the two copies when v2 merges.
down_revision = "fc891ac1df16"
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
