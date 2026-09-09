"""discovery draft clock: draft_picks.picked_at/discovery_announced_at,
leagues.discovery_clock_anchor_*

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-09-09

HAND-WRITTEN, same reason as the prior two: autogenerate emits DROP TABLE for the six
v2_* tables live in the database but absent from models.py.

A per-pick 24h clock for the discovery draft (rules.discovery_clock), derived entirely
on read from two new facts:

`draft_picks.picked_at` — WHEN a pick was actually made. Did not exist before this;
`draft_picks` had no timestamp column at all. NULL for every pick made before this
ships, which is correct: nothing before the clock feature needs a clock reading.

`draft_picks.discovery_announced_at` — mirrors trades.announced_at exactly, for the
"a pick was made" Discord announcement (a true one-time completion event on a real
row, unlike the clock-moved/deadline-warning announcements, which have no row and
dedupe through the existing discord_alerts fingerprint table instead — no new table
needed for those).

`leagues.discovery_clock_anchor_pick` (default 1) + `discovery_clock_anchor_at` —
where the clock chain restarts from. Ordinarily (1, whenever the window opened), but
it is a general "restart the clock here" lever, not just an open-timestamp: this
lets a mid-draft feature rollout (2026, pick 1 already made before the clock rule
existed) set anchor_pick=2 once, so pick 1 stays outside the clock's scope forever
rather than backdating pick 2's clock to whenever pick 1 happened to be made.
"""

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a8"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "draft_picks",
        sa.Column("picked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "draft_picks",
        sa.Column("discovery_announced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "leagues",
        sa.Column("discovery_clock_anchor_pick", sa.Integer(), nullable=False,
                  server_default="1"),
    )
    op.add_column(
        "leagues",
        sa.Column("discovery_clock_anchor_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("leagues", "discovery_clock_anchor_at")
    op.drop_column("leagues", "discovery_clock_anchor_pick")
    op.drop_column("draft_picks", "discovery_announced_at")
    op.drop_column("draft_picks", "picked_at")
