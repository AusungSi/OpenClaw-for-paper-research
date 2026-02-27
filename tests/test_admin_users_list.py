from __future__ import annotations

from datetime import timedelta

from app.core.timezone import now_utc
from app.domain.enums import DeliveryStatus, ReminderStatus, ScheduleType, VoiceRecordStatus
from app.domain.models import DeliveryLog, InboundMessage, Reminder, User, VoiceRecord
from admin_test_utils import build_admin_test_client


def test_admin_users_list_with_aggregates():
    client, session_local = build_admin_test_client()
    db_session = session_local()
    now = now_utc()
    user_a = User(
        wecom_user_id="alice_wecom",
        timezone="Asia/Shanghai",
        locale="zh-CN",
        created_at=now - timedelta(days=2),
        updated_at=now,
    )
    user_b = User(
        wecom_user_id="bob_wecom",
        timezone="Asia/Tokyo",
        locale="zh-CN",
        created_at=now - timedelta(days=1),
        updated_at=now - timedelta(minutes=10),
    )
    db_session.add_all([user_a, user_b])
    db_session.flush()

    reminder_a = Reminder(
        user_id=user_a.id,
        content="alice pending",
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
    reminder_b = Reminder(
        user_id=user_b.id,
        content="bob completed",
        schedule_type=ScheduleType.ONE_TIME,
        run_at_utc=now - timedelta(hours=1),
        rrule=None,
        timezone="Asia/Tokyo",
        next_run_utc=None,
        status=ReminderStatus.COMPLETED,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    db_session.add_all([reminder_a, reminder_b])
    db_session.flush()

    db_session.add(
        DeliveryLog(
            reminder_id=reminder_a.id,
            planned_at_utc=now - timedelta(minutes=2),
            sent_at_utc=now - timedelta(minutes=1),
            delay_seconds=60,
            status=DeliveryStatus.FAILED,
            error="test failed",
        )
    )
    db_session.add(
        InboundMessage(
            wecom_msg_id="msg-1",
            user_id=user_a.id,
            msg_type="text",
            raw_xml="<xml>1</xml>",
            normalized_text="hello",
            created_at=now - timedelta(minutes=3),
        )
    )
    db_session.add(
        VoiceRecord(
            user_id=user_a.id,
            wecom_msg_id="v-1",
            media_id="m-1",
            audio_format="amr",
            source="local",
            transcript_text="hello voice",
            status=VoiceRecordStatus.TRANSCRIBED,
            error=None,
            latency_ms=123,
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=2),
        )
    )
    db_session.flush()

    resp = client.get("/api/v1/admin/users", params={"q": "alice", "page": 1, "size": 10, "sort": "updated_at"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["wecom_user_id"] == "alice_wecom"
    assert item["pending_reminders"] == 1
    assert item["failed_deliveries_24h"] == 1
    assert item["last_inbound_at"] is not None
    assert item["last_voice_status"] == "transcribed"
    db_session.close()


def test_admin_users_list_pagination():
    client, session_local = build_admin_test_client()
    db_session = session_local()
    now = now_utc()
    for idx in range(3):
        db_session.add(
            User(
                wecom_user_id=f"user_{idx}",
                timezone="Asia/Shanghai",
                locale="zh-CN",
                created_at=now - timedelta(minutes=idx),
                updated_at=now - timedelta(minutes=idx),
            )
        )
    db_session.flush()

    resp = client.get("/api/v1/admin/users", params={"page": 2, "size": 2, "sort": "created_at"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["page"] == 2
    assert body["size"] == 2
    assert len(body["items"]) == 1
    db_session.close()
