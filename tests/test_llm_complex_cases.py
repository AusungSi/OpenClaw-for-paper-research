from __future__ import annotations

from app.domain.enums import OperationType, ScheduleType
from app.domain.schemas import IntentDraft, IntentLite
from app.llm.providers import NlgResult
from app.services.intent_service import IntentService
from app.services.reply_generation_service import ReplyGenerationService
from app.services.reply_renderer import ReplyRenderer


class StaticIntentProvider:
    name = "fake_intent"
    mode = "local"
    model = "fake"
    prompt_version = "intent_v2_minimal"

    def parse_intent(self, text: str, timezone_name: str, context_messages: list[str]) -> IntentLite:
        return IntentLite(
            operation=OperationType.QUERY,
            content="",
            when_text=None,
            confidence=0.9,
            clarification_question=None,
        )

    def healthcheck(self):
        return True, None


class ScriptedReplyProvider:
    name = "fake_reply"
    mode = "local"
    model = "fake"
    prompt_version = "reply_v2_assistant"

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def generate_reply(self, event_type: str, facts: dict, fallback: str):
        return NlgResult(
            reply=self.mapping.get(event_type, fallback),
            provider=self.name,
            model=self.model,
            latency_ms=1,
            request_id="complex-test",
            used_fallback=False,
        )

    def healthcheck(self):
        return True, None


def _intent_service() -> IntentService:
    return IntentService(intent_providers=[StaticIntentProvider()])


def test_intent_complex_monthly_last_workday_rrule():
    service = _intent_service()
    lite = IntentLite(
        operation=OperationType.ADD,
        content="\u63d0\u4ea4\u62a5\u9500\u6750\u6599\u5e76\u540c\u6b65\u8d22\u52a1\u7fa4",
        when_text="\u6bcf\u6708\u6700\u540e\u4e00\u4e2a\u5de5\u4f5c\u65e5\u665a\u4e0a8\u70b9",
        confidence=0.95,
        clarification_question=None,
    )
    draft = service.normalize_intent_lite(
        lite,
        text="\u9ebb\u70e6\u5e2e\u6211\u8bbe\u7f6e\u4e00\u4e2a\u91cd\u590d\u63d0\u9192\uff1a\u6bcf\u6708\u6700\u540e\u4e00\u4e2a\u5de5\u4f5c\u65e5\u665a\u4e0a8\u70b9\uff0c\u63d0\u4ea4\u62a5\u9500\u6750\u6599\u5e76\u540c\u6b65\u8d22\u52a1\u7fa4\u3002",
        timezone_name="Asia/Shanghai",
    )
    assert draft.operation == OperationType.ADD
    assert draft.schedule == ScheduleType.RRULE
    assert draft.rrule == "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"
    assert draft.run_at_local is not None
    assert draft.clarification_question is None


def test_intent_complex_weekly_update_rrule():
    service = _intent_service()
    lite = IntentLite(
        operation=OperationType.UPDATE,
        content="\u9879\u76ee\u5468\u62a5",
        when_text="\u6bcf\u5468\u4e94\u4e0b\u53486\u70b9",
        confidence=0.9,
        clarification_question=None,
    )
    draft = service.normalize_intent_lite(
        lite,
        text="\u8bf7\u628a\u539f\u6765\u7684\u9879\u76ee\u5468\u62a5\u63d0\u9192\u4fee\u6539\u6210\u6bcf\u5468\u4e94\u4e0b\u53486\u70b9\uff0c\u9884\u7559\u534a\u5c0f\u65f6\u7ed9\u6211\u6574\u7406\u9644\u4ef6\u3002",
        timezone_name="Asia/Shanghai",
    )
    assert draft.operation == OperationType.UPDATE
    assert draft.schedule == ScheduleType.RRULE
    assert draft.rrule == "FREQ=WEEKLY;BYDAY=FR"
    assert draft.run_at_local is not None
    assert draft.clarification_question is None


def test_intent_long_query_text_keeps_query_semantics():
    service = _intent_service()
    lite = IntentLite(
        operation=OperationType.QUERY,
        content="",
        when_text=None,
        confidence=0.88,
        clarification_question="\u8fd9\u6761\u6e05\u7a7a",
    )
    draft = service.normalize_intent_lite(
        lite,
        text="\u5e2e\u6211\u68c0\u67e5\u4e00\u4e0b\u4ece\u660e\u5929\u5230\u4e0b\u5468\u4e09\u671f\u95f4\uff0c\u6240\u6709\u548c\u9762\u8bd5\u3001\u5468\u62a5\u4ee5\u53ca\u62a5\u9500\u76f8\u5173\u7684\u63d0\u9192\u6761\u76ee\u3002",
        timezone_name="Asia/Shanghai",
    )
    assert draft.operation == OperationType.QUERY
    assert draft.schedule is None
    assert draft.run_at_local is None
    assert draft.needs_confirmation is False
    assert draft.clarification_question is None


def test_intent_delete_with_time_like_content_needs_clarification():
    service = _intent_service()
    lite = IntentLite(
        operation=OperationType.DELETE,
        content="\u660e\u5929\u4e0b\u53483\u70b9",
        when_text=None,
        confidence=0.83,
        clarification_question=None,
    )
    draft = service.normalize_intent_lite(
        lite,
        text="\u5220\u9664\u660e\u5929\u4e0b\u53483\u70b9\u90a3\u6761",
        timezone_name="Asia/Shanghai",
    )
    assert draft.operation == OperationType.DELETE
    assert draft.clarification_question is not None


def test_reply_confirmation_prompt_long_and_conversational():
    draft = IntentDraft(
        operation=OperationType.ADD,
        content="\u51c6\u5907\u5ba2\u6237A\u7684\u7acb\u9879\u8bc4\u5ba1\u6750\u6599",
        timezone="Asia/Shanghai",
        schedule=ScheduleType.ONE_TIME,
        run_at_local="2026-03-02T20:00:00+08:00",
        rrule=None,
        confidence=1.0,
        needs_confirmation=True,
        clarification_question=None,
    )
    reply = (
        "\u597d\u7684\uff0c\u6211\u4f1a\u5728 2026-03-02 20:00\uff08\u5317\u4eac\u65f6\u95f4\uff09 "
        "\u63d0\u9192\u4f60\u51c6\u5907\u5ba2\u6237A\u7684\u7acb\u9879\u8bc4\u5ba1\u6750\u6599\uff0c"
        "\u8fd9\u6837\u4f60\u53ef\u4ee5\u6709\u8db3\u591f\u65f6\u95f4\u6700\u540e\u68c0\u67e5\u4e00\u904d\u3002\u56de\u590d\u786e\u8ba4\u5373\u53ef\u3002"
    )
    service = ReplyGenerationService(reply_providers=[ScriptedReplyProvider({"confirmation_prompt": reply})])
    fallback = ReplyRenderer().confirmation_prompt(
        operation=draft.operation,
        content=draft.content,
        timezone=draft.timezone,
        schedule=draft.schedule,
        run_at_local=draft.run_at_local,
        rrule=draft.rrule,
    )
    text = service.generate_confirmation_prompt(draft, fallback)
    assert text == reply
    assert service.last_error is None


def test_reply_add_success_with_long_task_keeps_facts():
    content = "\u5b8c\u6210Q1\u590d\u76d8PPT\u5b9a\u7a3f\u5e76\u53d1\u9001\u7ed9\u9879\u76ee\u7ec4"
    when_text = "2026-03-03 21:30\uff08\u5317\u4eac\u65f6\u95f4\uff09"
    reply = (
        "\u5b89\u6392\u597d\u4e86\uff0c\u6211\u4f1a\u5728 2026-03-03 21:30\uff08\u5317\u4eac\u65f6\u95f4\uff09 "
        "\u63d0\u9192\u4f60\u5b8c\u6210Q1\u590d\u76d8PPT\u5b9a\u7a3f\u5e76\u53d1\u9001\u7ed9\u9879\u76ee\u7ec4\uff0c"
        "\u4e0d\u4f1a\u8ba9\u4f60\u6f0f\u6389\u622a\u6b62\u65f6\u95f4\u3002"
    )
    service = ReplyGenerationService(reply_providers=[ScriptedReplyProvider({"add_success": reply})])
    text = service.generate_add_success(content=content, when_text=when_text, timezone="Asia/Shanghai", fallback="fallback")
    assert text == reply
    assert service.last_error is None


def test_reply_query_summary_for_multiple_complex_items():
    items = [
        (1, "\u9879\u76ee\u5468\u62a5", "2026-03-04 18:00\uff08\u5317\u4eac\u65f6\u95f4\uff09"),
        (2, "\u5ba2\u6237A\u7acb\u9879\u8bc4\u5ba1", "2026-03-05 10:00\uff08\u5317\u4eac\u65f6\u95f4\uff09"),
        (3, "\u62a5\u9500\u6750\u6599\u63d0\u4ea4", "2026-03-06 20:00\uff08\u5317\u4eac\u65f6\u95f4\uff09"),
    ]
    reply = (
        "\u4f60\u73b0\u5728\u4e00\u5171\u6709 3 \u6761\u63d0\u9192\uff0c\u6700\u8fd1\u7684\u662f\u9879\u76ee\u5468\u62a5\uff0c"
        "\u6211\u4f1a\u5728 2026-03-04 18:00\uff08\u5317\u4eac\u65f6\u95f4\uff09 \u63d0\u9192\u4f60\uff0c"
        "\u540e\u9762\u4e24\u6761\u4e5f\u5df2\u7ecf\u5e2e\u4f60\u8bb0\u597d\u4e86\u3002"
    )
    service = ReplyGenerationService(reply_providers=[ScriptedReplyProvider({"query_summary": reply})])
    text = service.generate_query_summary(items, fallback="fallback")
    assert text == reply
    assert service.last_error is None


def test_reply_update_success_falls_back_when_missing_update_signal():
    content = "\u9879\u76ee\u5468\u62a5"
    when_text = "2026-03-07 19:00\uff08\u5317\u4eac\u65f6\u95f4\uff09"
    fallback = "\u6536\u5230\uff0c\u8fd9\u6761\u63d0\u9192\u5df2\u7ecf\u66f4\u65b0\uff1a\u9879\u76ee\u5468\u62a5\uff0c\u65f6\u95f4\u662f 2026-03-07 19:00\uff08\u5317\u4eac\u65f6\u95f4\uff09\u3002"
    # Missing "\u6539" / "\u66f4\u65b0" signal on purpose to verify fallback path.
    reply = "\u597d\u7684\uff0c\u9879\u76ee\u5468\u62a5\u6211\u8bb0\u4f4f\u4e86\uff0c\u4f1a\u5728 2026-03-07 19:00\uff08\u5317\u4eac\u65f6\u95f4\uff09 \u63d0\u9192\u4f60\u3002"
    service = ReplyGenerationService(reply_providers=[ScriptedReplyProvider({"update_success": reply})])
    text = service.generate_update_success(content=content, when_text=when_text, fallback=fallback)
    assert text == fallback
    assert service.last_error is not None
