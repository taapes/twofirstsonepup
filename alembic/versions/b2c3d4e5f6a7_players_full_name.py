"""players.full_name — FPL's first + second name

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-18

`players.name` is FPL's `web_name`, the SHORT form: "Woltemade", "Gabriel", "Raya".
`sync._name(e)` keeps only that and discards `first_name`/`second_name`, which the
bootstrap payload has been carrying all along.

That gap is what makes matching a discovery pick hard. A discovery pick is recorded in
September as free text, because the player is not in the Premier League yet and has no
`players` row to point at — and a manager calls out "Nick Woltemade", not "Woltemade".
Comparing that label against web_name alone means the obvious matches don't look like
matches. With the full name stored, "Nick Woltemade" -> "Nick Woltemade" is exact, and
the web_name-only case still resolves as a token subset.

FPL-canonical data written by sync (`sync_players` phase 2, where the element dict is
already in scope), so this sits on the legal side of the two-truths boundary: it is a
fact FPL owns, refreshed on every full sync, never edited by the league.

Nullable with no backfill. The value arrives on the first full sync after deploy —
which happens daily, year-round, because `sync_players` is deliberately ungated by
season freeze. Until then the matcher simply falls back to web_name, which is what it
had before. There is nothing to backfill FROM: the names were discarded at sync time,
not stored somewhere else.

Display is unaffected — every read path still uses `name`.

HAND-WRITTEN, per the note in a1b2c3d4e5f6: `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in models.py.
"""

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("full_name", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "full_name")
