"""fixtures: live-match-state columns

Revision ID: c9d0e1f2a3b4
Revises: f6a7b8c9d0e1
Create Date: 2026-08-24

The Scores page today can only say a real-life PL fixture is "finished" or not --
there was no way to tell "kicked off, in progress" from "not started yet," and no
live score. The classic FPL fixtures feed `sync_fixtures` already fetches carries
`started`, `finished_provisional`, `team_h_score`/`team_a_score`, and `minutes` on
every fixture; this migration just gives sync.py somewhere to put them. No new
HTTP call, no backfill (all nullable -- historical/pre-migration rows simply have
NULL here, and `rules.fixture_status` treats a NULL `started` as "not started").

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in
this repo emits DROP TABLE for the six v2_* tables that exist in the live
database but not in models.py.
"""

import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("started", sa.Boolean()),
    ("finished_provisional", sa.Boolean()),
    ("home_score", sa.Integer()),
    ("away_score", sa.Integer()),
    ("minutes", sa.Integer()),
)


def upgrade() -> None:
    for name, col_type in _COLUMNS:
        op.add_column("fixtures", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    for name, _col_type in reversed(_COLUMNS):
        op.drop_column("fixtures", name)
