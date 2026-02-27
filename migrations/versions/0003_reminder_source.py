"""add source column to reminders

Revision ID: 0003_reminder_source
Revises: 0002_voice_records
Create Date: 2026-02-27 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_reminder_source"
down_revision = "0002_voice_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reminders") as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(length=16), nullable=False, server_default="wechat"))
    op.execute("UPDATE reminders SET source = 'wechat' WHERE source IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("reminders") as batch_op:
        batch_op.drop_column("source")
