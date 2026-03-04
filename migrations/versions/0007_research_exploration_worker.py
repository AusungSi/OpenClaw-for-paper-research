"""add research exploration rounds and worker queue columns

Revision ID: 0007_research_exploration_worker
Revises: 0006_research_fulltext_graph
Create Date: 2026-03-04 13:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_research_exploration_worker"
down_revision = "0006_research_fulltext_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("research_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("queue_name", sa.String(length=32), nullable=False, server_default="research"))
        batch_op.add_column(sa.Column("worker_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_research_jobs_queue_sched", "research_jobs", ["queue_name", "status", "scheduled_at"], unique=False)
    op.create_index("ix_research_jobs_lease", "research_jobs", ["status", "lease_until"], unique=False)

    with op.batch_alter_table("research_paper_fulltext", schema=None) as batch_op:
        batch_op.add_column(sa.Column("parser", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("quality_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("sections_json", sa.Text(), nullable=False, server_default="{}"))

    op.create_table(
        "research_rounds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("research_tasks.id"), nullable=False),
        sa.Column("direction_index", sa.Integer(), nullable=False),
        sa.Column("parent_round_id", sa.Integer(), sa.ForeignKey("research_rounds.id"), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("feedback_text", sa.Text(), nullable=True),
        sa.Column("query_terms_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_research_rounds_task_dir", "research_rounds", ["task_id", "direction_index", "created_at"], unique=False)
    op.create_index("ix_research_rounds_parent", "research_rounds", ["parent_round_id"], unique=False)

    with op.batch_alter_table("research_graph_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("round_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("view_type", sa.String(length=8), nullable=False, server_default="citation"))
    op.create_foreign_key(
        "fk_research_graph_snapshots_round_id",
        "research_graph_snapshots",
        "research_rounds",
        ["round_id"],
        ["id"],
    )

    op.create_table(
        "research_round_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("round_id", sa.Integer(), sa.ForeignKey("research_rounds.id"), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("queries_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("round_id", "candidate_index", name="uq_research_round_candidate_idx"),
    )

    op.create_table(
        "research_round_papers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("round_id", sa.Integer(), sa.ForeignKey("research_rounds.id"), nullable=False),
        sa.Column("paper_id", sa.Integer(), sa.ForeignKey("research_papers.id"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="seed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("round_id", "paper_id", "role", name="uq_research_round_paper_role"),
    )
    op.create_index("ix_research_round_papers_round", "research_round_papers", ["round_id", "rank"], unique=False)

    op.create_table(
        "research_citation_fetch_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("research_tasks.id"), nullable=False),
        sa.Column("paper_key", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "paper_key", "source", name="uq_research_citation_fetch_cache"),
    )
    op.create_index(
        "ix_research_citation_fetch_cache_lookup",
        "research_citation_fetch_cache",
        ["task_id", "source", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_research_citation_fetch_cache_lookup", table_name="research_citation_fetch_cache")
    op.drop_table("research_citation_fetch_cache")

    op.drop_index("ix_research_round_papers_round", table_name="research_round_papers")
    op.drop_table("research_round_papers")
    op.drop_table("research_round_candidates")

    op.drop_index("ix_research_rounds_parent", table_name="research_rounds")
    op.drop_index("ix_research_rounds_task_dir", table_name="research_rounds")
    op.drop_table("research_rounds")

    op.drop_constraint("fk_research_graph_snapshots_round_id", "research_graph_snapshots", type_="foreignkey")
    with op.batch_alter_table("research_graph_snapshots", schema=None) as batch_op:
        batch_op.drop_column("view_type")
        batch_op.drop_column("round_id")

    with op.batch_alter_table("research_paper_fulltext", schema=None) as batch_op:
        batch_op.drop_column("sections_json")
        batch_op.drop_column("quality_score")
        batch_op.drop_column("parser")

    op.drop_index("ix_research_jobs_lease", table_name="research_jobs")
    op.drop_index("ix_research_jobs_queue_sched", table_name="research_jobs")
    with op.batch_alter_table("research_jobs", schema=None) as batch_op:
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("lease_until")
        batch_op.drop_column("worker_id")
        batch_op.drop_column("queue_name")
