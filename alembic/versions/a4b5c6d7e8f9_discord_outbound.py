"""discord outbound: trades.announced_at + discord_alerts

Revision ID: a4b5c6d7e8f9
Revises: 3fb3a45fea86
Create Date: 2026-08-29

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in models.py.

Two markers, one for each direction of "have I already said this?".

`trades.announced_at` is the announce queue: the sweep is `WHERE announced_at IS NULL`.
It is nullable and **back-stamped on every existing row** by this migration, which is
the entire reason the migration has a data step. Without the back-stamp the first
deploy would treat the league's whole trade history as unannounced and dump years of
it into the channel in one burst. `func.now()` is right for the stamp rather than each
trade's own date: the column records when we ANNOUNCED, and we are announcing nothing
for these — we are declining to.

The marker has to be persisted rather than held in memory because trades also arrive
from the FPL feed, and sync is idempotent and re-runs constantly. An in-process guard
would re-announce every synced trade on every run.

`discord_alerts` is the same idea for commissioner alerts, which have no row to stamp:
`flagged_actions`/`data_health` are recomputed from scratch every sync and carry no
ids, so dedupe is content-addressed on a hash of the rendered alert. UNIQUE
(league_id, fingerprint) is what makes the sweep idempotent under a concurrent or
retried sync. See the DiscordAlert docstring for why that choice also sets a sane
re-alert cadence.

No CHECK constraints, matching the rest of this schema, where invariants are
write-path-enforced. No FK from discord_alerts to anything but leagues: an alert is
about a situation, not a row, and the situations it describes (a player parked on the
IL, a squad with three blanks) have no single owning table.

Autogenerate also proposed `ix_draft_queue_season_year` and `ix_side_payouts_manager_id`
— pre-existing model/DB index drift unrelated to this feature, deliberately left out.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "a4b5c6d7e8f9"
down_revision = "3fb3a45fea86"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Back-stamp EVERY existing trade. See the module docstring: this is the guard
    # against a first-deploy flood, and it must run before anything can sweep.
    op.execute("UPDATE trades SET announced_at = now()")

    op.create_table(
        "discord_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "league_id",
            UUID(as_uuid=True),
            sa.ForeignKey("leagues.id"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("league_id", "fingerprint", name="uq_discord_alert"),
    )
    op.create_index("ix_discord_alerts_league_id", "discord_alerts", ["league_id"])
    op.create_index("ix_discord_alerts_fingerprint", "discord_alerts", ["fingerprint"])


def downgrade() -> None:
    op.drop_index("ix_discord_alerts_fingerprint", table_name="discord_alerts")
    op.drop_index("ix_discord_alerts_league_id", table_name="discord_alerts")
    op.drop_table("discord_alerts")
    op.drop_column("trades", "announced_at")
