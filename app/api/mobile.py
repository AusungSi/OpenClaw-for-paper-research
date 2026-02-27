from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.domain.schemas import (
    AsrTranscribeResponse,
    PairCodeRequest,
    RefreshRequest,
    ReminderCreateRequest,
    ReminderListResponse,
    ReminderResponse,
    ReminderUpdateRequest,
    TokenResponse,
)
from app.infra.db import get_db
from app.infra.repos import MobileRepo
from app.services.asr_service import AsrError, AsrService, AsrTimeoutError, AsrValidationError
from app.services.mobile_auth_service import MobileAuthService
from app.services.reminder_service import ReminderService


router = APIRouter(prefix="/api/v1")


def get_auth_service(request: Request) -> MobileAuthService:
    return request.app.state.mobile_auth_service


def get_reminder_service(request: Request) -> ReminderService:
    return request.app.state.reminder_service


def get_asr_service(request: Request) -> AsrService:
    return request.app.state.asr_service


def get_current_user_id(
    authorization: str = Header(default=""),
    auth_service: MobileAuthService = Depends(get_auth_service),
) -> int:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.replace("Bearer ", "", 1).strip()
    try:
        payload = auth_service.verify_access_token(token)
        return int(payload.sub)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc


@router.post("/auth/pair", response_model=TokenResponse)
def pair_mobile(
    payload: PairCodeRequest,
    db: Session = Depends(get_db),
    auth_service: MobileAuthService = Depends(get_auth_service),
) -> TokenResponse:
    device = MobileRepo(db).claim_pair_code(payload.pair_code, payload.device_id)
    if not device:
        raise HTTPException(status_code=400, detail="invalid or expired pair code")
    return auth_service.issue_tokens(db, user_id=device.user_id, device_id=payload.device_id)


@router.post("/auth/refresh", response_model=TokenResponse)
def refresh_token(
    payload: RefreshRequest,
    db: Session = Depends(get_db),
    auth_service: MobileAuthService = Depends(get_auth_service),
) -> TokenResponse:
    try:
        return auth_service.refresh_tokens(db, payload.refresh_token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.get("/reminders", response_model=ReminderListResponse)
def list_reminders(
    status: str | None = Query(default=None),
    from_utc: datetime | None = Query(default=None),
    to_utc: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    reminder_service: ReminderService = Depends(get_reminder_service),
) -> ReminderListResponse:
    items, total = reminder_service.list_for_user(db, user_id, status, page, size, from_utc, to_utc)
    return ReminderListResponse(items=items, total=total, page=page, size=size)


@router.post("/reminders", response_model=ReminderResponse)
def create_reminder(
    payload: ReminderCreateRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    reminder_service: ReminderService = Depends(get_reminder_service),
) -> ReminderResponse:
    row = reminder_service.create_from_mobile(db, user_id, payload)
    return ReminderResponse(
        id=row.id,
        content=row.content,
        schedule_type=row.schedule_type,
        source=row.source,
        run_at_utc=row.run_at_utc,
        rrule=row.rrule,
        timezone=row.timezone,
        next_run_utc=row.next_run_utc,
        status=row.status.value,
    )


@router.patch("/reminders/{reminder_id}", response_model=ReminderResponse)
def update_reminder(
    reminder_id: int,
    payload: ReminderUpdateRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    reminder_service: ReminderService = Depends(get_reminder_service),
) -> ReminderResponse:
    try:
        row = reminder_service.update_from_mobile(db, user_id, reminder_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ReminderResponse(
        id=row.id,
        content=row.content,
        schedule_type=row.schedule_type,
        source=row.source,
        run_at_utc=row.run_at_utc,
        rrule=row.rrule,
        timezone=row.timezone,
        next_run_utc=row.next_run_utc,
        status=row.status.value,
    )


@router.delete("/reminders/{reminder_id}")
def delete_reminder(
    reminder_id: int,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    reminder_service: ReminderService = Depends(get_reminder_service),
) -> dict[str, bool]:
    deleted = reminder_service.delete_for_user(db, user_id, reminder_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="reminder not found")
    return {"ok": True}


@router.get("/calendar")
def calendar_view(
    from_utc: datetime | None = Query(default=None),
    to_utc: datetime | None = Query(default=None),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    reminder_service: ReminderService = Depends(get_reminder_service),
) -> dict[str, list[dict[str, str]]]:
    items, _ = reminder_service.list_for_user(
        db,
        user_id,
        status=None,
        page=1,
        size=500,
        from_utc=from_utc,
        to_utc=to_utc,
    )
    grouped: dict[str, list[dict[str, str]]] = {}
    for item in items:
        day = item.next_run_utc.date().isoformat() if item.next_run_utc else "unscheduled"
        grouped.setdefault(day, []).append({"id": str(item.id), "content": item.content, "status": item.status})
    return grouped


@router.post("/asr/transcribe", response_model=AsrTranscribeResponse)
async def asr_transcribe(
    file: UploadFile = File(...),
    language_hint: str | None = Query(default=None),
    _user_id: int = Depends(get_current_user_id),
    asr_service: AsrService = Depends(get_asr_service),
) -> AsrTranscribeResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="audio file is empty")
    if not (file.content_type or "").startswith("audio/"):
        raise HTTPException(status_code=400, detail="file must be audio/*")
    try:
        result = asr_service.transcribe_bytes(
            data,
            filename=file.filename,
            mime_type=file.content_type,
            language_hint=language_hint,
        )
    except AsrValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AsrTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except AsrError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AsrTranscribeResponse(
        text=result.text,
        language=result.language,
        provider=result.provider,
        model=result.model,
        request_id=result.request_id,
        latency_ms=result.latency_ms,
        used_fallback=result.used_fallback,
    )
