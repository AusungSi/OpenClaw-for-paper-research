"""add voice_records table

Revision ID: 0002_voice_records
Revises: 0001_initial
Create Date: 2026-02-26 00:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_voice_records"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("wecom_msg_id", sa.String(length=128), nullable=False),
        sa.Column("media_id", sa.String(length=256), nullable=True),
        sa.Column("audio_format", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=11), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("wecom_msg_id", name="uq_voice_records_wecom_msg_id"),
    )


def downgrade() -> None:
    op.drop_table("voice_records")
