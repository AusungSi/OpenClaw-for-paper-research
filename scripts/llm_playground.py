from __future__ import annotations

import argparse
import sys
from pathlib import Path

import orjson

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.domain.schemas import IntentLite
from app.llm.ollama_client import OllamaClient
from app.llm.providers import LocalOllamaIntentProvider, LocalOllamaReplyProvider
from app.services.intent_service import IntentService
from app.services.reply_generation_service import ReplyGenerationService


def run_intent(args: argparse.Namespace) -> int:
    provider = LocalOllamaIntentProvider(ollama_client=OllamaClient())
    service = IntentService(intent_providers=[provider])

    for idx in range(args.repeat):
        raw = provider.parse_intent_raw(args.text, args.timezone, args.context or [])
        lite = IntentLite.model_validate(raw)
        draft = service.normalize_intent_lite(
            lite,
            text=args.text,
            timezone_name=args.timezone,
        )

        print(f"\n=== intent run {idx + 1} ===")
        print("raw_json:")
        print(orjson.dumps(raw, option=orjson.OPT_INDENT_2).decode("utf-8"))
        print("intent_lite:")
        print(orjson.dumps(lite.model_dump(), option=orjson.OPT_INDENT_2).decode("utf-8"))
        print("normalized_draft:")
        print(orjson.dumps(draft.model_dump(), option=orjson.OPT_INDENT_2).decode("utf-8"))
    return 0


def run_reply(args: argparse.Namespace) -> int:
    provider = LocalOllamaReplyProvider(ollama_client=OllamaClient())
    service = ReplyGenerationService(reply_providers=[provider])

    facts = orjson.loads(args.facts_json)
    required = args.required or []

    for idx in range(args.repeat):
        reply = service.generate_event_reply(
            event_type=args.event_type,
            facts=facts,
            fallback=args.fallback,
            required_keywords=required,
        )
        print(f"\n=== reply run {idx + 1} ===")
        print(reply)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MemoMate LLM playground")
    sub = parser.add_subparsers(dest="mode", required=True)

    intent = sub.add_parser("intent", help="Test intent prompt and normalization")
    intent.add_argument("--text", required=True, help="User input text")
    intent.add_argument("--timezone", default="Asia/Shanghai", help="User timezone")
    intent.add_argument("--context", action="append", default=[], help="Recent context text, can repeat")
    intent.add_argument("--repeat", type=int, default=1, help="Run count")
    intent.set_defaults(func=run_intent)

    reply = sub.add_parser("reply", help="Test reply generation")
    reply.add_argument("--event-type", required=True, help="NLG event type")
    reply.add_argument("--facts-json", required=True, help="Facts JSON string")
    reply.add_argument("--fallback", default="我先按这个结果帮你处理。", help="Fallback text")
    reply.add_argument("--required", action="append", default=[], help="Required keyword, can repeat")
    reply.add_argument("--repeat", type=int, default=1, help="Run count")
    reply.set_defaults(func=run_reply)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
