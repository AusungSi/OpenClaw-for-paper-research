"""add research fulltext and graph tables

Revision ID: 0006_research_fulltext_graph
Revises: 0005_research_search_cache
Create Date: 2026-03-04 00:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_research_fulltext_graph"
down_revision = "0005_research_search_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("research_jobs", schema=None) as batch_op:
        batch_op.alter_column("job_type", existing_type=sa.String(length=6), type_=sa.String(length=24), nullable=False)

    op.create_table(
        "research_paper_fulltext",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("research_tasks.id"), nullable=False),
        sa.Column("paper_id", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("pdf_path", sa.Text(), nullable=True),
        sa.Column("text_path", sa.Text(), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("fail_reason", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "paper_id", name="uq_research_fulltext_task_paper"),
    )
    op.create_index(
        "ix_research_fulltext_task_status",
        "research_paper_fulltext",
        ["task_id", "status"],
        unique=False,
    )

    op.create_table(
        "research_citation_edges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("research_tasks.id"), nullable=False),
        sa.Column("src_paper_id", sa.String(length=128), nullable=False),
        sa.Column("dst_paper_id", sa.String(length=128), nullable=False),
        sa.Column("edge_type", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "src_paper_id", "dst_paper_id", "edge_type", name="uq_research_citation_edge"),
    )
    op.create_index(
        "ix_research_citation_edges_task",
        "research_citation_edges",
        ["task_id"],
        unique=False,
    )

    op.create_table(
        "research_graph_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("research_tasks.id"), nullable=False),
        sa.Column("direction_index", sa.Integer(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("nodes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("edges_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("stats_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_research_graph_snapshots_task",
        "research_graph_snapshots",
        ["task_id", "direction_index", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_research_graph_snapshots_task", table_name="research_graph_snapshots")
    op.drop_table("research_graph_snapshots")

    op.drop_index("ix_research_citation_edges_task", table_name="research_citation_edges")
    op.drop_table("research_citation_edges")

    op.drop_index("ix_research_fulltext_task_status", table_name="research_paper_fulltext")
    op.drop_table("research_paper_fulltext")

    with op.batch_alter_table("research_jobs", schema=None) as batch_op:
        batch_op.alter_column("job_type", existing_type=sa.String(length=24), type_=sa.String(length=6), nullable=False)
