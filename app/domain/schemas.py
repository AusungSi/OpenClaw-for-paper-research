from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import OperationType, ReminderSource, ScheduleType, TokenType


class IntentDraft(BaseModel):
    operation: OperationType
    content: str = ""
    timezone: str
    source: ReminderSource = ReminderSource.WECHAT
    schedule: ScheduleType | None = None
    run_at_local: str | None = None
    rrule: str | None = None
    confidence: float = 0.0
    needs_confirmation: bool = True
    clarification_question: str | None = None


class IntentLite(BaseModel):
    operation: OperationType
    content: str = ""
    when_text: str | None = None
    confidence: float = 0.0
    clarification_question: str | None = None


class PairCodeRequest(BaseModel):
    pair_code: str = Field(min_length=4, max_length=16)
    device_id: str = Field(min_length=3, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sub: str
    token_type: TokenType
    exp: int
    iat: int
    device_id: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class ReminderCreateRequest(BaseModel):
    content: str
    timezone: str
    schedule_type: ScheduleType
    run_at_local: str | None = None
    rrule: str | None = None


class ReminderUpdateRequest(BaseModel):
    content: str | None = None
    timezone: str | None = None
    run_at_local: str | None = None
    rrule: str | None = None


class ReminderResponse(BaseModel):
    id: int
    content: str
    schedule_type: ScheduleType
    source: ReminderSource
    run_at_utc: datetime | None
    rrule: str | None
    timezone: str
    next_run_utc: datetime | None
    status: str


class ReminderListResponse(BaseModel):
    items: list[ReminderResponse]
    total: int
    page: int
    size: int


class AsrTranscribeResponse(BaseModel):
    text: str
    language: str
    provider: str
    model: str
    request_id: str | None = None
    latency_ms: int
    used_fallback: bool = False


class HealthResponse(BaseModel):
    db_ok: bool
    ollama_ok: bool
    scheduler_ok: bool
    wecom_send_ok: bool
    wecom_last_error: str | None = None
    webhook_dedup_ok: bool
    asr_ok: bool = True
    asr_provider: str | None = None
    asr_last_error: str | None = None
    nlg_last_error: str | None = None
    intent_provider_ok: bool = True
    intent_provider_name: str | None = None
    intent_last_error: str | None = None
    reply_provider_ok: bool = True
    reply_provider_name: str | None = None
    reply_last_error: str | None = None
    asr_provider_ok: bool = True
    asr_provider_name: str | None = None
    openclaw_http_ok: int = 0
    openclaw_http_fail: int = 0
    openclaw_cli_fallback_count: int = 0
    openclaw_latency_ms: int = 0
    research_jobs_total: int = 0
    research_job_latency_ms: int = 0
    research_cache_hit: int = 0
    research_cache_miss: int = 0
    research_export_success: int = 0
    research_export_fail: int = 0
    research_search_source_status: dict[str, int] = Field(default_factory=dict)


class CapabilityItem(BaseModel):
    enabled: bool
    provider: str
    model: str | None = None
    mode: str | None = None
    fallback_enabled: bool = True


class CapabilitiesResponse(BaseModel):
    intent: CapabilityItem
    reply: CapabilityItem
    asr: CapabilityItem


class AdminOverviewResponse(BaseModel):
    server_time: datetime
    app_env: str
    db_ok: bool
    ollama_ok: bool
    scheduler_ok: bool
    wecom_send_ok: bool
    wecom_last_error: str | None = None
    webhook_dedup_ok: bool
    intent_provider: str
    reply_provider: str
    asr_provider: str
    dedup_duplicates: int
    dedup_failures: int
    reminder_counts: dict[str, int]
    delivery_counts_24h: dict[str, int]


class AdminDispatchResponse(BaseModel):
    processed_count: int
    duration_ms: int
    executed_at: datetime
    error: str | None = None


class ReminderSnoozeRequest(BaseModel):
    minutes: int


class AdminActionResponse(BaseModel):
    ok: bool
    reminder_id: int
    previous_status: str | None = None
    current_status: str | None = None
    previous_last_error: str | None = None
    previous_next_run_utc: datetime | None = None
    next_run_utc: datetime | None = None
    no_change: bool = False
    message: str | None = None


class AdminUserListItem(BaseModel):
    id: int
    wecom_user_id: str
    timezone: str
    locale: str
    created_at: datetime
    updated_at: datetime
    pending_reminders: int
    failed_deliveries_24h: int
    last_inbound_at: datetime | None = None
    last_voice_status: str | None = None


class AdminUserListResponse(BaseModel):
    items: list[AdminUserListItem]
    total: int
    page: int
    size: int


class AdminUserProfile(BaseModel):
    id: int
    wecom_user_id: str
    timezone: str
    locale: str
    created_at: datetime
    updated_at: datetime


class AdminUserDeviceItem(BaseModel):
    id: int
    user_id: int
    device_id: str | None = None
    pair_code: str | None = None
    pair_code_expires_at: datetime | None = None
    token_version: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AdminUserDeviceListResponse(BaseModel):
    items: list[AdminUserDeviceItem]


class AdminUserAuditOverviewResponse(BaseModel):
    user: AdminUserProfile
    reminder_counts: dict[str, int]
    pending_action_counts: dict[str, int]
    delivery_counts_7d: dict[str, int]
    inbound_counts_7d: dict[str, int]
    voice_counts_7d: dict[str, int]
    devices: list[AdminUserDeviceItem]
    token_stats: dict[str, int]


class AdminReminderItem(BaseModel):
    id: int
    user_id: int
    content: str
    schedule_type: ScheduleType
    source: ReminderSource
    run_at_utc: datetime | None = None
    rrule: str | None = None
    timezone: str
    next_run_utc: datetime | None = None
    status: str
    last_error: str | None = None
    updated_at: datetime


class AdminReminderListResponse(BaseModel):
    items: list[AdminReminderItem]
    total: int
    page: int
    size: int


class AdminUserPendingActionItem(BaseModel):
    id: int
    action_id: str
    user_id: int
    action_type: str
    draft_json: str
    source_message_id: str
    status: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class AdminUserPendingActionListResponse(BaseModel):
    items: list[AdminUserPendingActionItem]
    total: int
    page: int
    size: int


class AdminInboundMessageItem(BaseModel):
    id: int
    user_id: int
    wecom_msg_id: str
    msg_type: str
    normalized_text: str
    raw_xml: str
    created_at: datetime


class AdminInboundMessageListResponse(BaseModel):
    items: list[AdminInboundMessageItem]
    total: int
    page: int
    size: int


class AdminUserVoiceRecordItem(BaseModel):
    id: int
    user_id: int
    wecom_msg_id: str
    media_id: str | None = None
    audio_format: str | None = None
    source: str
    transcript_text: str | None = None
    status: str
    error: str | None = None
    latency_ms: int | None = None
    created_at: datetime
    updated_at: datetime


class AdminUserVoiceRecordListResponse(BaseModel):
    items: list[AdminUserVoiceRecordItem]
    total: int
    page: int
    size: int


class AdminUserDeliveryItem(BaseModel):
    id: int
    reminder_id: int
    planned_at_utc: datetime
    sent_at_utc: datetime
    delay_seconds: int
    status: str
    error: str | None = None


class AdminUserDeliveryListResponse(BaseModel):
    items: list[AdminUserDeliveryItem]
    total: int
    page: int
    size: int


class AdminChatReplyItem(BaseModel):
    text: str
    created_at: datetime


class AdminChatSendRequest(BaseModel):
    user_id: int
    text: str
    session_id: str | None = None


class AdminChatSendResponse(BaseModel):
    session_id: str
    msg_id: str
    user_id: int
    wecom_user_id: str
    input_text: str
    source: ReminderSource
    replies: list[AdminChatReplyItem]
    pipeline_status: str
    errors: dict[str, str | None] = Field(default_factory=dict)


class ResearchTaskCreateRequest(BaseModel):
    topic: str
    year_from: int | None = None
    year_to: int | None = None
    top_n: int | None = None
    sources: list[str] | None = None


class ResearchTaskSearchRequest(BaseModel):
    direction_index: int
    top_n: int | None = None
    force_refresh: bool = False


class ResearchTaskPlanResponse(BaseModel):
    task_id: str
    status: str
    queued: bool = True


class ResearchTaskSearchEnqueueResponse(BaseModel):
    task_id: str
    status: str
    direction_index: int
    force_refresh: bool = False


class ResearchFulltextBuildResponse(BaseModel):
    task_id: str
    status: str
    queued: bool = True


class ResearchFulltextItem(BaseModel):
    paper_id: str
    status: str
    source_url: str | None = None
    pdf_path: str | None = None
    text_path: str | None = None
    text_chars: int = 0
    fail_reason: str | None = None
    fetched_at: datetime | None = None
    parsed_at: datetime | None = None


class ResearchFulltextStatusResponse(BaseModel):
    task_id: str
    summary: dict[str, int] = Field(default_factory=dict)
    items: list[ResearchFulltextItem] = Field(default_factory=list)


class ResearchGraphBuildRequest(BaseModel):
    direction_index: int | None = None


class ResearchGraphBuildResponse(BaseModel):
    task_id: str
    status: str
    queued: bool = True
    direction_index: int | None = None


class ResearchGraphNode(BaseModel):
    id: str
    type: str
    label: str
    year: int | None = None
    source: str | None = None
    direction_index: int | None = None
    score: float | None = None
    fulltext_status: str | None = None


class ResearchGraphEdge(BaseModel):
    source: str
    target: str
    type: str
    weight: float = 1.0


class ResearchGraphResponse(BaseModel):
    task_id: str
    direction_index: int | None = None
    depth: int = 1
    status: str
    nodes: list[ResearchGraphNode] = Field(default_factory=list)
    edges: list[ResearchGraphEdge] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)


class ResearchDirectionItem(BaseModel):
    direction_index: int
    name: str
    queries: list[str]
    exclude_terms: list[str] = Field(default_factory=list)
    papers_count: int = 0


class ResearchPaperItem(BaseModel):
    index: int
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    method_summary: str = ""
    source: str


class ResearchTaskResponse(BaseModel):
    task_id: str
    topic: str
    status: str
    constraints: dict = Field(default_factory=dict)
    directions: list[ResearchDirectionItem] = Field(default_factory=list)
    papers_total: int = 0
    last_job_type: str | None = None
    last_job_status: str | None = None
    last_failure_reason: str | None = None
    last_attempts: int = 0
    next_retry_at: datetime | None = None
    fulltext_stats: dict[str, int] = Field(default_factory=dict)
    graph_stats: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ResearchTaskListResponse(BaseModel):
    items: list[ResearchTaskResponse]
    total: int


class ResearchSearchResponse(BaseModel):
    task_id: str
    direction_index: int
    page: int
    page_size: int
    total: int
    items: list[ResearchPaperItem] = Field(default_factory=list)


class ResearchExportResponse(BaseModel):
    task_id: str
    format: str
    path: str
