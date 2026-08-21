"""injury_list / international_list: last_played_gw

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-21

The signal behind the must-return alert (docs/DESIGN_IL_OWNERSHIP.md §6): the most
recent gameweek an absent player logged real minutes for his club, while he is still
off the manager's roster.

The data already exists and used to be thrown away. `sync.sync_gameweek_points` fetches
`/event/{gw}/live`, whose `elements` map carries minutes for EVERY player in the game —
not just the ones on a manager's roster — but only ever persisted minutes for players in
each manager's picks. An absent player is on nobody's roster, so his minutes were read
out of the payload and discarded. This column is where they land instead; no new HTTP
call.

Nullable, no default, no backfill: it only matters for OPEN absences going forward, and
every existing row predates the rule.

The only new stored field in the whole absence-ownership design, by design — see §6 for
why the alert deliberately does NOT widen `GameweekPoints.player_points` instead
(`rules.zero_minute_count` iterates that list and must keep meaning "FPL's lineup", not
"our notion of the squad").

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in models.py.
"""

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

_TABLES = ("injury_list", "international_list")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("last_played_gw", sa.Integer(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "last_played_gw")
