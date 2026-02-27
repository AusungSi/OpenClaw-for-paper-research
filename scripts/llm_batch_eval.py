from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm.ollama_client import OllamaClient
from app.llm.providers import LocalOllamaIntentProvider
from app.services.intent_service import IntentService
from app.services.reply_generation_service import ReplyGenerationService


@dataclass
class EvalResult:
    name: str
    passed: bool
    detail: str


def eval_intent(repeat: int) -> list[EvalResult]:
    provider = LocalOllamaIntentProvider(ollama_client=OllamaClient())
    service = IntentService(intent_providers=[provider])
    cases: list[dict[str, Any]] = [
        {"name": "query_basic", "text": "查询", "expect_op": "query", "expect_clarify": False},
        {"name": "delete_keyword", "text": "删除 开会", "expect_op": "delete", "expect_clarify": False},
        {"name": "add_with_time", "text": "明天早上9点提醒我开会", "expect_op": "add", "expect_time": True},
        {"name": "add_without_time", "text": "提醒我开会", "expect_op": "add", "expect_clarify": True},
        {"name": "rrule_weekly", "text": "每周五下午3点提醒我周报", "expect_op": "add", "expect_rrule": True},
        {"name": "update_with_time", "text": "把开会改成明天10点", "expect_op": "update", "expect_time": True},
    ]

    results: list[EvalResult] = []
    for case in cases:
        pass_count = 0
        details = []
        for _ in range(repeat):
            lite = provider.parse_intent(case["text"], "Asia/Shanghai", [])
            draft = service.normalize_intent_lite(lite, text=case["text"], timezone_name="Asia/Shanghai")
            ok = draft.operation.value == case["expect_op"]
            if case.get("expect_time"):
                ok = ok and bool(draft.run_at_local)
            if case.get("expect_rrule"):
                ok = ok and bool(draft.rrule)
            if case.get("expect_clarify"):
                ok = ok and bool(draft.clarification_question)
            if "expect_clarify" in case and not case.get("expect_clarify"):
                ok = ok and not bool(draft.clarification_question)
            if ok:
                pass_count += 1
            details.append(
                f"op={draft.operation.value},run_at={bool(draft.run_at_local)},rrule={bool(draft.rrule)},clarify={bool(draft.clarification_question)}"
            )
        passed = pass_count == repeat
        results.append(EvalResult(case["name"], passed, f"{pass_count}/{repeat} passes; {details[-1]}"))
    return results


def eval_reply(repeat: int) -> list[EvalResult]:
    service = ReplyGenerationService()
    cases: list[dict[str, Any]] = [
        {
            "name": "confirmation_prompt",
            "event_type": "confirmation_prompt",
            "facts": {
                "operation": "add",
                "content": "开会",
                "timezone": "Asia/Shanghai",
                "schedule": "one_time",
                "run_at_local": "2026-02-28T09:00:00+08:00",
                "rrule": None,
            },
            "fallback": "我会在 2026-02-28 09:00（北京时间） 提醒你开会，回复确认即可。",
            "validator": lambda text: ("提醒" in text and "确认" in text and "开会" in text),
        },
        {
            "name": "add_success",
            "event_type": "add_success",
            "facts": {
                "operation": "add",
                "content": "开会",
                "time_text": "2026-02-28 09:00（北京时间）",
                "timezone": "Asia/Shanghai",
                "status": "created",
            },
            "fallback": "安排好了：开会。我会在 2026-02-28 09:00（北京时间） 提醒你。",
            "validator": lambda text: ("提醒" in text and "开会" in text),
        },
        {
            "name": "query_summary",
            "event_type": "query_summary",
            "facts": {
                "operation": "query",
                "items": [{"id": 1, "content": "开会", "when": "2026-02-28 09:00（北京时间）"}],
                "count": 1,
            },
            "fallback": "你现在有 1 条待提醒：[1] 开会，我会在 2026-02-28 09:00（北京时间） 提醒你。",
            "validator": lambda text: ("提醒" in text and "开会" in text),
        },
        {
            "name": "not_found_delete",
            "event_type": "not_found_delete",
            "facts": {"operation": "delete", "keyword": "开会", "status": "not_found"},
            "fallback": "我没找到可删除的那条提醒，你可以换个关键词再试试。",
            "validator": lambda text: ("删" in text),
        },
    ]

    results: list[EvalResult] = []
    for case in cases:
        pass_count = 0
        details = []
        for _ in range(repeat):
            reply = service.generate_event_reply(
                event_type=case["event_type"],
                facts=case["facts"],
                fallback=case["fallback"],
                required_keywords=[],
            )
            used_fallback = reply == case["fallback"]
            ok = bool(case["validator"](reply))
            if ok:
                pass_count += 1
            details.append(f"fallback={used_fallback},last_error={service.last_error}")
        passed = pass_count == repeat
        results.append(EvalResult(case["name"], passed, f"{pass_count}/{repeat} passes; {details[-1]}"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch evaluate MemoMate LLM behavior")
    parser.add_argument("--repeat", type=int, default=2, help="Repeat each case N times")
    args = parser.parse_args()

    client = OllamaClient()
    if not client.healthcheck():
        print("ollama_unavailable=True")
        return 2

    print("=== intent batch eval ===")
    intent_results = eval_intent(args.repeat)
    for row in intent_results:
        print(f"{row.name}: {'PASS' if row.passed else 'FAIL'} | {row.detail}")

    print("\n=== reply batch eval ===")
    reply_results = eval_reply(args.repeat)
    for row in reply_results:
        print(f"{row.name}: {'PASS' if row.passed else 'FAIL'} | {row.detail}")

    passed_all = all(r.passed for r in intent_results + reply_results)
    print(f"\nsummary_passed={passed_all}")
    return 0 if passed_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
