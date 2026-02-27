from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.timezone import now_utc
from app.domain.enums import OperationType, ReminderStatus, ScheduleType
from app.domain.models import Reminder, User
from app.infra.repos import ReminderRepo
from app.services.confirm_service import ConfirmService
from app.services.intent_service import IntentService
from app.services.message_ingest import MessageIngestService
from app.services.reminder_service import ReminderService


class FakeOllama:
    def __init__(self, now_local: datetime):
        self.now_local = now_local
        self.last_prompt = ""

    def generate_json(self, prompt: str, **kwargs):
        self.last_prompt = prompt
        return {
            "operation": "add",
            "content": "开会",
            "when_text": "明天早上9点",
            "confidence": 0.95,
            "clarification_question": None,
        }


class FakeWeCom:
    def __init__(self):
        self.messages: list[str] = []

    def send_text(self, user_id: str, content: str):
        self.messages.append(content)
        return True, None


class FailingOllama:
    def generate_json(self, prompt: str, **kwargs):
        raise RuntimeError("offline")


def test_intent_fallback_parse():
    service = IntentService(ollama_client=FailingOllama())
    draft = service.parse_intent("明天早上9点提醒我开会", "Asia/Shanghai")
    assert draft.operation == OperationType.ADD
    assert draft.needs_confirmation is True
    assert draft.schedule is not None


def test_confirm_flow_creates_reminder(db_session):
    tz = ZoneInfo("Asia/Shanghai")
    now_local = datetime.now(tz)
    intent_service = IntentService(ollama_client=FakeOllama(now_local))
    wecom = FakeWeCom()
    ingest = MessageIngestService(
        intent_service=intent_service,
        confirm_service=ConfirmService(),
        reminder_service=ReminderService(),
        wecom_client=wecom,
    )

    ingest.process_text_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="m1",
        raw_xml="<xml></xml>",
        text="明天早上9点提醒我开会",
    )
    ingest.process_text_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="m2",
        raw_xml="<xml></xml>",
        text="确认",
    )

    reminders, total = ReminderRepo(db_session).list(
        user_id=1,
        status="pending",
        page=1,
        size=10,
        from_utc=None,
        to_utc=None,
    )
    assert total == 1
    assert reminders[0].content == "开会"
    assert any("回复“确认”" in msg for msg in wecom.messages)
    assert any("安排好了" in msg and "北京时间" in msg for msg in wecom.messages)


def test_intent_prompt_includes_context():
    tz = ZoneInfo("Asia/Shanghai")
    now_local = datetime.now(tz)
    fake = FakeOllama(now_local)
    service = IntentService(ollama_client=fake)
    _ = service.parse_intent(
        "改成10点",
        "Asia/Shanghai",
        context_messages=["明天早上9点提醒我开会", "确认"],
    )
    assert "Recent conversation context" in fake.last_prompt
    assert "- 明天早上9点提醒我开会" in fake.last_prompt
    assert "- 确认" in fake.last_prompt


def test_duplicate_msg_id_is_deduped(db_session):
    tz = ZoneInfo("Asia/Shanghai")
    now_local = datetime.now(tz)
    intent_service = IntentService(ollama_client=FakeOllama(now_local))
    wecom = FakeWeCom()
    ingest = MessageIngestService(
        intent_service=intent_service,
        confirm_service=ConfirmService(),
        reminder_service=ReminderService(),
        wecom_client=wecom,
    )

    ingest.process_text_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="dup-1",
        raw_xml="<xml></xml>",
        text="明天早上9点提醒我开会",
    )
    ingest.process_text_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="dup-1",
        raw_xml="<xml></xml>",
        text="明天早上9点提醒我开会",
    )

    assert ingest.dedup_duplicates == 1
    assert ingest.webhook_dedup_ok is True
    assert len(wecom.messages) == 1


def test_query_summary_uses_beijing_time(db_session):
    now = now_utc()
    user = User(
        wecom_user_id="wangwu",
        timezone="Asia/Shanghai",
        locale="zh-CN",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.flush()

    reminder = Reminder(
        user_id=user.id,
        content="开周会",
        schedule_type=ScheduleType.ONE_TIME,
        run_at_utc=datetime(2026, 2, 27, 1, 0, tzinfo=timezone.utc),
        rrule=None,
        timezone="Asia/Shanghai",
        next_run_utc=datetime(2026, 2, 27, 1, 0, tzinfo=timezone.utc),
        status=ReminderStatus.PENDING,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    db_session.add(reminder)
    db_session.flush()

    text = ReminderService().query_summary(db_session, user.id)
    assert "2026-02-27 09:00（北京时间）" in text
    assert "UTC" not in text
