from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import OperationType, ScheduleType, TokenType


class IntentDraft(BaseModel):
    operation: OperationType
    content: str = ""
    timezone: str
    schedule: ScheduleType | None = None
    run_at_local: str | None = None
    rrule: str | None = None
    confidence: float = 0.0
    needs_confirmation: bool = True
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
