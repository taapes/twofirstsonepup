"""discord inbound: managers.discord_user_id + discord_messages + discord_ingests

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-08-29

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in models.py.

Three additions, zero backfill. Every existing row keeps NULL in the new column and the
two tables start empty; the feature is inert until a channel is configured.

`managers.discord_user_id` is what makes the whole inbound half safe, because it turns
the hardest part of the problem — "which human is this?" — into a lookup instead of a
name match. The UNIQUE is deliberately **(league_id, discord_user_id)** and NOT global:
`managers` holds one row per manager PER SEASON, so one human legitimately owns a row in
every season and a global unique index would break the first rollover after this ships.
`advance_season` carries the value forward alongside display_name and password_hash,
without which the mapping would silently empty at that rollover and every proposal would
quietly lose confidence with nothing reporting it.

`discord_messages` stores the raw message BEFORE anything parses it, which is what makes
the pipeline replayable: a parser bug is fixed by re-running over stored rows rather than
re-asking Discord. `discord_message_id` is UNIQUE and doubles as the poll cursor
(snowflakes are monotonic). It is a String, not a BigInteger: a snowflake exceeds 2^53,
which is why Discord itself sends them as strings.

`discord_ingests` holds PROPOSALS. Nothing here is ever applied automatically — see the
model docstring for why that is permanent rather than cautious (an IL announcement does
not name the replacement player the write requires). UNIQUE
(discord_message_id, kind, dedupe_key) makes re-parsing idempotent; rejected rows are
kept, never deleted, so a dismissal is never re-proposed.

Two CASCADEs are added here (ingest -> message, and the message table's own rows), which
takes the schema's total to four. Same justification as discovery_match_suggestions: a
proposal is wholly derived from its message and regenerable, so it has no meaning once
the message is gone.

Autogenerate also proposed `ix_draft_queue_season_year` and `ix_side_payouts_manager_id`
— pre-existing model/DB index drift unrelated to this feature, deliberately left out.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None

_UQ_MANAGER_DISCORD = "uq_manager_discord_user"
_FK_INGEST_MESSAGE = "fk_discord_ingests_message_id_discord_messages"


def upgrade() -> None:
    op.add_column("managers", sa.Column("discord_user_id", sa.String(), nullable=True))
    op.create_index("ix_managers_discord_user_id", "managers", ["discord_user_id"])
    op.create_unique_constraint(
        _UQ_MANAGER_DISCORD, "managers", ["league_id", "discord_user_id"]
    )

    op.create_table(
        "discord_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False
        ),
        sa.Column("channel_id", sa.String(), nullable=False),
        # UNIQUE below; also the poll cursor.
        sa.Column("discord_message_id", sa.String(), nullable=False),
        sa.Column("author_discord_id", sa.String(), nullable=True),
        sa.Column("author_name", sa.String(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "parse_status", sa.String(), nullable=False, server_default="unparsed"
        ),
        sa.UniqueConstraint("discord_message_id", name="uq_discord_message_id"),
    )
    op.create_index("ix_discord_messages_league_id", "discord_messages", ["league_id"])
    op.create_index("ix_discord_messages_channel_id", "discord_messages", ["channel_id"])
    op.create_index(
        "ix_discord_messages_discord_message_id", "discord_messages",
        ["discord_message_id"],
    )
    op.create_index(
        "ix_discord_messages_author_discord_id", "discord_messages",
        ["author_discord_id"],
    )

    op.create_table(
        "discord_ingests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "discord_message_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "discord_messages.id", ondelete="CASCADE", name=_FK_INGEST_MESSAGE
            ),
            nullable=False,
        ),
        sa.Column(
            "league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False, server_default=""),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("resolution", JSONB(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("applied_entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "discord_message_id", "kind", "dedupe_key", name="uq_discord_ingest"
        ),
    )
    op.create_index(
        "ix_discord_ingests_discord_message_id", "discord_ingests",
        ["discord_message_id"],
    )
    op.create_index("ix_discord_ingests_league_id", "discord_ingests", ["league_id"])
    op.create_index("ix_discord_ingests_kind", "discord_ingests", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_discord_ingests_kind", table_name="discord_ingests")
    op.drop_index("ix_discord_ingests_league_id", table_name="discord_ingests")
    op.drop_index(
        "ix_discord_ingests_discord_message_id", table_name="discord_ingests"
    )
    op.drop_table("discord_ingests")

    for ix in (
        "ix_discord_messages_author_discord_id",
        "ix_discord_messages_discord_message_id",
        "ix_discord_messages_channel_id",
        "ix_discord_messages_league_id",
    ):
        op.drop_index(ix, table_name="discord_messages")
    op.drop_table("discord_messages")

    op.drop_constraint(_UQ_MANAGER_DISCORD, "managers", type_="unique")
    op.drop_index("ix_managers_discord_user_id", table_name="managers")
    op.drop_column("managers", "discord_user_id")
