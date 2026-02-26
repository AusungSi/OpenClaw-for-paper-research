from __future__ import annotations

from app.domain.enums import OperationType
from app.domain.schemas import IntentDraft
from app.services.reply_generation_service import ReplyGenerationService
from app.services.reply_renderer import ReplyRenderer


class ValidReplyOllama:
    def generate_json(self, prompt: str, **kwargs):
        return {"reply": "安排好啦，明天开会我会在2026-02-27 09:00（北京时间）提醒你。"}


class InvalidReplyOllama:
    def generate_json(self, prompt: str, **kwargs):
        return {"reply": "好的。"}


def test_nlg_add_success_contains_facts():
    service = ReplyGenerationService(ollama_client=ValidReplyOllama())
    result = service.generate_add_success(
        content="明天开会",
        when_text="2026-02-27 09:00（北京时间）",
        timezone="Asia/Shanghai",
        fallback="fallback",
    )
    assert "明天开会" in result
    assert "2026-02-27 09:00（北京时间）" in result
    assert service.last_error is None


def test_nlg_fallback_when_missing_facts():
    renderer = ReplyRenderer()
    draft = IntentDraft(
        operation=OperationType.ADD,
        content="明天开会",
        timezone="Asia/Shanghai",
        schedule=None,
        run_at_local="2026-02-27T09:00:00+08:00",
        rrule=None,
        confidence=1.0,
        needs_confirmation=True,
        clarification_question=None,
    )
    fallback = renderer.confirmation_prompt(
        operation=draft.operation,
        content=draft.content,
        timezone=draft.timezone,
        schedule=None,
        run_at_local=draft.run_at_local,
        rrule=draft.rrule,
    )
    service = ReplyGenerationService(ollama_client=InvalidReplyOllama())
    result = service.generate_confirmation_prompt(draft, fallback)
    assert result == fallback
    assert service.last_error is not None
