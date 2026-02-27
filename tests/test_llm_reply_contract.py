from __future__ import annotations

from app.domain.enums import OperationType
from app.domain.schemas import IntentDraft
from app.llm.providers import NlgResult
from app.services.reply_generation_service import ReplyGenerationService
from app.services.reply_renderer import ReplyRenderer


class ScriptedReplyProvider:
    name = "fake_reply"
    mode = "local"
    model = "fake"
    prompt_version = "reply_v2_assistant"

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def generate_reply(self, event_type: str, facts: dict, fallback: str):
        text = self.mapping.get(event_type, fallback)
        return NlgResult(
            reply=text,
            provider=self.name,
            model=self.model,
            latency_ms=1,
            request_id="nlg-test",
            used_fallback=False,
        )

    def healthcheck(self):
        return True, None


def test_confirmation_prompt_contains_facts_and_next_step():
    reply = "我会在 2026-02-27 09:00（北京时间） 提醒你明天开会，回复确认即可。"
    service = ReplyGenerationService(reply_providers=[ScriptedReplyProvider({"confirmation_prompt": reply})])
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
    fallback = ReplyRenderer().confirmation_prompt(
        operation=draft.operation,
        content=draft.content,
        timezone=draft.timezone,
        schedule=None,
        run_at_local=draft.run_at_local,
        rrule=draft.rrule,
    )
    text = service.generate_confirmation_prompt(draft, fallback)
    assert "提醒你明天开会" in text
    assert "确认" in text


def test_add_success_contract():
    reply = "好的，我会在 2026-02-27 09:00（北京时间） 提醒你明天开会。"
    service = ReplyGenerationService(reply_providers=[ScriptedReplyProvider({"add_success": reply})])
    text = service.generate_add_success(
        content="明天开会",
        when_text="2026-02-27 09:00（北京时间）",
        timezone="Asia/Shanghai",
        fallback="fallback",
    )
    assert "明天开会" in text
    assert "2026-02-27 09:00（北京时间）" in text


def test_query_summary_contract():
    reply = "你现在有 1 条提醒：开会，我会在 2026-02-27 09:00（北京时间） 提醒你。"
    service = ReplyGenerationService(reply_providers=[ScriptedReplyProvider({"query_summary": reply})])
    text = service.generate_query_summary([(1, "开会", "2026-02-27 09:00（北京时间）")], fallback="fallback")
    assert "开会" in text
    assert "提醒" in text


def test_reply_fallback_on_missing_facts():
    provider = ScriptedReplyProvider({"add_success": "好的。"})
    service = ReplyGenerationService(reply_providers=[provider])
    fallback = "安排好了：明天开会。我会在 2026-02-27 09:00（北京时间） 提醒你。"
    text = service.generate_add_success(
        content="明天开会",
        when_text="2026-02-27 09:00（北京时间）",
        timezone="Asia/Shanghai",
        fallback=fallback,
    )
    assert text == fallback
    assert service.last_error is not None


def test_delete_not_found_requires_keyword_and_delete_intent():
    provider = ScriptedReplyProvider({"not_found_delete": "我没找到相关记录，回复确认即可。"})
    service = ReplyGenerationService(reply_providers=[provider])
    fallback = "我没找到可删除的那条提醒，你可以换个关键词再试试。"
    text = service.generate_not_found_delete(keyword="开会", fallback=fallback)
    assert text == fallback
    assert service.last_error is not None


def test_update_success_requires_update_intent():
    provider = ScriptedReplyProvider({"update_success": "好的，这条提醒处理好了。"})
    service = ReplyGenerationService(reply_providers=[provider])
    fallback = "收到，这条提醒已经更新：开会"
    text = service.generate_update_success(
        content="开会",
        when_text="2026-02-27 09:00（北京时间）",
        fallback=fallback,
    )
    assert text == fallback
    assert service.last_error is not None
