from __future__ import annotations

import re

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
        required = [draft.content] if draft.content else []
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
        required = [content, "提醒"]
        return self._generate("add_success", facts, fallback, required)

    def generate_query_summary(self, items: list[tuple[int, str, str]], fallback: str) -> str:
        facts = {
            "operation": OperationType.QUERY.value,
            "items": [{"id": item_id, "content": content, "when": when_text} for item_id, content, when_text in items],
            "count": len(items),
        }
        required = []
        if items:
            required = ["提醒"]
            if items[0][1]:
                required.append(items[0][1])
        else:
            required = ["没有", "提醒"]
        return self._generate("query_summary", facts, fallback, required)

    def generate_delete_success(self, *, content: str, fallback: str) -> str:
        facts = {
            "operation": OperationType.DELETE.value,
            "content": content,
            "status": "deleted",
        }
        required = [content]
        return self._generate("delete_success", facts, fallback, required)

    def generate_update_success(self, *, content: str, when_text: str | None, fallback: str) -> str:
        facts = {
            "operation": OperationType.UPDATE.value,
            "content": content,
            "time_text": when_text,
            "status": "updated",
        }
        required = [content]
        if when_text:
            required.append(when_text[:10])
        return self._generate("update_success", facts, fallback, required)

    def generate_not_found_delete(self, *, keyword: str, fallback: str) -> str:
        facts = {
            "operation": OperationType.DELETE.value,
            "keyword": keyword,
            "status": "not_found",
        }
        required = [keyword] if keyword else []
        return self._generate("not_found_delete", facts, fallback, required)

    def generate_not_found_update(self, *, keyword: str, fallback: str) -> str:
        facts = {
            "operation": OperationType.UPDATE.value,
            "keyword": keyword,
            "status": "not_found",
        }
        required = [keyword] if keyword else []
        return self._generate("not_found_update", facts, fallback, required)

    def generate_event_reply(
        self,
        *,
        event_type: str,
        facts: dict,
        fallback: str,
        required_keywords: list[str] | None = None,
    ) -> str:
        return self._generate(event_type, facts, fallback, required_keywords or [])

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
                if not self._passes_event_validation(event_type, facts, reply):
                    error = "event_fact_validation_failed"
                    continue
                self._last_error = None
                self._last_provider = provider.name
                logger.info(
                    "reply_generation_succeeded nlg_event=%s provider=%s prompt_version=%s",
                    event_type,
                    provider.name,
                    getattr(provider, "prompt_version", "unknown"),
                )
                return reply
            except LlmProviderError as exc:
                error = str(exc)
                self._last_error = f"nlg_provider_failed:{provider.name}:{exc}"
                logger.warning(
                    "reply_generation_provider_failed nlg_event=%s provider=%s prompt_version=%s error=%s",
                    event_type,
                    provider.name,
                    getattr(provider, "prompt_version", "unknown"),
                    exc,
                )
            except Exception as exc:
                error = str(exc)
                self._last_error = f"nlg_provider_failed:{provider.name}:{exc}"
                logger.warning(
                    "reply_generation_provider_failed nlg_event=%s provider=%s prompt_version=%s error=%s",
                    event_type,
                    provider.name,
                    getattr(provider, "prompt_version", "unknown"),
                    exc,
                )
        self._last_error = f"nlg_failed:{event_type}:{error}"
        logger.warning("reply_generation_failed nlg_event=%s error=%s", event_type, error)
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
        normalized = ReplyGenerationService._normalize_text(reply)
        for keyword in required_keywords:
            key = keyword.strip()
            if len(key) < 2:
                continue
            normalized_key = ReplyGenerationService._normalize_text(key)
            if normalized_key not in normalized:
                return False
        return True

    @staticmethod
    def _normalize_text(text: str) -> str:
        value = (text or "").lower()
        value = re.sub(r"[\s\u3000]+", "", value)
        value = re.sub(r"[，。、“”‘’：:；;！!？?\-_/\\()\[\]{}<>]", "", value)
        return value

    @staticmethod
    def _has_time_signal(text: str) -> bool:
        if not text:
            return False
        if re.search(r"\d{1,2}[:：点]\d{0,2}", text):
            return True
        keywords = ("今天", "明天", "后天", "周", "星期", "每周", "每月", "每年", "上午", "下午", "晚上")
        return any(k in text for k in keywords)

    @classmethod
    def _passes_event_validation(cls, event_type: str, facts: dict, reply: str) -> bool:
        # Event-level guard to reduce false fallback while keeping factual anchors.
        normalized = cls._normalize_text(reply)

        if event_type == "confirmation_prompt":
            if "确认" not in reply:
                return False
            content = str(facts.get("content") or "").strip()
            if content and cls._normalize_text(content) not in normalized:
                return False
            run_at_local = str(facts.get("run_at_local") or "").strip()
            if run_at_local and not cls._has_time_signal(reply):
                return False
            return True

        if event_type == "add_success":
            content = str(facts.get("content") or "").strip()
            if content and cls._normalize_text(content) not in normalized:
                return False
            if "提醒" not in reply:
                return False
            time_text = str(facts.get("time_text") or "").strip()
            if time_text and not cls._has_time_signal(reply):
                return False
            return True

        if event_type == "query_summary":
            if "提醒" not in reply and "事项" not in reply and "任务" not in reply:
                return False
            items = facts.get("items") or []
            if items:
                first_content = str(items[0].get("content") or "").strip()
                if first_content and cls._normalize_text(first_content) not in normalized and not cls._has_time_signal(reply):
                    return False
            return True

        if event_type in {"delete_success", "not_found_delete"}:
            if "删" not in reply:
                return False
            return True

        if event_type in {"update_success", "not_found_update"}:
            if "改" not in reply and "更新" not in reply:
                return False
            return True

        return True
