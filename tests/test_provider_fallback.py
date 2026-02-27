from __future__ import annotations

from app.core.config import Settings
from app.domain.enums import OperationType
from app.domain.schemas import IntentLite
from app.llm.providers import LlmProviderError, NlgResult
from app.services.provider_factory import build_asr_providers
from app.services.asr_service import AsrError, AsrService, TranscriptionResult
from app.services.intent_service import IntentService
from app.services.reply_generation_service import ReplyGenerationService


class FailingIntentProvider:
    name = "external_intent"
    mode = "external"
    model = "external-model"

    def parse_intent(self, text: str, timezone_name: str, context_messages: list[str]) -> IntentLite:
        raise LlmProviderError("external down")

    def healthcheck(self):
        return False, "down"


class OkIntentProvider:
    name = "ollama"
    mode = "local"
    model = "qwen3:8b"

    def parse_intent(self, text: str, timezone_name: str, context_messages: list[str]) -> IntentLite:
        return IntentLite(
            operation=OperationType.ADD,
            content="开会",
            when_text="明天9点",
            confidence=0.9,
            clarification_question=None,
        )

    def healthcheck(self):
        return True, None


class FailingReplyProvider:
    name = "external_reply"
    mode = "external"
    model = "external"

    def generate_reply(self, event_type: str, facts: dict, fallback: str):
        raise LlmProviderError("external down")

    def healthcheck(self):
        return False, "down"


class OkReplyProvider:
    name = "ollama"
    mode = "local"
    model = "qwen3:8b"

    def generate_reply(self, event_type: str, facts: dict, fallback: str):
        content = str(facts.get("content", "已处理"))
        time_text = str(facts.get("time_text", "稍后"))
        return NlgResult(
            reply=f"好的，{content}我记下了，会在{time_text}提醒你，请回复确认。",
            provider=self.name,
            model=self.model,
            latency_ms=20,
            request_id="nlg-1",
            used_fallback=False,
        )

    def healthcheck(self):
        return True, None


class FailingAsrProvider:
    name = "iflytek"
    mode = "external"
    model = "iflytek"

    def transcribe(self, audio_bytes: bytes, *, filename=None, mime_type=None, language_hint=None):
        raise AsrError("external down")

    def healthcheck(self):
        return False, "down"


class OkAsrProvider:
    name = "local"
    mode = "local"
    model = "large-v3"

    def transcribe(self, audio_bytes: bytes, *, filename=None, mime_type=None, language_hint=None):
        return TranscriptionResult(
            text="提醒我开会",
            language="zh",
            provider=self.name,
            model=self.model,
            request_id="asr-1",
            latency_ms=10,
            used_fallback=False,
        )

    def healthcheck(self):
        return True, None


def test_intent_provider_fallback_to_local():
    service = IntentService(intent_providers=[FailingIntentProvider(), OkIntentProvider()])
    service.settings.intent_fallback_enabled = True
    draft = service.parse_intent("明天9点提醒我开会", "Asia/Shanghai")
    assert draft.operation == OperationType.ADD
    assert draft.content == "开会"


def test_reply_provider_fallback_to_local():
    service = ReplyGenerationService(reply_providers=[FailingReplyProvider(), OkReplyProvider()])
    service.settings.reply_fallback_enabled = True
    result = service.generate_add_success(
        content="开会",
        when_text="2026-02-27 09:00（北京时间）",
        timezone="Asia/Shanghai",
        fallback="fallback",
    )
    assert "开会" in result
    assert "确认" in result


def test_asr_provider_fallback_to_local():
    service = AsrService(providers=[FailingAsrProvider(), OkAsrProvider()])
    service.settings.asr_fallback_enabled = True
    result = service.transcribe_bytes(b"abc", filename="a.wav", mime_type="audio/wav", language_hint="zh")
    assert result.text == "提醒我开会"
    assert result.used_fallback is True


def test_build_asr_providers_skips_external_when_disabled():
    settings = Settings(
        asr_provider="local",
        asr_fallback_enabled=True,
        asr_external_enabled=False,
        fallback_order="external,local",
    )
    providers = build_asr_providers(settings)
    assert [provider.name for provider in providers] == ["local"]


def test_asr_service_keeps_first_error_for_diagnosis():
    service = AsrService(providers=[FailingAsrProvider(), FailingAsrProvider()])
    service.settings.asr_fallback_enabled = True
    try:
        service.transcribe_bytes(b"abc", filename="a.wav", mime_type="audio/wav", language_hint="zh")
    except AsrError as exc:
        assert str(exc) == "external down"
        assert "iflytek:external down" in (service.last_error or "")
    else:
        raise AssertionError("expected AsrError")
