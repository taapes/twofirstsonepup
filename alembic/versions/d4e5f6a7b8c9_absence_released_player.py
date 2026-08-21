"""injury_list / international_list: released_player_id

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-20

The player a manager gives up to bring an absentee back AFTER GW38.

Mid-season this column stays NULL and is meant to: the manager makes the swap in the
FPL app and the next roster snapshot shows it. After GW38 there is no next snapshot —
`advance_season` freezes the league — so without somewhere to record the swap the
manager would carry the absentee PLUS a full 15 into keeper selection and choose five
keepers out of sixteen while everyone else chooses out of fifteen.

It is MANAGER-DESIGNATED, never inferred. FPL records no paired add/drop (we
reconstruct them by diffing consecutive roster snapshots), so a manager who drops two
midfielders and adds two midfielders in one gameweek produces a diff with no fact in it
saying which arrival replaced which departure. An earlier design tried to follow the
replacement's slot forward automatically and was withdrawn for exactly that reason —
see docs/DESIGN_IL_OWNERSHIP.md §5.

Nullable, no default, no backfill: every existing row predates the rule, and NULL is
the correct answer for all of them. `services._absence_held` folds a set value into
`_owner_maps` as its one subtraction.

A column on the absence row rather than a `roster_releases` table (the shape the
retired "G. Jesus should be a free agent" backlog entry sketched): there is at most one
release per absence resolution, so a separate table would only add a join and the
possibility of an orphan.

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in models.py.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

_TABLES = ("injury_list", "international_list")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("released_player_id", UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table}_released_player_id_players",
            table,
            "players",
            ["released_player_id"],
            ["id"],
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(
            f"fk_{table}_released_player_id_players", table, type_="foreignkey"
        )
        op.drop_column(table, "released_player_id")
