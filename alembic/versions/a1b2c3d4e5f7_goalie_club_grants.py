"""goalie_club_grants

Revision ID: a1b2c3d4e5f7
Revises: e8f9a0b1c2d3
Create Date: 2026-09-08

HAND-WRITTEN, same reason as e8f9a0b1c2d3: autogenerate emits DROP TABLE for the six
v2_* tables live in the database but absent from models.py.

One table, zero backfill (the commissioner confirms the map through an admin form
before anything is written — nothing here writes from inference).

Records a 2026-only house rule the draft mechanics never captured: each manager holds
TWO Premier League clubs' worth of goalkeeper rights, not two individual keepers. FPL's
own roster is untouched (still 15 players, 2 GKP), so `goalie_team_mode` correctly stays
'off' and this is a new, independent fact rather than a fourth mode value. It plays the
role `DraftPick.team_id` plays for the older single-club `keeper` mode — the base fact
`_goalie_team_history` reads — except there is no draft pick to derive it from here.

No CHECK constraint on "at most 2 clubs per manager": validated in services, since a
transitional state mid-commissioner-correction is legitimate. The one thing the schema
does enforce is that a club has exactly one owner per season.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "a1b2c3d4e5f7"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goalie_club_grants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "league_id", UUID(as_uuid=True), sa.ForeignKey("leagues.id"), nullable=False
        ),
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column(
            "manager_id", UUID(as_uuid=True), sa.ForeignKey("managers.id"), nullable=False
        ),
        sa.Column(
            "team_id", UUID(as_uuid=True), sa.ForeignKey("pl_teams.id"), nullable=False
        ),
        sa.UniqueConstraint(
            "league_id", "season_year", "team_id", name="uq_goalie_grant_club_per_season"
        ),
    )
    op.create_index(
        "ix_goalie_club_grants_league_id", "goalie_club_grants", ["league_id"]
    )
    op.create_index(
        "ix_goalie_club_grants_season_year", "goalie_club_grants", ["season_year"]
    )
    op.create_index(
        "ix_goalie_club_grants_manager_id", "goalie_club_grants", ["manager_id"]
    )
    op.create_index(
        "ix_goalie_club_grants_team_id", "goalie_club_grants", ["team_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_goalie_club_grants_team_id", table_name="goalie_club_grants")
    op.drop_index("ix_goalie_club_grants_manager_id", table_name="goalie_club_grants")
    op.drop_index("ix_goalie_club_grants_season_year", table_name="goalie_club_grants")
    op.drop_index("ix_goalie_club_grants_league_id", table_name="goalie_club_grants")
    op.drop_table("goalie_club_grants")
