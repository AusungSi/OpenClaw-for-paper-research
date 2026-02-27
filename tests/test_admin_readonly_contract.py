from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.core.timezone import now_utc
from app.domain.enums import OperationType, PendingActionStatus, ReminderStatus, ScheduleType, VoiceRecordStatus
from app.domain.models import InboundMessage, PendingAction, Reminder, User, VoiceRecord
from admin_test_utils import build_admin_test_client


def _count_rows(db_session) -> dict[str, int]:
    return {
        "users": int(db_session.execute(select(func.count(User.id))).scalar_one()),
        "reminders": int(db_session.execute(select(func.count(Reminder.id))).scalar_one()),
        "pending_actions": int(db_session.execute(select(func.count(PendingAction.id))).scalar_one()),
        "inbound_messages": int(db_session.execute(select(func.count(InboundMessage.id))).scalar_one()),
        "voice_records": int(db_session.execute(select(func.count(VoiceRecord.id))).scalar_one()),
    }


def test_admin_user_audit_endpoints_are_readonly():
    client, session_local = build_admin_test_client()
    db_session = session_local()
    now = now_utc()
    user = User(
        wecom_user_id="readonly_audit",
        timezone="Asia/Shanghai",
        locale="zh-CN",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.flush()

    db_session.add(
        Reminder(
            user_id=user.id,
            content="readonly reminder",
            schedule_type=ScheduleType.ONE_TIME,
            run_at_utc=now + timedelta(hours=1),
            rrule=None,
            timezone="Asia/Shanghai",
            next_run_utc=now + timedelta(hours=1),
            status=ReminderStatus.PENDING,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        PendingAction(
            action_id="readonly-act",
            user_id=user.id,
            action_type=OperationType.ADD,
            draft_json='{"content":"x"}',
            source_message_id="msg-1",
            status=PendingActionStatus.PENDING,
            expires_at=now + timedelta(minutes=5),
            created_at=now,
            updated_at=now,
        )
    )
    db_session.add(
        InboundMessage(
            wecom_msg_id="readonly-msg",
            user_id=user.id,
            msg_type="text",
            raw_xml="<xml/>",
            normalized_text="hi",
            created_at=now,
        )
    )
    db_session.add(
        VoiceRecord(
            user_id=user.id,
            wecom_msg_id="readonly-voice",
            media_id="m-1",
            audio_format="amr",
            source="local",
            transcript_text="t",
            status=VoiceRecordStatus.TRANSCRIBED,
            error=None,
            latency_ms=12,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.flush()

    before = _count_rows(db_session)
    paths = [
        f"/api/v1/admin/users/{user.id}/overview",
        f"/api/v1/admin/users/{user.id}/reminders",
        f"/api/v1/admin/users/{user.id}/pending-actions",
        f"/api/v1/admin/users/{user.id}/inbound-messages",
        f"/api/v1/admin/users/{user.id}/voice-records",
        f"/api/v1/admin/users/{user.id}/deliveries",
        f"/api/v1/admin/users/{user.id}/devices",
    ]
    for path in paths:
        resp = client.get(path)
        assert resp.status_code == 200

    after = _count_rows(db_session)
    assert before == after
    db_session.close()
