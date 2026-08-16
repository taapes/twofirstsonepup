"""unaccent extension — accent-insensitive player search

Revision ID: a1b2c3d4e5f6
Revises: e9f0a1b2c3d4
Create Date: 2026-08-15

A manager typing "Sesko" during the live draft found nothing: the search is
`Player.name ILIKE '%q%'`, and 'Sesko' does not match 'Šeško' — š is simply a
different character. Same for Ødegaard, Kadıoğlu, Milosavljević and anyone else FPL
spells with their real name. This is a draft-day usability bug, not a data bug.

`unaccent()` is used on BOTH sides of the comparison in `services.search_players`, so
one condition covers every combination: ASCII query -> accented name, accented query
-> accented name, and the unchanged ASCII-to-ASCII case.

Why the extension rather than a hand-written `translate()` map: the map has to
enumerate every character, and the first draft of one already missed ğ. Every future
signing with an unfamiliar letter would be a silent miss, in the one code path where
"no results" looks identical to "not in the pool". unaccent's rules ship with
Postgres and cover ø/ı/ğ, which a NFKD-only pass does not (NFKD has no decomposition
for ø — see the `_TRANSLIT` table in scripts/import_projections.py, which exists for
exactly this reason).

No column, no backfill, no sync change: the normalisation happens at query time, so
there is no derived value that can go stale when a player is added or renamed.

Deliberately NOT indexed. The pool is ~800 rows and the search already scans it; an
expression index on unaccent(name) needs the function marked IMMUTABLE, which it is
not by default, and wrapping it to fake that is a known footgun on restore. Revisit
only if the pool grows by an order of magnitude.

HAND-WRITTEN. `alembic revision --autogenerate` in this repo emits DROP TABLE for the
six v2_* tables that exist in the live database but not in models.py.
"""

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS so this is a no-op on a database that already has it (the local
    # test container may, once a developer has run the suite).
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")


def downgrade() -> None:
    # Dropping the extension would break search_players while the old code is still
    # deployed, and nothing else in the schema depends on it. Left in place on
    # purpose: an unused extension costs nothing.
    pass
