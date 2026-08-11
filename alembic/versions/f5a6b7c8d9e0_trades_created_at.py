"""trades.created_at — the only reliable ordering for trades

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-08-11

`trades` had no usable ordering column: `date` is NULL on every commissioner-entered
row, and the primary key is a random uuid4. Two readers need chronology:

  - `pick_ownership` documents "the latest reassignment wins" but sorted on
    `Trade.id`, i.e. at random — wrong whenever a pick changes hands twice.
  - `player_ownership` (new) folds commissioner-entered player trades onto the FPL
    roster snapshot. A trade and a trade-back are indistinguishable without a time:
    A->B then B->A ends at A, B->A then A->B ends at B, and no graph walk can tell
    those apart.

No backfill. Existing rows all take the migration timestamp, which only matters if
two overlay-eligible trades touch the same player — and deliberately NOT derived
from `event_gw`, because a gameweek number is not a timestamp and synced rows never
enter the overlay set anyway.
"""

import sqlalchemy as sa
from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "created_at" not in {c["name"] for c in insp.get_columns("trades")}:
        op.add_column(
            "trades",
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "created_at" in {c["name"] for c in insp.get_columns("trades")}:
        op.drop_column("trades", "created_at")
