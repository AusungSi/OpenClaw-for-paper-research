from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezone import now_utc
from app.domain.enums import ReminderStatus
from app.domain.schemas import (
    AdminActionResponse,
    AdminDispatchResponse,
    AdminInboundMessageItem,
    AdminInboundMessageListResponse,
    AdminOverviewResponse,
    AdminReminderItem,
    AdminReminderListResponse,
    AdminUserAuditOverviewResponse,
    AdminUserDeviceItem,
    AdminUserDeviceListResponse,
    AdminUserDeliveryItem,
    AdminUserDeliveryListResponse,
    AdminUserListItem,
    AdminUserListResponse,
    AdminUserPendingActionItem,
    AdminUserPendingActionListResponse,
    AdminUserProfile,
    AdminUserVoiceRecordItem,
    AdminUserVoiceRecordListResponse,
)
from app.infra.admin_repo import AdminRepo


class AdminService:
    SNOOZE_ALLOWED = {5, 10, 15, 30, 60, 120, 1440}

    def __init__(self, db: Session):
        self.db = db
        self.repo = AdminRepo(db)
        self.settings = get_settings()

    def overview(
        self,
        *,
        scheduler_started: bool,
        ollama_ok: bool,
        wecom_send_ok: bool,
        wecom_last_error: str | None,
        webhook_dedup_ok: bool,
        intent_provider_name: str,
        reply_provider_name: str,
        asr_provider_name: str,
        dedup_duplicates: int,
        dedup_failures: int,
    ) -> AdminOverviewResponse:
        db_ok = False
        try:
            self.db.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False

        return AdminOverviewResponse(
            server_time=datetime.now(timezone.utc),
            app_env=self.settings.app_env,
            db_ok=db_ok,
            ollama_ok=ollama_ok,
            scheduler_ok=scheduler_started,
            wecom_send_ok=wecom_send_ok,
            wecom_last_error=wecom_last_error,
            webhook_dedup_ok=webhook_dedup_ok,
            intent_provider=intent_provider_name,
            reply_provider=reply_provider_name,
            asr_provider=asr_provider_name,
            dedup_duplicates=dedup_duplicates,
            dedup_failures=dedup_failures,
            reminder_counts=self.repo.global_reminder_counts(),
            delivery_counts_24h=self.repo.global_delivery_counts_24h(),
        )

    def list_users(
        self,
        *,
        q: str | None,
        timezone_name: str | None,
        page: int,
        size: int,
        sort: str,
    ) -> AdminUserListResponse:
        items, total = self.repo.list_users(
            q=q,
            timezone_name=timezone_name,
            page=page,
            size=size,
            sort=sort,
        )
        return AdminUserListResponse(
            items=[AdminUserListItem(**item) for item in items],
            total=total,
            page=page,
            size=size,
        )

    def get_user_overview(self, user_id: int) -> AdminUserAuditOverviewResponse | None:
        data = self.repo.get_user_overview(user_id)
        if not data:
            return None
        user = data["user"]
        devices = [
            AdminUserDeviceItem(
                id=row.id,
                user_id=row.user_id,
                device_id=row.device_id,
                pair_code=row.pair_code,
                pair_code_expires_at=row.pair_code_expires_at,
                token_version=row.token_version,
                is_active=row.is_active,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in data["devices"]
        ]
        return AdminUserAuditOverviewResponse(
            user=AdminUserProfile(
                id=user.id,
                wecom_user_id=user.wecom_user_id,
                timezone=user.timezone,
                locale=user.locale,
                created_at=user.created_at,
                updated_at=user.updated_at,
            ),
            reminder_counts=data["reminder_counts"],
            pending_action_counts=data["pending_action_counts"],
            delivery_counts_7d=data["delivery_counts_7d"],
            inbound_counts_7d=data["inbound_counts_7d"],
            voice_counts_7d=data["voice_counts_7d"],
            devices=devices,
            token_stats=data["token_stats"],
        )

    def list_user_reminders(
        self,
        *,
        user_id: int,
        status: str | None,
        source: str | None,
        q: str | None,
        from_utc: datetime | None,
        to_utc: datetime | None,
        page: int,
        size: int,
    ) -> AdminReminderListResponse:
        items, total = self.repo.list_user_reminders(
            user_id=user_id,
            status=status,
            source=source,
            q=q,
            from_utc=from_utc,
            to_utc=to_utc,
            page=page,
            size=size,
        )
        return AdminReminderListResponse(
            items=[
                AdminReminderItem(
                    id=row.id,
                    user_id=row.user_id,
                    content=row.content,
                    schedule_type=row.schedule_type,
                    source=row.source,
                    run_at_utc=row.run_at_utc,
                    rrule=row.rrule,
                    timezone=row.timezone,
                    next_run_utc=row.next_run_utc,
                    status=row.status.value,
                    last_error=row.last_error,
                    updated_at=row.updated_at,
                )
                for row in items
            ],
            total=total,
            page=page,
            size=size,
        )

    def list_user_pending_actions(
        self,
        *,
        user_id: int,
        status: str | None,
        page: int,
        size: int,
    ) -> AdminUserPendingActionListResponse:
        items, total = self.repo.list_user_pending_actions(user_id=user_id, status=status, page=page, size=size)
        return AdminUserPendingActionListResponse(
            items=[
                AdminUserPendingActionItem(
                    id=row.id,
                    action_id=row.action_id,
                    user_id=row.user_id,
                    action_type=row.action_type.value,
                    draft_json=row.draft_json,
                    source_message_id=row.source_message_id,
                    status=row.status.value,
                    expires_at=row.expires_at,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in items
            ],
            total=total,
            page=page,
            size=size,
        )

    def list_user_inbound_messages(
        self,
        *,
        user_id: int,
        msg_type: str | None,
        page: int,
        size: int,
    ) -> AdminInboundMessageListResponse:
        items, total = self.repo.list_user_inbound_messages(user_id=user_id, msg_type=msg_type, page=page, size=size)
        return AdminInboundMessageListResponse(
            items=[
                AdminInboundMessageItem(
                    id=row.id,
                    user_id=row.user_id,
                    wecom_msg_id=row.wecom_msg_id,
                    msg_type=row.msg_type,
                    normalized_text=row.normalized_text,
                    raw_xml=row.raw_xml,
                    created_at=row.created_at,
                )
                for row in items
            ],
            total=total,
            page=page,
            size=size,
        )

    def list_user_voice_records(
        self,
        *,
        user_id: int,
        status: str | None,
        source: str | None,
        page: int,
        size: int,
    ) -> AdminUserVoiceRecordListResponse:
        items, total = self.repo.list_user_voice_records(
            user_id=user_id,
            status=status,
            source=source,
            page=page,
            size=size,
        )
        return AdminUserVoiceRecordListResponse(
            items=[
                AdminUserVoiceRecordItem(
                    id=row.id,
                    user_id=row.user_id,
                    wecom_msg_id=row.wecom_msg_id,
                    media_id=row.media_id,
                    audio_format=row.audio_format,
                    source=row.source,
                    transcript_text=row.transcript_text,
                    status=row.status.value,
                    error=row.error,
                    latency_ms=row.latency_ms,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in items
            ],
            total=total,
            page=page,
            size=size,
        )

    def list_user_deliveries(
        self,
        *,
        user_id: int,
        status: str | None,
        from_utc: datetime | None,
        to_utc: datetime | None,
        page: int,
        size: int,
    ) -> AdminUserDeliveryListResponse:
        items, total = self.repo.list_user_deliveries(
            user_id=user_id,
            status=status,
            from_utc=from_utc,
            to_utc=to_utc,
            page=page,
            size=size,
        )
        return AdminUserDeliveryListResponse(
            items=[
                AdminUserDeliveryItem(
                    id=row.id,
                    reminder_id=row.reminder_id,
                    planned_at_utc=row.planned_at_utc,
                    sent_at_utc=row.sent_at_utc,
                    delay_seconds=row.delay_seconds,
                    status=row.status.value,
                    error=row.error,
                )
                for row in items
            ],
            total=total,
            page=page,
            size=size,
        )

    def list_user_devices(self, user_id: int) -> AdminUserDeviceListResponse:
        user = self.repo.get_user(user_id)
        if not user:
            return AdminUserDeviceListResponse(items=[])
        rows = self.repo.list_user_devices(user_id)
        return AdminUserDeviceListResponse(
            items=[
                AdminUserDeviceItem(
                    id=row.id,
                    user_id=row.user_id,
                    device_id=row.device_id,
                    pair_code=row.pair_code,
                    pair_code_expires_at=row.pair_code_expires_at,
                    token_version=row.token_version,
                    is_active=row.is_active,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]
        )

    def cancel_reminder(self, reminder_id: int) -> AdminActionResponse:
        reminder = self.repo.get_reminder(reminder_id)
        if not reminder:
            raise ValueError("reminder not found")
        prev_status = reminder.status.value
        if reminder.status == ReminderStatus.CANCELED:
            return AdminActionResponse(
                ok=True,
                reminder_id=reminder.id,
                previous_status=prev_status,
                current_status=prev_status,
                no_change=True,
                message="already canceled",
            )
        reminder.status = ReminderStatus.CANCELED
        reminder.updated_at = now_utc()
        self.db.add(reminder)
        self.db.flush()
        return AdminActionResponse(
            ok=True,
            reminder_id=reminder.id,
            previous_status=prev_status,
            current_status=reminder.status.value,
            no_change=False,
        )

    def retry_reminder(self, reminder_id: int) -> AdminActionResponse:
        reminder = self.repo.get_reminder(reminder_id)
        if not reminder:
            raise ValueError("reminder not found")
        prev_status = reminder.status.value
        prev_error = reminder.last_error
        prev_next_run = reminder.next_run_utc

        reminder.status = ReminderStatus.PENDING
        reminder.last_error = None
        reminder.next_run_utc = now_utc()
        reminder.updated_at = now_utc()
        self.db.add(reminder)
        self.db.flush()

        return AdminActionResponse(
            ok=True,
            reminder_id=reminder.id,
            previous_status=prev_status,
            current_status=reminder.status.value,
            previous_last_error=prev_error,
            previous_next_run_utc=prev_next_run,
            next_run_utc=reminder.next_run_utc,
            no_change=False,
        )

    def snooze_reminder(self, reminder_id: int, minutes: int) -> AdminActionResponse:
        if minutes not in self.SNOOZE_ALLOWED:
            raise ValueError(f"minutes must be one of {sorted(self.SNOOZE_ALLOWED)}")

        reminder = self.repo.get_reminder(reminder_id)
        if not reminder:
            raise ValueError("reminder not found")
        if reminder.status != ReminderStatus.PENDING:
            raise ValueError("only pending reminders can be snoozed")

        base = reminder.next_run_utc or reminder.run_at_utc
        if not base:
            raise ValueError("reminder has no schedulable run time")
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)

        prev_next_run = reminder.next_run_utc
        reminder.next_run_utc = base + timedelta(minutes=minutes)
        reminder.updated_at = now_utc()
        self.db.add(reminder)
        self.db.flush()

        return AdminActionResponse(
            ok=True,
            reminder_id=reminder.id,
            previous_status=reminder.status.value,
            current_status=reminder.status.value,
            previous_next_run_utc=prev_next_run,
            next_run_utc=reminder.next_run_utc,
            no_change=False,
        )

    @staticmethod
    def dispatch_response(
        *,
        processed_count: int,
        duration_ms: int,
        error: str | None = None,
    ) -> AdminDispatchResponse:
        return AdminDispatchResponse(
            processed_count=processed_count,
            duration_ms=duration_ms,
            executed_at=datetime.now(timezone.utc),
            error=error,
        )

    def user_exists(self, user_id: int) -> bool:
        return self.repo.get_user(user_id) is not None
