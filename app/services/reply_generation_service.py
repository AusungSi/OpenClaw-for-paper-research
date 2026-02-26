from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.enums import OperationType
from app.domain.schemas import IntentDraft
from app.llm.ollama_client import OllamaClient
from app.llm.providers import LlmProviderError, LocalOllamaReplyProvider, ReplyLLMProvider


logger = get_logger("reply_generation")


class ReplyGenerationService:
    def __init__(
        self,
        *,
        reply_providers: list[ReplyLLMProvider] | None = None,
        ollama_client: OllamaClient | None = None,
    ) -> None:
        self.settings = get_settings()
        if reply_providers:
            self.reply_providers = reply_providers
        else:
            self.reply_providers = [
                LocalOllamaReplyProvider(
                    settings=self.settings,
                    ollama_client=ollama_client or OllamaClient(),
                )
            ]
        self._last_error: str | None = None
        self._last_provider: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def generate_confirmation_prompt(self, draft: IntentDraft, fallback: str) -> str:
        facts = {
            "operation": draft.operation.value,
            "content": draft.content,
            "timezone": draft.timezone,
            "schedule": draft.schedule.value if draft.schedule else None,
            "run_at_local": draft.run_at_local,
            "rrule": draft.rrule,
        }
        required = [draft.content]
        if draft.run_at_local:
            required.append(draft.run_at_local[:10])
        if draft.operation in (OperationType.ADD, OperationType.UPDATE):
            required.append("确认")
        return self._generate("confirmation_prompt", facts, fallback, required)

    def generate_add_success(
        self,
        *,
        content: str,
        when_text: str,
        timezone: str,
        fallback: str,
    ) -> str:
        facts = {
            "operation": OperationType.ADD.value,
            "content": content,
            "time_text": when_text,
            "timezone": timezone,
            "status": "created",
        }
        required = [content, when_text[:10], "提醒"]
        return self._generate("add_success", facts, fallback, required)

    def generate_query_summary(self, items: list[tuple[int, str, str]], fallback: str) -> str:
        facts = {
            "operation": OperationType.QUERY.value,
            "items": [{"id": item_id, "content": content, "when": when_text} for item_id, content, when_text in items],
            "count": len(items),
        }
        required = []
        if items:
            required = [items[0][1], "提醒"]
        else:
            required = ["没有", "提醒"]
        return self._generate("query_summary", facts, fallback, required)

    def _generate(self, event_type: str, facts: dict, fallback: str, required_keywords: list[str]) -> str:
        providers = self.reply_providers if self.settings.reply_fallback_enabled else self.reply_providers[:1]
        error: str | None = None
        for provider in providers:
            try:
                result = provider.generate_reply(event_type, facts, fallback)
                reply = result.reply.strip()
                if not reply:
                    error = "empty_reply"
                    continue
                if not self._contains_required_facts(reply, required_keywords):
                    error = "missing_required_facts"
                    continue
                self._last_error = None
                self._last_provider = provider.name
                return reply
            except LlmProviderError as exc:
                error = str(exc)
                self._last_error = f"nlg_provider_failed:{provider.name}:{exc}"
                logger.warning("reply_generation_provider_failed provider=%s error=%s", provider.name, exc)
            except Exception as exc:
                error = str(exc)
                self._last_error = f"nlg_provider_failed:{provider.name}:{exc}"
                logger.warning("reply_generation_provider_failed provider=%s error=%s", provider.name, exc)
        self._last_error = f"nlg_failed:{event_type}:{error}"
        logger.warning("reply_generation_failed event_type=%s error=%s", event_type, error)
        return fallback

    def health_status(self) -> tuple[bool, str, str | None]:
        provider = self.reply_providers[0]
        ok, error = provider.healthcheck()
        return ok, provider.name, error

    def capability(self) -> dict[str, str | bool | None]:
        provider = self.reply_providers[0]
        return {
            "enabled": True,
            "provider": provider.name,
            "model": getattr(provider, "model", None),
            "mode": getattr(provider, "mode", None),
            "fallback_enabled": self.settings.reply_fallback_enabled,
        }

    @staticmethod
    def _contains_required_facts(reply: str, required_keywords: list[str]) -> bool:
        normalized = reply.replace(" ", "")
        for keyword in required_keywords:
            key = keyword.strip()
            if len(key) < 2:
                continue
            if key.replace(" ", "") not in normalized:
                return False
        return True
