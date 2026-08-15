"""draft_picks.team_id + draft_queue.team_id — drafting a goalie team

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-08-15

A manager now spends one of their fourteen picks on a Premier League CLUB rather than
on two goalkeepers. Both tables that record "what was selected" gain a nullable
`team_id`; which column is set is what says whether a row is a player, a free-text
discovery pick, or a goalie team.

Two things about the constraints are deliberate.

First, the CHECK on draft_picks is narrow — "a team pick carries nothing else" — not
the `num_nonnulls(player_id, player_label, team_id) = 1` that is the honest invariant.
Existing discovery rows are not provably one-of, and a CHECK that fails to validate
aborts the migration on live data. Every existing row has team_id IS NULL and so
satisfies the narrow form trivially. draft_queue DOES get the strict two-column
version, because that table has only ever held one kind of row.

Second, both uniqueness rules are PARTIAL indexes. A plain UNIQUE over a nullable
column is useless in Postgres, which treats NULLs as distinct — every ordinary player
pick would count as its own distinct "club" and neither rule would bind. The two rules
are: one goalie team per manager per draft, and a club may be taken only once.

`draft_queue.player_id` drops NOT NULL so a club can be queued in the same ranked
list. Its old UNIQUE stays; it no longer constrains anything (same NULL semantics), so
the club half is the partial index beside it.

HAND-WRITTEN, and it must stay that way. `alembic revision --autogenerate` in this
repo emits DROP TABLE for the six v2_* tables that exist in the live database but not
in main's Base.metadata.
"""

import sqlalchemy as sa
from alembic import op

revision = "c7d8e9f0a1b2"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    # --- draft_picks ---
    cols = {c["name"] for c in insp.get_columns("draft_picks")}
    if "team_id" not in cols:
        op.add_column("draft_picks", sa.Column("team_id", sa.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_draftpick_team", "draft_picks", "pl_teams", ["team_id"], ["id"]
        )

    checks = {c["name"] for c in insp.get_check_constraints("draft_picks")}
    if "ck_draftpick_team_or_player" not in checks:
        op.create_check_constraint(
            "ck_draftpick_team_or_player",
            "draft_picks",
            "team_id IS NULL OR (player_id IS NULL AND player_label IS NULL)",
        )

    idxs = {i["name"] for i in insp.get_indexes("draft_picks")}
    if "uq_draftpick_one_team_per_manager" not in idxs:
        op.create_index(
            "uq_draftpick_one_team_per_manager",
            "draft_picks",
            ["league_id", "season_year", "draft_type", "manager_id"],
            unique=True,
            postgresql_where=sa.text("team_id IS NOT NULL"),
        )
    if "uq_draftpick_team_once" not in idxs:
        op.create_index(
            "uq_draftpick_team_once",
            "draft_picks",
            ["league_id", "season_year", "draft_type", "team_id"],
            unique=True,
            postgresql_where=sa.text("team_id IS NOT NULL"),
        )

    # --- draft_queue ---
    qcols = {c["name"]: c for c in insp.get_columns("draft_queue")}
    if "team_id" not in qcols:
        op.add_column("draft_queue", sa.Column("team_id", sa.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_draftqueue_team", "draft_queue", "pl_teams", ["team_id"], ["id"]
        )
    if "player_id" in qcols and not qcols["player_id"]["nullable"]:
        op.alter_column("draft_queue", "player_id", nullable=True)

    qchecks = {c["name"] for c in insp.get_check_constraints("draft_queue")}
    if "ck_draftqueue_player_or_team" not in qchecks:
        op.create_check_constraint(
            "ck_draftqueue_player_or_team",
            "draft_queue",
            "num_nonnulls(player_id, team_id) = 1",
        )

    qidxs = {i["name"] for i in insp.get_indexes("draft_queue")}
    if "uq_draftqueue_team_entry" not in qidxs:
        op.create_index(
            "uq_draftqueue_team_entry",
            "draft_queue",
            ["league_id", "season_year", "draft_type", "manager_id", "team_id"],
            unique=True,
            postgresql_where=sa.text("team_id IS NOT NULL"),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())

    qidxs = {i["name"] for i in insp.get_indexes("draft_queue")}
    if "uq_draftqueue_team_entry" in qidxs:
        op.drop_index("uq_draftqueue_team_entry", table_name="draft_queue")
    qchecks = {c["name"] for c in insp.get_check_constraints("draft_queue")}
    if "ck_draftqueue_player_or_team" in qchecks:
        op.drop_constraint("ck_draftqueue_player_or_team", "draft_queue", type_="check")
    qcols = {c["name"] for c in insp.get_columns("draft_queue")}
    if "team_id" in qcols:
        # A queued club has nowhere to go once the column is gone, and leaving the
        # row would violate the restored NOT NULL.
        op.execute(sa.text("DELETE FROM draft_queue WHERE team_id IS NOT NULL"))
        op.drop_constraint("fk_draftqueue_team", "draft_queue", type_="foreignkey")
        op.drop_column("draft_queue", "team_id")
    op.alter_column("draft_queue", "player_id", nullable=False)

    idxs = {i["name"] for i in insp.get_indexes("draft_picks")}
    for name in ("uq_draftpick_team_once", "uq_draftpick_one_team_per_manager"):
        if name in idxs:
            op.drop_index(name, table_name="draft_picks")
    checks = {c["name"] for c in insp.get_check_constraints("draft_picks")}
    if "ck_draftpick_team_or_player" in checks:
        op.drop_constraint("ck_draftpick_team_or_player", "draft_picks", type_="check")
    cols = {c["name"] for c in insp.get_columns("draft_picks")}
    if "team_id" in cols:
        op.drop_constraint("fk_draftpick_team", "draft_picks", type_="foreignkey")
        op.drop_column("draft_picks", "team_id")
