"""add target authorization audit records

Revision ID: a44f9c6d8e21
Revises: c42d15a7e901
Create Date: 2026-10-21 15:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a44f9c6d8e21"
down_revision: Union[str, None] = "c42d15a7e901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "target_authorization_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("target_environment_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("actor_id", sa.String(length=120), nullable=False),
        sa.Column("target_url_snapshot", sa.String(length=2048), nullable=False),
        sa.Column("allowed_hosts_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('authorized', 'revoked')", name="ck_target_authorization_event_action"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_environment_id"], ["target_environments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_target_authorization_events_target",
        "target_authorization_events",
        ["target_environment_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_target_authorization_events_target", table_name="target_authorization_events")
    op.drop_table("target_authorization_events")
