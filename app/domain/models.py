from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
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
    ResearchGraphBuildStatus,
    ResearchJobStatus,
    ResearchJobType,
    ResearchPaperFulltextStatus,
    ResearchTaskStatus,
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
    research_tasks: Mapped[list["ResearchTask"]] = relationship(back_populates="user")


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


class ResearchTask(Base):
    __tablename__ = "research_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    constraints_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[ResearchTaskStatus] = mapped_column(
        Enum(ResearchTaskStatus), default=ResearchTaskStatus.CREATED, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped["User"] = relationship(back_populates="research_tasks")
    directions: Mapped[list["ResearchDirection"]] = relationship(back_populates="task")
    jobs: Mapped[list["ResearchJob"]] = relationship(back_populates="task")


class ResearchDirection(Base):
    __tablename__ = "research_directions"
    __table_args__ = (UniqueConstraint("task_id", "direction_index", name="uq_research_direction_task_idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), nullable=False)
    direction_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    queries_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    exclude_terms_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    papers_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    task: Mapped["ResearchTask"] = relationship(back_populates="directions")
    papers: Mapped[list["ResearchPaper"]] = relationship(back_populates="direction")


class ResearchPaper(Base):
    __tablename__ = "research_papers"
    __table_args__ = (
        UniqueConstraint("task_id", "doi", name="uq_research_paper_task_doi"),
        UniqueConstraint("task_id", "title_norm", name="uq_research_paper_task_title_norm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), nullable=False)
    direction_id: Mapped[int] = mapped_column(ForeignKey("research_directions.id"), nullable=False)
    paper_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(String(512), nullable=False)
    authors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    venue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    method_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    direction: Mapped["ResearchDirection"] = relationship(back_populates="papers")


class ResearchJob(Base):
    __tablename__ = "research_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), nullable=False)
    job_type: Mapped[ResearchJobType] = mapped_column(Enum(ResearchJobType), nullable=False)
    status: Mapped[ResearchJobStatus] = mapped_column(
        Enum(ResearchJobStatus), default=ResearchJobStatus.QUEUED, nullable=False
    )
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    task: Mapped["ResearchTask"] = relationship(back_populates="jobs")


class ResearchSession(Base):
    __tablename__ = "research_sessions"
    __table_args__ = (UniqueConstraint("user_id", name="uq_research_sessions_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    active_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_direction_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    page_size: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchSearchCache(Base):
    __tablename__ = "research_search_cache"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_research_search_cache_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), nullable=False)
    direction_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    year_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top_n: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_key: Mapped[str] = mapped_column(String(128), nullable=False)
    papers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    papers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchPaperFulltext(Base):
    __tablename__ = "research_paper_fulltext"
    __table_args__ = (
        UniqueConstraint("task_id", "paper_id", name="uq_research_fulltext_task_paper"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), nullable=False)
    paper_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[ResearchPaperFulltextStatus] = mapped_column(
        Enum(ResearchPaperFulltextStatus), nullable=False, default=ResearchPaperFulltextStatus.NOT_STARTED
    )
    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchCitationEdge(Base):
    __tablename__ = "research_citation_edges"
    __table_args__ = (
        UniqueConstraint("task_id", "src_paper_id", "dst_paper_id", "edge_type", name="uq_research_citation_edge"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), nullable=False)
    src_paper_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dst_paper_id: Mapped[str] = mapped_column(String(128), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchGraphSnapshot(Base):
    __tablename__ = "research_graph_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), nullable=False)
    direction_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    nodes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    edges_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    stats_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[ResearchGraphBuildStatus] = mapped_column(
        Enum(ResearchGraphBuildStatus), nullable=False, default=ResearchGraphBuildStatus.QUEUED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
