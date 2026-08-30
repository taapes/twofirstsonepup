"""create the two indexes the models declared but no revision ever made

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-08-30

HAND-WRITTEN, per the note in a1b2c3d4e5f6.

`draft_queue.season_year` and `side_payouts.manager_id` both carry `index=True` in
models.py, and no migration has ever created them — so on a schema built from
migrations they exist in the model and not in the database. Harmless in itself (a
missing index is slow, not wrong; both tables are tiny), but every
`alembic revision --autogenerate` since has carried these two unrelated `create_index`
calls into whatever revision was being written, to be stripped out by hand. That is a
chore with a failure mode: eventually someone strips one and leaves the other, or
strips neither, and an unrelated revision quietly grows an index nobody reviewed.

Creating them is the cheap fix — after this, an autogenerate diff contains only what
the author actually changed, which is the property that makes reviewing one worth
anything.

`IF NOT EXISTS` rather than a plain create: production may already have these. They
predate the incident that made this repo build test schemas from migrations, so the
live database and a freshly-migrated one are not guaranteed to agree here, and a
revision that crashes on the one database that matters is worse than one that no-ops.

Verified 2026-08-30: with this applied, `alembic revision --autogenerate` against a
freshly-migrated schema detects nothing.
"""

from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_draft_queue_season_year", "draft_queue", "season_year"),
    ("ix_side_payouts_manager_id", "side_payouts", "manager_id"),
)


def upgrade() -> None:
    for name, table, column in _INDEXES:
        op.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ("{column}")')


def downgrade() -> None:
    for name, _table, _column in reversed(_INDEXES):
        op.execute(f'DROP INDEX IF EXISTS "{name}"')
