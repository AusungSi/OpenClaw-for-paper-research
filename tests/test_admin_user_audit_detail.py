from __future__ import annotations

from datetime import timedelta

from app.core.timezone import now_utc
from app.domain.enums import DeliveryStatus, OperationType, PendingActionStatus, ReminderStatus, ScheduleType, VoiceRecordStatus
from app.domain.models import (
    DeliveryLog,
    InboundMessage,
    MobileDevice,
    PendingAction,
    RefreshToken,
    Reminder,
    User,
    VoiceRecord,
)
from admin_test_utils import build_admin_test_client


def test_admin_user_detail_endpoints():
    client, session_local = build_admin_test_client()
    db_session = session_local()
    now = now_utc()
    user = User(
        wecom_user_id="audit_user",
        timezone="Asia/Shanghai",
        locale="zh-CN",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.flush()

    reminder = Reminder(
        user_id=user.id,
        content="audit reminder",
        schedule_type=ScheduleType.ONE_TIME,
        run_at_utc=now + timedelta(hours=2),
        rrule=None,
        timezone="Asia/Shanghai",
        next_run_utc=now + timedelta(hours=2),
        status=ReminderStatus.PENDING,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    db_session.add(reminder)
    db_session.flush()

    db_session.add(
        PendingAction(
            action_id="act-1",
            user_id=user.id,
            action_type=OperationType.ADD,
            draft_json='{"k":"v"}',
            source_message_id="msg-1",
            status=PendingActionStatus.PENDING,
            expires_at=now + timedelta(minutes=5),
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        InboundMessage(
            wecom_msg_id="in-1",
            user_id=user.id,
            msg_type="text",
            raw_xml="<xml/>",
            normalized_text="hello",
            created_at=now,
        )
    )
    db_session.add(
        VoiceRecord(
            user_id=user.id,
            wecom_msg_id="voice-1",
            media_id="m-1",
            audio_format="amr",
            source="local",
            transcript_text="voice text",
            status=VoiceRecordStatus.TRANSCRIBED,
            error=None,
            latency_ms=80,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        DeliveryLog(
            reminder_id=reminder.id,
            planned_at_utc=now,
            sent_at_utc=now,
            delay_seconds=0,
            status=DeliveryStatus.SENT,
            error=None,
        )
    )
    db_session.add(
        MobileDevice(
            user_id=user.id,
            device_id="iphone-1",
            pair_code=None,
            pair_code_expires_at=None,
            token_version=2,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        RefreshToken(
            user_id=user.id,
            device_id="iphone-1",
            token_hash="h1",
            expires_at=now + timedelta(days=10),
            revoked_at=None,
            created_at=now,
        )
    )
    db_session.flush()

    overview = client.get(f"/api/v1/admin/users/{user.id}/overview")
    assert overview.status_code == 200
    ob = overview.json()
    assert ob["user"]["wecom_user_id"] == "audit_user"
    assert ob["reminder_counts"]["pending"] == 1
    assert ob["token_stats"]["active"] == 1

    reminders = client.get(f"/api/v1/admin/users/{user.id}/reminders")
    assert reminders.status_code == 200
    assert reminders.json()["total"] == 1

    pending_actions = client.get(f"/api/v1/admin/users/{user.id}/pending-actions")
    assert pending_actions.status_code == 200
    assert pending_actions.json()["total"] == 1

    inbound = client.get(f"/api/v1/admin/users/{user.id}/inbound-messages")
    assert inbound.status_code == 200
    assert inbound.json()["total"] == 1

    voices = client.get(f"/api/v1/admin/users/{user.id}/voice-records")
    assert voices.status_code == 200
    assert voices.json()["total"] == 1

    deliveries = client.get(f"/api/v1/admin/users/{user.id}/deliveries")
    assert deliveries.status_code == 200
    assert deliveries.json()["total"] == 1

    devices = client.get(f"/api/v1/admin/users/{user.id}/devices")
    assert devices.status_code == 200
    assert len(devices.json()["items"]) == 1
    db_session.close()


def test_admin_user_not_found():
    client, _session_local = build_admin_test_client()
    resp = client.get("/api/v1/admin/users/999/overview")
    assert resp.status_code == 404
