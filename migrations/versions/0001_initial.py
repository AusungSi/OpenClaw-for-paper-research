"""initial tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-02-26 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wecom_user_id", sa.String(length=128), nullable=False, unique=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wecom_msg_id", sa.String(length=128), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("msg_type", sa.String(length=32), nullable=False),
        sa.Column("raw_xml", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action_type", sa.String(length=6), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("schedule_type", sa.String(length=8), nullable=False),
        sa.Column("run_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rrule", sa.Text(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("next_run_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reminder_id", sa.Integer(), sa.ForeignKey("reminders.id"), nullable=False),
        sa.Column("planned_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delay_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=6), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "mobile_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=True),
        sa.Column("pair_code", sa.String(length=16), nullable=True, unique=True),
        sa.Column("pair_code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("refresh_tokens")
    op.drop_table("mobile_devices")
    op.drop_table("deliveries")
    op.drop_table("reminders")
    op.drop_table("pending_actions")
    op.drop_table("inbound_messages")
    op.drop_table("users")

