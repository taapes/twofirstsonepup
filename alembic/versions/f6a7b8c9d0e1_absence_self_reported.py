"""injury_list / international_list: self_reported

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-24

Marks an absence entry a MANAGER placed for a player already off their roster --
drafted him, he got hurt, dropped him for a replacement before ever recording it
here -- distinct from an ordinary on-roster placement or an admin backfill.

Purely a display/audit marker. `place_on_il`/`place_on_intl` enforce the real
guards before this is ever set (the injured player must appear in this manager's
own roster history this season, and nobody else may currently hold him for real);
nothing downstream branches on the column itself except the new data_health
visibility check and a My Team badge. The retroactive effect on keeper-drop and
anti-tanking derivation was a deliberate decision (recompute naturally, same as
any other IL entry) and needed no schema of its own -- this column is not part of
that, it only says who entered the row and how.

No queue/status table (contrast `discovery_match_suggestions`): the placement
takes effect immediately on submission, same as every other self-service
IL/international action, so there is nothing pending to model.

Nullable-with-default rather than backfilled: every existing row predates this
distinction and `false` (an ordinary placement) is the correct answer for all of
them.

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in
this repo emits DROP TABLE for the six v2_* tables that exist in the live
database but not in models.py.
"""

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

_TABLES = ("injury_list", "international_list")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "self_reported", sa.Boolean(), nullable=False, server_default="false"
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "self_reported")
