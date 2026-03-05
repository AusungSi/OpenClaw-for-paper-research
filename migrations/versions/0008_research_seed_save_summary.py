"""add research seed corpus and paper save/summary fields

Revision ID: 0008_research_seed_save_summary
Revises: 0007_research_exploration_worker
Create Date: 2026-03-05 10:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_research_seed_save_summary"
down_revision = "0007_research_exploration_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_seed_papers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("research_tasks.id"), nullable=False),
        sa.Column("paper_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_norm", sa.String(length=512), nullable=False),
        sa.Column("authors_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("venue", sa.String(length=255), nullable=True),
        sa.Column("doi", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "doi", name="uq_research_seed_paper_task_doi"),
        sa.UniqueConstraint("task_id", "title_norm", name="uq_research_seed_paper_task_title_norm"),
    )
    op.create_index("ix_research_seed_papers_task", "research_seed_papers", ["task_id", "id"], unique=False)

    with op.batch_alter_table("research_papers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("saved", sa.Boolean(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column("saved_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("saved_bib_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("key_points", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("key_points_source", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("key_points_status", sa.String(length=16), nullable=False, server_default="none"))
        batch_op.add_column(sa.Column("key_points_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("key_points_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("research_papers", schema=None) as batch_op:
        batch_op.drop_column("key_points_updated_at")
        batch_op.drop_column("key_points_error")
        batch_op.drop_column("key_points_status")
        batch_op.drop_column("key_points_source")
        batch_op.drop_column("key_points")
        batch_op.drop_column("saved_at")
        batch_op.drop_column("saved_bib_path")
        batch_op.drop_column("saved_path")
        batch_op.drop_column("saved")

    op.drop_index("ix_research_seed_papers_task", table_name="research_seed_papers")
    op.drop_table("research_seed_papers")
