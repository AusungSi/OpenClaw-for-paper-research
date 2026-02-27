from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.enums import (
    DeliveryStatus,
    OperationType,
    PendingActionStatus,
    ReminderSource,
    ReminderStatus,
    ScheduleType,
    VoiceRecordStatus,
)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wecom_user_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="zh-CN", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    reminders: Mapped[list["Reminder"]] = relationship(back_populates="user")


class InboundMessage(Base):
    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wecom_msg_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_xml: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    action_type: Mapped[OperationType] = mapped_column(Enum(OperationType), nullable=False)
    draft_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[PendingActionStatus] = mapped_column(
        Enum(PendingActionStatus), default=PendingActionStatus.PENDING, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_type: Mapped[ScheduleType] = mapped_column(Enum(ScheduleType), nullable=False)
    source: Mapped[ReminderSource] = mapped_column(Enum(ReminderSource), nullable=False, default=ReminderSource.WECHAT)
    run_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rrule: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    next_run_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus), default=ReminderStatus.PENDING, nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped["User"] = relationship(back_populates="reminders")


class DeliveryLog(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), nullable=False)
    planned_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(Enum(DeliveryStatus), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class MobileDevice(Base):
    __tablename__ = "mobile_devices"
    __table_args__ = (UniqueConstraint("pair_code", name="uq_mobile_devices_pair_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pair_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pair_code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VoiceRecord(Base):
    __tablename__ = "voice_records"
    __table_args__ = (UniqueConstraint("wecom_msg_id", name="uq_voice_records_wecom_msg_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    wecom_msg_id: Mapped[str] = mapped_column(String(128), nullable=False)
    media_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    audio_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[VoiceRecordStatus] = mapped_column(Enum(VoiceRecordStatus), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
