"""add research search cache table

Revision ID: 0005_research_search_cache
Revises: 0004_research_core
Create Date: 2026-03-04 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_research_search_cache"
down_revision = "0004_research_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_search_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("research_tasks.id"), nullable=False),
        sa.Column("direction_index", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("year_from", sa.Integer(), nullable=True),
        sa.Column("year_to", sa.Integer(), nullable=True),
        sa.Column("top_n", sa.Integer(), nullable=False),
        sa.Column("cache_key", sa.String(length=128), nullable=False),
        sa.Column("papers_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("papers_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cache_key", name="uq_research_search_cache_key"),
    )
    op.create_index(
        "ix_research_search_cache_lookup",
        "research_search_cache",
        ["task_id", "direction_index", "source", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_research_search_cache_lookup", table_name="research_search_cache")
    op.drop_table("research_search_cache")
