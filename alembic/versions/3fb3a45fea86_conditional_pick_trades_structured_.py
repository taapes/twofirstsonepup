"""conditional pick trades: clause columns on trades + trade_condition_terms

Revision ID: 3fb3a45fea86
Revises: c9d0e1f2a3b4
Create Date: 2026-08-25

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in models.py.

Three nullable columns + one child table, zero backfill. Every existing row keeps NULL
in all of them and behaves exactly as before: `condition_logic IS NULL` is what "this
is an ordinary pick trade" means, and it is the discriminator every read path checks.

A condition is a CLAUSE (these columns) over TERMS (the new table). One clause per
trade row rather than a clause table, because a Trade row moves exactly one pick — a
deal with three conditional clauses is three rows, which is what a multi-pick deal
already was. The grouping a clause table would provide, the Trade row already provides.

The terms are a separate table rather than more columns because a real 2026 deal needed
a four-way OR ("one of KS winning the cup, KS winning the league, Cunha scoring 190, or
pick 12 scoring 225") and a two-way AND. A fixed column set cannot hold either, and the
first attempt at this feature — seven flat columns on `trades`, one condition each —
could represent only a third of the conditions the league had actually agreed.

`metric` deliberately admits 'manual' alongside the four evaluable metrics. That is
what makes the schema closed under conditions nothing can compute (red cards; the
eventual points of a draft slot): the clause is stored verbatim in `note` and the
commissioner rules on it via `manual_state`. The alternative is either modelling every
metric a league might ever invent, or dropping terms from a real agreement on the floor.

No CHECK constraints and no uniqueness, matching `trades`, where every invariant is
write-path-enforced (rules.validate_condition_term / rules.validate_pick_condition).

`ondelete="CASCADE"` is the second cascade in this schema, after
discovery_match_suggestions, and for the same reason: a term is wholly derived from its
trade and meaningless without it, so the alternative is `delete_trade` failing on an FK
to a table written long after it.

Autogenerate also proposed `ix_draft_queue_season_year` and `ix_side_payouts_manager_id`
— pre-existing model/DB index drift unrelated to this feature, deliberately left out so
this revision contains only the condition schema.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "3fb3a45fea86"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

# Named, not None: an auto-named FK can't be dropped by name in downgrade().
_FK_TERM_TRADE = "fk_trade_condition_terms_trade_id_trades"
_FK_TERM_PLAYER = "fk_trade_condition_terms_player_id_players"

# Declaration order, so downgrade() can just reverse it.
_TRADE_COLUMNS = (
    ("condition_logic", sa.String()),        # 'all' | 'any'; NULL = not conditional
    ("condition_effect", sa.String()),       # 'escalate_round' | 'transfer_if_met'
    ("pick_round_if_met", sa.Integer()),     # escalate_round only
)


def upgrade() -> None:
    for name, type_ in _TRADE_COLUMNS:
        op.add_column("trades", sa.Column(name, type_, nullable=True))

    op.create_table(
        "trade_condition_terms",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trade_id",
            UUID(as_uuid=True),
            sa.ForeignKey("trades.id", ondelete="CASCADE", name=_FK_TERM_TRADE),
            nullable=False,
        ),
        # rules.CONDITION_METRICS + 'manual'
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column(
            "player_id",
            UUID(as_uuid=True),
            sa.ForeignKey("players.id", name=_FK_TERM_PLAYER),
            nullable=True,
        ),
        # A PERSON NAME, never an FK — managers has one row per season and a condition
        # resolves against a season whose rows may not exist yet (FuturePick precedent).
        sa.Column("manager_name", sa.String(), nullable=True),
        sa.Column("season_year", sa.Integer(), nullable=True),
        sa.Column("comparison", sa.String(), nullable=True),
        sa.Column("threshold", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("manual_state", sa.String(), nullable=True),  # NULL | 'met' | 'not_met'
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_trade_condition_terms_trade_id", "trade_condition_terms", ["trade_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_trade_condition_terms_trade_id", table_name="trade_condition_terms")
    op.drop_table("trade_condition_terms")
    for name, _type in reversed(_TRADE_COLUMNS):
        op.drop_column("trades", name)
