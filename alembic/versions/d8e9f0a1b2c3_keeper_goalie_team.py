"""keeper_selections.team_id + keeper_seeds.team_id — keeping a goalie team

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-15

Under `goalie_team_mode = 'keeper'` a goalie team can be one of a manager's five
keepers, so both keeper tables gain a nullable `team_id` and lose NOT NULL on
`player_id`. Which column is set is what says whether the row keeps a person or a
club.

Both tables get the strict `num_nonnulls(player_id, team_id) = 1` CHECK — unlike
draft_picks, which needed a narrow one because its discovery rows are not provably
one-of. These two have only ever held player rows, so the strict form validates.

Uniqueness is partial indexes for the usual Postgres reason (NULLs count as distinct,
so a plain UNIQUE over a nullable column constrains nothing): a manager keeps at most
one goalie team per season, and a club is kept by at most one manager per season.

`keeper_seeds.team_id` is unused in the rule's first season — no club has any history
to correct yet — but `advance_season` needs somewhere to write the carried clock from
year two, and adding the column later would mean a second migration on the same table.

HAND-WRITTEN, and it must stay that way. `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in main's Base.metadata.
"""

import sqlalchemy as sa
from alembic import op

revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None

_TABLES = {
    "keeper_selections": {
        "fk": "fk_keeper_sel_team",
        "check": "ck_keeper_sel_player_or_team",
        "indexes": [
            ("uq_keeper_sel_one_team_per_manager",
             ["league_id", "manager_id", "season_year"]),
            ("uq_keeper_sel_team_once", ["league_id", "season_year", "team_id"]),
        ],
    },
    "keeper_seeds": {
        "fk": "fk_keeper_seed_team",
        "check": "ck_keeper_seed_player_or_team",
        "indexes": [("uq_keeper_seed_mgr_team", ["manager_id", "team_id"])],
    },
}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for table, spec in _TABLES.items():
        cols = {c["name"]: c for c in insp.get_columns(table)}
        if "team_id" not in cols:
            op.add_column(table, sa.Column("team_id", sa.UUID(), nullable=True))
            op.create_foreign_key(spec["fk"], table, "pl_teams", ["team_id"], ["id"])
        if "player_id" in cols and not cols["player_id"]["nullable"]:
            op.alter_column(table, "player_id", nullable=True)

        checks = {c["name"] for c in insp.get_check_constraints(table)}
        if spec["check"] not in checks:
            op.create_check_constraint(
                spec["check"], table, "num_nonnulls(player_id, team_id) = 1"
            )

        idxs = {i["name"] for i in insp.get_indexes(table)}
        for name, columns in spec["indexes"]:
            if name not in idxs:
                op.create_index(
                    name, table, columns, unique=True,
                    postgresql_where=sa.text("team_id IS NOT NULL"),
                )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    for table, spec in _TABLES.items():
        idxs = {i["name"] for i in insp.get_indexes(table)}
        for name, _columns in spec["indexes"]:
            if name in idxs:
                op.drop_index(name, table_name=table)
        checks = {c["name"] for c in insp.get_check_constraints(table)}
        if spec["check"] in checks:
            op.drop_constraint(spec["check"], table, type_="check")
        cols = {c["name"] for c in insp.get_columns(table)}
        if "team_id" in cols:
            # A club keeper has nowhere to go once the column is gone, and leaving
            # the row would violate the restored NOT NULL.
            op.execute(sa.text(f"DELETE FROM {table} WHERE team_id IS NOT NULL"))
            op.drop_constraint(spec["fk"], table, type_="foreignkey")
            op.drop_column(table, "team_id")
        op.alter_column(table, "player_id", nullable=False)
