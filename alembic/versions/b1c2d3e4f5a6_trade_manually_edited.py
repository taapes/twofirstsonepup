"""trades.manually_edited — protect commissioner corrections from sync

Revision ID: b1c2d3e4f5a6
Revises: d7e8f9a0b1c2
Create Date: 2026-08-11

sync_trades reconciles a hand-entered trade to an FPL one by matching the exact
(player_id, from_manager, to_manager) triple, and upserts `event_gw`/`league_id` on
top of whatever it finds. So once an admin can correct a trade, two things break:
a corrected direction no longer matches, and the next sync inserts a DUPLICATE row;
and a corrected gameweek gets rewritten back to the feed's value.

This flag marks a row as hand-corrected so sync leaves it alone.
"""

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "manually_edited" not in {c["name"] for c in insp.get_columns("trades")}:
        op.add_column(
            "trades",
            sa.Column(
                "manually_edited", sa.Boolean(), nullable=False, server_default="false"
            ),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "manually_edited" in {c["name"] for c in insp.get_columns("trades")}:
        op.drop_column("trades", "manually_edited")
