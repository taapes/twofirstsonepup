"""player code (stable cross-season id) + player_season snapshot

Revision ID: d7e8f9a0b1c2
Revises: a7b8c9d0e1f2
Create Date: 2026-08-10

FPL reassigns `players.fpl_id` every season, so upserting the bootstrap on that
key rewrites each row's identity in place (Aug 2026: 570 of 841 rows). `code` is
FPL's PERMANENT player id, so sync can match existing rows on it instead.

`players.fpl_id` therefore can no longer be UNIQUE NOT NULL. Two real cases break
that constraint once sync rekeys on `code`:
  - mid-sync reassignment: player A moves fpl_id 5->12 while player B still holds
    12 when A is processed first, inside the same transaction;
  - permanent collision: a player who left the PL keeps a stale fpl_id forever,
    and a later season reassigns that element id to somebody new.
So fpl_id becomes nullable and its unique index becomes PARTIAL (enforced only
when NOT NULL), letting many departed players sit at NULL simultaneously.

`player_season` freezes each season's player identity/stats so historical rosters
render the right names and clubs.

NOTE ON BRANCHES: this file is carried on both `main` and `v2/in-app-league` with
the SAME revision id and different ancestry (v2's copy Revises e5f6a7b8c9d0 via
its own a7b8c9d0e1f2). That mirrors a7b8c9d0e1f2 itself and is deliberate: there
is ONE shared database, so a distinct v2-only id would break `alembic current` /
`alembic upgrade head` from the v2 checkout. Keep the two copies byte-identical;
a future merge is then a clean no-op. Do NOT run `alembic merge`.

Every step is guarded with sa.inspect existence checks so the migration is
idempotent under either checkout.
"""

import sqlalchemy as sa
from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    player_cols = {c["name"] for c in insp.get_columns("players")}
    player_idxs = {ix["name"] for ix in insp.get_indexes("players")}

    # --- players.code (stable cross-season id) ---
    if "code" not in player_cols:
        op.add_column("players", sa.Column("code", sa.Integer(), nullable=True))
    if "ix_players_code" not in player_idxs:
        op.create_index("ix_players_code", "players", ["code"], unique=True)

    # --- fpl_id: nullable + partial unique ---
    if "ix_players_fpl_id" in player_idxs:
        op.drop_index("ix_players_fpl_id", table_name="players")
    op.alter_column("players", "fpl_id", existing_type=sa.Integer(), nullable=True)
    if "uq_players_fpl_id_live" not in player_idxs:
        op.create_index(
            "uq_players_fpl_id_live",
            "players",
            ["fpl_id"],
            unique=True,
            postgresql_where=sa.text("fpl_id IS NOT NULL"),
        )

    # --- player_season ---
    if "player_season" not in insp.get_table_names():
        op.create_table(
            "player_season",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("league_id", sa.UUID(), nullable=False),
            sa.Column("player_id", sa.UUID(), nullable=False),
            sa.Column("fpl_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("position", sa.String(), nullable=True),
            sa.Column("current_team", sa.String(), nullable=True),
            sa.Column("price", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("news", sa.Text(), nullable=True),
            sa.Column("total_points", sa.Integer(), nullable=True),
            sa.Column("goals_scored", sa.Integer(), nullable=True),
            sa.Column("assists", sa.Integer(), nullable=True),
            sa.Column("clean_sheets", sa.Integer(), nullable=True),
            sa.Column("bonus", sa.Integer(), nullable=True),
            sa.Column("minutes", sa.Integer(), nullable=True),
            sa.Column("form", sa.String(), nullable=True),
            sa.Column("points_per_game", sa.String(), nullable=True),
            sa.Column("ict_index", sa.String(), nullable=True),
            sa.Column("selected_by_percent", sa.String(), nullable=True),
            sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
            sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("league_id", "fpl_id", name="uq_player_season_league_fpl"),
        )
        op.create_index(
            "ix_player_season_league_id", "player_season", ["league_id"]
        )
        op.create_index(
            "ix_player_season_player_id", "player_season", ["player_id"]
        )
        op.create_index("ix_player_season_fpl_id", "player_season", ["fpl_id"])
        op.create_index(
            "ix_player_season_league_player",
            "player_season",
            ["league_id", "player_id"],
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "player_season" in insp.get_table_names():
        op.drop_table("player_season")
    player_idxs = {ix["name"] for ix in insp.get_indexes("players")}
    if "uq_players_fpl_id_live" in player_idxs:
        op.drop_index("uq_players_fpl_id_live", table_name="players")
    op.alter_column("players", "fpl_id", existing_type=sa.Integer(), nullable=False)
    op.create_index("ix_players_fpl_id", "players", ["fpl_id"], unique=True)
    if "ix_players_code" in player_idxs:
        op.drop_index("ix_players_code", table_name="players")
    if "code" in {c["name"] for c in insp.get_columns("players")}:
        op.drop_column("players", "code")
