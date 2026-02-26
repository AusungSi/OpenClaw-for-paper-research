from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.enums import ReminderStatus
from app.infra.repos import ReminderRepo
from app.services.asr_service import AsrError, TranscriptionResult
from app.services.confirm_service import ConfirmService
from app.services.intent_service import IntentService
from app.services.message_ingest import MessageIngestService
from app.services.reminder_service import ReminderService


class FakeOllama:
    def __init__(self, now_local: datetime):
        self.now_local = now_local

    def generate_json(self, prompt: str, **kwargs):
        return {
            "operation": "add",
            "content": "语音开会",
            "timezone": "Asia/Shanghai",
            "schedule": "one_time",
            "run_at_local": (self.now_local + timedelta(hours=1)).isoformat(),
            "rrule": None,
            "confidence": 0.95,
            "needs_confirmation": True,
            "clarification_question": None,
        }


class FakeWeCom:
    def __init__(self):
        self.messages: list[str] = []

    def send_text(self, user_id: str, content: str):
        self.messages.append(content)
        return True, None

    def download_media(self, media_id: str):
        return b"voice-bytes", "audio/amr"


class FakeAsrService:
    def __init__(self):
        self.called = 0

    def transcribe_wecom_media(self, *, wecom_client, media_id: str, audio_format: str | None):
        self.called += 1
        return TranscriptionResult(
            text="明天早上9点提醒我开会",
            language="zh",
            provider="local",
            model="large-v3",
            request_id="req-voice",
            latency_ms=100,
            used_fallback=False,
        )


class FailingAsrService:
    def __init__(self, error: str):
        self.error = error

    def transcribe_wecom_media(self, *, wecom_client, media_id: str, audio_format: str | None):
        raise AsrError(self.error)


def _build_ingest(now_local: datetime, wecom: FakeWeCom, asr_service=None) -> MessageIngestService:
    return MessageIngestService(
        intent_service=IntentService(ollama_client=FakeOllama(now_local)),
        confirm_service=ConfirmService(),
        reminder_service=ReminderService(),
        wecom_client=wecom,
        asr_service=asr_service,
    )


def test_voice_with_recognition_enters_confirm_flow(db_session):
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    wecom = FakeWeCom()
    ingest = _build_ingest(now_local, wecom, asr_service=FakeAsrService())

    ingest.process_voice_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="voice-1",
        raw_xml="<xml></xml>",
        media_id="m1",
        audio_format="amr",
        recognition="明天早上9点提醒我开会",
    )
    ingest.process_text_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="voice-2",
        raw_xml="<xml></xml>",
        text="确认",
    )

    reminders, total = ReminderRepo(db_session).list(
        user_id=1,
        status=ReminderStatus.PENDING.value,
        page=1,
        size=10,
        from_utc=None,
        to_utc=None,
    )
    assert total == 1
    assert reminders[0].content == "语音开会"
    assert any("确认" in msg for msg in wecom.messages)


def test_voice_with_media_uses_asr(db_session):
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    wecom = FakeWeCom()
    asr_service = FakeAsrService()
    ingest = _build_ingest(now_local, wecom, asr_service=asr_service)

    ingest.process_voice_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="voice-media-1",
        raw_xml="<xml></xml>",
        media_id="m2",
        audio_format="amr",
        recognition="",
    )
    assert asr_service.called == 1
    assert any("确认" in msg for msg in wecom.messages)


def test_voice_duplicate_msg_id_is_deduped(db_session):
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    wecom = FakeWeCom()
    ingest = _build_ingest(now_local, wecom, asr_service=FakeAsrService())

    ingest.process_voice_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="voice-dup",
        raw_xml="<xml></xml>",
        media_id="m3",
        audio_format="amr",
        recognition="明天早上9点提醒我开会",
    )
    ingest.process_voice_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="voice-dup",
        raw_xml="<xml></xml>",
        media_id="m3",
        audio_format="amr",
        recognition="明天早上9点提醒我开会",
    )
    assert ingest.dedup_duplicates == 1


def test_voice_asr_timeout_reply_is_friendly(db_session):
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    wecom = FakeWeCom()
    ingest = _build_ingest(now_local, wecom, asr_service=FailingAsrService("audio conversion timed out"))

    ingest.process_voice_message(
        db=db_session,
        wecom_user_id="zhangsan",
        msg_id="voice-timeout",
        raw_xml="<xml></xml>",
        media_id="m4",
        audio_format="amr",
        recognition="",
    )
    assert any("超时" in msg for msg in wecom.messages)
