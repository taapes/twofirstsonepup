"""trades.team_id — trading a goalie team

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-15

A goalie team is a tradeable asset like a player or a pick, so `trades` gains a
nullable `team_id`. One row moves the whole club.

Deliberately NOT one Trade row per goalkeeper. Two things break if you expand it:
`services._owner_maps` seeds ownership from `rosters` and applies a trade only when
the snapshot agrees the sender holds the player — a club has no roster row, so every
expanded row would fail closed and show up forever under /admin/health's "site trades
applied". And `_derive_keeper_status`'s recursive trade chain labels each player
independently, so one club would arrive carrying three different acquisition labels
and three different clocks.

No CHECK and no partial index here. `trades` is already a union of three shapes
(player, pick, and now club) with no discriminator column, and a row legitimately
carries a club in one direction and nothing in the other; a constraint tight enough to
be useful would not validate against the rows already there.

HAND-WRITTEN, and it must stay that way. `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in main's Base.metadata.
"""

import sqlalchemy as sa
from alembic import op

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("trades")}
    if "team_id" not in cols:
        op.add_column("trades", sa.Column("team_id", sa.UUID(), nullable=True))
        op.create_foreign_key("fk_trade_team", "trades", "pl_teams", ["team_id"], ["id"])


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("trades")}
    if "team_id" in cols:
        op.drop_constraint("fk_trade_team", "trades", type_="foreignkey")
        op.drop_column("trades", "team_id")
