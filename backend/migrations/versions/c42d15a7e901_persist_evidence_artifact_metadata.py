"""persist evidence metadata and private artifact references

Revision ID: c42d15a7e901
Revises: b1f57ea4610b
Create Date: 2026-10-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c42d15a7e901"
down_revision: Union[str, None] = "b1f57ea4610b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=24), nullable=False),
        sa.Column("object_key", sa.String(length=1000), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("storage_backend IN ('local')", name="ck_artifacts_storage_backend"),
        sa.CheckConstraint("byte_size > 0", name="ck_artifacts_byte_size"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["qa_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_run_created", "artifacts", ["run_id", "created_at"])
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("observation_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["qa_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_run_created", "evidence", ["run_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_evidence_run_created", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("ix_artifacts_run_created", table_name="artifacts")
    op.drop_table("artifacts")
