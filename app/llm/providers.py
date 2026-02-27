from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from time import perf_counter
from typing import Protocol
from zoneinfo import ZoneInfo

import orjson

from app.core.config import Settings, get_settings
from app.domain.schemas import IntentLite
from app.llm.ollama_client import OllamaClient, load_prompt_template


class LlmProviderError(Exception):
    pass


@dataclass
class NlgResult:
    reply: str
    provider: str
    model: str
    latency_ms: int
    request_id: str | None = None
    used_fallback: bool = False


class IntentLLMProvider(Protocol):
    name: str
    mode: str
    model: str

    def parse_intent(self, text: str, timezone_name: str, context_messages: list[str]) -> IntentLite:
        raise NotImplementedError

    def healthcheck(self) -> tuple[bool, str | None]:
        raise NotImplementedError


class ReplyLLMProvider(Protocol):
    name: str
    mode: str
    model: str

    def generate_reply(self, event_type: str, facts: dict, fallback: str) -> NlgResult:
        raise NotImplementedError

    def healthcheck(self) -> tuple[bool, str | None]:
        raise NotImplementedError


class LocalOllamaIntentProvider:
    name = "ollama"
    mode = "local"
    prompt_version = "intent_v2_minimal"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        ollama_client: OllamaClient | None = None,
        prompt_template: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ollama_client = ollama_client or OllamaClient()
        self.prompt_template = prompt_template or load_prompt_template()
        self.model = self.settings.intent_model or self.settings.ollama_model

    def parse_intent_raw(self, text: str, timezone_name: str, context_messages: list[str]) -> dict:
        now_local = datetime.now(ZoneInfo(timezone_name))
        context_text = self._format_context(context_messages)
        prompt = (
            self.prompt_template.replace("{text}", text)
            .replace("{now_local}", now_local.isoformat())
            .replace("{conversation_context}", context_text)
        )
        data = self.ollama_client.generate_json(
            prompt,
            model=self.model,
            timeout_seconds=self.settings.intent_timeout_seconds,
            options={
                "temperature": self.settings.ollama_intent_temperature,
                "top_p": 0.9,
            },
            retries=max(1, self.settings.intent_retries),
        )
        if not isinstance(data, dict):
            raise LlmProviderError("intent provider returned non-dict payload")
        return data

    def parse_intent(self, text: str, timezone_name: str, context_messages: list[str]) -> IntentLite:
        data = self.parse_intent_raw(text, timezone_name, context_messages)
        if not isinstance(data, dict):
            raise LlmProviderError("intent provider returned non-dict payload")
        return IntentLite.model_validate(data)

    def healthcheck(self) -> tuple[bool, str | None]:
        if not self.ollama_client.healthcheck():
            return False, "ollama_unavailable"
        return True, None

    @staticmethod
    def _format_context(context_messages: list[str]) -> str:
        cleaned = [msg.strip() for msg in context_messages if msg and msg.strip()]
        if not cleaned:
            return "（无）"
        return "\n".join(f"- {msg}" for msg in cleaned)


class ExternalIntentProvider:
    name = "external_intent"
    mode = "external"
    prompt_version = "intent_external_placeholder"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.intent_model or "external-intent"

    def parse_intent(self, text: str, timezone_name: str, context_messages: list[str]) -> IntentLite:
        raise LlmProviderError("external intent provider reserved but not implemented in current phase")

    def healthcheck(self) -> tuple[bool, str | None]:
        missing = []
        if not self.settings.intent_external_base_url:
            missing.append("INTENT_EXTERNAL_BASE_URL")
        if not self.settings.intent_external_api_key:
            missing.append("INTENT_EXTERNAL_API_KEY")
        if missing:
            return False, f"external_intent_config_missing:{','.join(missing)}"
        return False, "external_intent_placeholder_not_implemented"


class LocalOllamaReplyProvider:
    name = "ollama"
    mode = "local"
    prompt_version = "reply_v2_assistant"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        ollama_client: OllamaClient | None = None,
        prompt_template: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ollama_client = ollama_client or OllamaClient()
        self.prompt_template = prompt_template or load_reply_prompt_template()
        self.model = self.settings.reply_model or self.settings.ollama_nlg_model or self.settings.ollama_model

    def generate_reply(self, event_type: str, facts: dict, fallback: str) -> NlgResult:
        prompt = (
            self.prompt_template.replace("{event_type}", event_type)
            .replace("{facts_json}", orjson.dumps(facts, option=orjson.OPT_INDENT_2).decode("utf-8"))
            .replace("{fallback_text}", fallback)
        )
        started = perf_counter()
        raw_reply = self.ollama_client.generate_text(
            prompt,
            model=self.model,
            timeout_seconds=self.settings.reply_timeout_seconds,
            options={
                "temperature": self.settings.ollama_nlg_temperature,
                "top_p": 0.9,
            },
            retries=max(1, self.settings.reply_retries),
        )
        reply = self._clean_reply(raw_reply)
        used_fallback = False
        if not reply:
            # If model output is malformed/empty, fall back to deterministic text.
            reply = fallback.strip()
            used_fallback = True
        if not reply:
            raise LlmProviderError("reply provider returned empty reply")
        latency_ms = int((perf_counter() - started) * 1000)
        return NlgResult(
            reply=reply,
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            request_id=None,
            used_fallback=used_fallback,
        )

    @staticmethod
    def _clean_reply(raw_text: str) -> str:
        text = (raw_text or "").strip()
        if not text:
            return ""

        # Remove fenced code wrappers if present.
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

        # If model still returns JSON, try extracting reply field.
        if text.startswith("{") and text.endswith("}"):
            try:
                data = orjson.loads(text)
                if isinstance(data, dict) and "reply" in data:
                    text = str(data["reply"]).strip()
            except Exception:
                pass

        # Remove common "reply:" prefix noise.
        text = re.sub(r"^(reply|回复)\s*[:：]\s*", "", text, flags=re.IGNORECASE).strip()
        return text

    def healthcheck(self) -> tuple[bool, str | None]:
        if not self.ollama_client.healthcheck():
            return False, "ollama_unavailable"
        return True, None


class ExternalReplyProvider:
    name = "external_reply"
    mode = "external"
    prompt_version = "reply_external_placeholder"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.reply_model or "external-reply"

    def generate_reply(self, event_type: str, facts: dict, fallback: str) -> NlgResult:
        raise LlmProviderError("external reply provider reserved but not implemented in current phase")

    def healthcheck(self) -> tuple[bool, str | None]:
        missing = []
        if not self.settings.reply_external_base_url:
            missing.append("REPLY_EXTERNAL_BASE_URL")
        if not self.settings.reply_external_api_key:
            missing.append("REPLY_EXTERNAL_API_KEY")
        if missing:
            return False, f"external_reply_config_missing:{','.join(missing)}"
        return False, "external_reply_placeholder_not_implemented"


def load_reply_prompt_template() -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "reply_nlg_v1.txt"
    return prompt_path.read_text(encoding="utf-8")
