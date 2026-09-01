"""ai_generated_content + manager_notes

Revision ID: e8f9a0b1c2d3
Revises: c6d7e8f9a0b1
Create Date: 2026-09-01

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in models.py.

Two tables, zero backfill. Both are inert until ANTHROPIC_API_KEY is set.

`ai_generated_content` holds generated prose, one row per (league, gameweek, kind), and
a regenerate UPDATES in place. That is the deliberate divergence from
discovery_match_suggestions, whose point is keeping every candidate forever — a
superseded gameweek review has no value and nothing downstream wants the old draft.

`status` is the gate that keeps generated commentary off Discord until a human sends it.
It only becomes 'posted' through the admin route; the post-sync hook has no sending path.
Enforced in two places on purpose: a model cannot tell when a joke lands badly on a
particular person in a particular week, and a chat message cannot be unsent.

`attempts` counts generation attempts INCLUDING failures, which is what the per-gameweek
cap is enforced against — a row that failed twice has still cost two API calls, and a cap
that only counted successes would be no cap at all against a persistently failing key.

**A NULL `gameweek_id` defeats uq_ai_generated_content**, because in Postgres NULL !=
NULL. Harmless for 'gw_review', which always sets it. A live trap for the epic's planned
trade-commentary rows, which key off a trade instead and would silently duplicate — left
nullable anyway because that is the shape the epic's later sub-items need, and a comment
is cheaper than a second table.

`manager_notes` is keyed on the PERSON, with no league_id and no managers.id FK.
`managers` holds one row per manager per season, so an FK would need re-entering at every
rollover — the exact trap Manager.discord_user_id documents, which cost the league every
mapping at the 26/27 rollover when FPL reissued all ten entry ids. A note about a human
is true across seasons.

No CHECK constraints, matching the rest of this schema, where invariants are
write-path-enforced. Enum-ish columns are plain String with the values in a comment.

Autogenerate proposes nothing else at this head — verified clean by c6d7e8f9a0b1.

The revision id was picked by hand to follow the house rolling-hex convention, and the
FIRST attempt (`d7e8f9a0b1c2`) collided with player_code_and_player_season. Alembic's
symptom is "Multiple head revisions are present", which reads like a branching problem
and is not one — check `uniq -d` over every `^revision =` in versions/ before blaming
the graph.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "e8f9a0b1c2d3"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_generated_content",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False
        ),
        sa.Column(
            "gameweek_id",
            UUID(as_uuid=True),
            sa.ForeignKey("gameweeks.id"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(), nullable=False),  # 'gw_review'
        sa.Column(
            "status", sa.String(), nullable=False, server_default="ready"
        ),  # 'ready' | 'posted' | 'discarded' | 'failed'
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "league_id", "gameweek_id", "kind", name="uq_ai_generated_content"
        ),
    )
    op.create_index(
        "ix_ai_generated_content_league_id", "ai_generated_content", ["league_id"]
    )
    op.create_index(
        "ix_ai_generated_content_gameweek_id", "ai_generated_content", ["gameweek_id"]
    )
    op.create_index("ix_ai_generated_content_kind", "ai_generated_content", ["kind"])

    op.create_table(
        "manager_notes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("person", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("person", name="uq_manager_note_person"),
    )
    op.create_index("ix_manager_notes_person", "manager_notes", ["person"])


def downgrade() -> None:
    op.drop_index("ix_manager_notes_person", table_name="manager_notes")
    op.drop_table("manager_notes")
    for ix in (
        "ix_ai_generated_content_kind",
        "ix_ai_generated_content_gameweek_id",
        "ix_ai_generated_content_league_id",
    ):
        op.drop_index(ix, table_name="ai_generated_content")
    op.drop_table("ai_generated_content")
