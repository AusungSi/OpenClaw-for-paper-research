from __future__ import annotations

from app.domain.enums import OperationType, ScheduleType
from app.domain.schemas import IntentLite
from app.services.intent_service import IntentService


class QueryIntentProvider:
    name = "fake"
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


def test_query_stays_query_not_add():
    service = IntentService(intent_providers=[QueryIntentProvider()])
    draft = service.parse_intent("查询", "Asia/Shanghai")
    assert draft.operation == OperationType.QUERY
    assert draft.schedule is None
    assert draft.run_at_local is None
    assert draft.needs_confirmation is False


def test_delete_contract_with_content():
    service = IntentService(intent_providers=[QueryIntentProvider()])
    lite = IntentLite(
        operation=OperationType.DELETE,
        content="开会",
        when_text=None,
        confidence=0.8,
        clarification_question=None,
    )
    draft = service.normalize_intent_lite(lite, text="删除 开会", timezone_name="Asia/Shanghai")
    assert draft.operation == OperationType.DELETE
    assert draft.content == "开会"
    assert draft.schedule is None
    assert draft.run_at_local is None


def test_update_without_content_needs_clarification():
    service = IntentService(intent_providers=[QueryIntentProvider()])
    lite = IntentLite(
        operation=OperationType.UPDATE,
        content="",
        when_text="明天9点",
        confidence=0.7,
        clarification_question=None,
    )
    draft = service.normalize_intent_lite(lite, text="改成明天9点", timezone_name="Asia/Shanghai")
    assert draft.operation == OperationType.UPDATE
    assert draft.clarification_question is not None


def test_add_with_when_text_has_run_at_local():
    service = IntentService(intent_providers=[QueryIntentProvider()])
    lite = IntentLite(
        operation=OperationType.ADD,
        content="开会",
        when_text="明天早上9点",
        confidence=0.9,
        clarification_question=None,
    )
    draft = service.normalize_intent_lite(lite, text="提醒我开会", timezone_name="Asia/Shanghai")
    assert draft.operation == OperationType.ADD
    assert draft.schedule in (ScheduleType.ONE_TIME, ScheduleType.RRULE)
    assert draft.run_at_local is not None
    assert draft.clarification_question is None


def test_add_without_time_returns_clarification():
    service = IntentService(intent_providers=[QueryIntentProvider()])
    lite = IntentLite(
        operation=OperationType.ADD,
        content="开会",
        when_text=None,
        confidence=0.8,
        clarification_question=None,
    )
    draft = service.normalize_intent_lite(lite, text="提醒我开会", timezone_name="Asia/Shanghai")
    assert draft.operation == OperationType.ADD
    assert draft.schedule is None
    assert draft.run_at_local is None
    assert draft.clarification_question is not None


def test_query_keeps_empty_content():
    service = IntentService(intent_providers=[QueryIntentProvider()])
    lite = IntentLite(
        operation=OperationType.QUERY,
        content="",
        when_text=None,
        confidence=0.9,
        clarification_question=None,
    )
    draft = service.normalize_intent_lite(lite, text="查询", timezone_name="Asia/Shanghai")
    assert draft.operation == OperationType.QUERY
    assert draft.content == ""
