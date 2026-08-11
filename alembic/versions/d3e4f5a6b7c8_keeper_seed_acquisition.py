"""keeper_seeds.acquisition — commissioner override of the derived acquisition

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-11

Keeper eligibility is derived, and two rules turn on it: the =<2 waiver-acquired
keeper cap and the 4-year clock. rules.keeper_status treats ANY unexplained gap in a
manager's tenure as a drop, which relabels the player 'waiver' AND caps their clock —
and a missing injury-list record is enough to cause that (CLAUDE.md flags 25/26 as
having no IL records at all). keeper_seeds already held the years correction; this
adds the other half so one row is "the commissioner's correction of this player's
keeper facts".

NULL means no override: use whatever the roster history implies.
"""

import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "acquisition" not in {c["name"] for c in insp.get_columns("keeper_seeds")}:
        op.add_column(
            "keeper_seeds", sa.Column("acquisition", sa.String(), nullable=True)
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "acquisition" in {c["name"] for c in insp.get_columns("keeper_seeds")}:
        op.drop_column("keeper_seeds", "acquisition")
