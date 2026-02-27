from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from app.core.logging import get_logger
from app.core.config import get_settings
from app.domain.enums import OperationType, ScheduleType
from app.domain.schemas import IntentDraft, IntentLite
from app.llm.ollama_client import OllamaClient, load_prompt_template
from app.llm.providers import IntentLLMProvider, LlmProviderError, LocalOllamaIntentProvider


logger = get_logger("intent")


class IntentService:
    def __init__(
        self,
        *,
        intent_providers: list[IntentLLMProvider] | None = None,
        ollama_client: OllamaClient | None = None,
    ) -> None:
        self.settings = get_settings()
        self.prompt_template = load_prompt_template()
        if intent_providers:
            self.intent_providers = intent_providers
        else:
            self.intent_providers = [
                LocalOllamaIntentProvider(
                    settings=self.settings,
                    ollama_client=ollama_client or OllamaClient(),
                    prompt_template=self.prompt_template,
                )
            ]
        self._last_error: str | None = None

    def parse_intent(self, text: str, timezone_name: str, context_messages: list[str] | None = None) -> IntentDraft:
        now_local = datetime.now(ZoneInfo(timezone_name))
        providers = self.intent_providers if self.settings.intent_fallback_enabled else self.intent_providers[:1]
        for provider in providers:
            try:
                lite = provider.parse_intent(text, timezone_name, context_messages or [])
                logger.info(
                    "intent_parsed intent_stage=lite provider=%s prompt_version=%s operation=%s",
                    provider.name,
                    getattr(provider, "prompt_version", "unknown"),
                    lite.operation.value,
                )
                draft = self.normalize_intent_lite(lite, text=text, timezone_name=timezone_name, now_local=now_local)
                logger.info(
                    "intent_parsed intent_stage=normalized provider=%s operation=%s schedule=%s",
                    provider.name,
                    draft.operation.value,
                    draft.schedule.value if draft.schedule else "null",
                )
                self._last_error = None
                return draft
            except LlmProviderError as exc:
                self._last_error = str(exc)
                logger.warning("intent_provider_failed provider=%s error=%s", provider.name, exc)
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("intent_provider_failed provider=%s error=%s", provider.name, exc)
        draft = self._parse_fallback(text, timezone_name, now_local)
        logger.info("intent_parsed intent_stage=normalized provider=fallback operation=%s", draft.operation.value)
        return draft

    def normalize_intent_lite(
        self,
        lite: IntentLite,
        *,
        text: str,
        timezone_name: str,
        now_local: datetime | None = None,
    ) -> IntentDraft:
        now = now_local or datetime.now(ZoneInfo(timezone_name))
        operation = lite.operation
        raw_content = (lite.content or "").strip()
        extracted_content = self._extract_content(text.strip(), operation)
        content = raw_content or extracted_content
        if operation == OperationType.QUERY and content == "未命名提醒":
            content = ""

        schedule: ScheduleType | None = None
        run_at_local: str | None = None
        rrule: str | None = None
        clarification_question = (lite.clarification_question or "").strip() or None

        if operation in (OperationType.DELETE, OperationType.UPDATE) and (not content or self._looks_like_time_phrase(content)):
            clarification_question = clarification_question or "你想删除或修改哪一条提醒？可以发关键词或编号。"

        if operation in (OperationType.ADD, OperationType.UPDATE):
            time_source = self._compose_time_source(text=text, when_text=lite.when_text)
            rrule = self._build_rrule(time_source)
            has_time = self._has_time_expression(time_source) or bool(rrule)
            if not has_time:
                clarification_question = clarification_question or "你想让我什么时候提醒你？比如：明天早上9点。"
            else:
                parsed_dt = self._extract_datetime(time_source, now)
                if rrule:
                    schedule = ScheduleType.RRULE
                    run_at_local = parsed_dt.isoformat()
                else:
                    schedule = ScheduleType.ONE_TIME
                    run_at_local = parsed_dt.isoformat()

        if operation == OperationType.QUERY:
            clarification_question = None

        return IntentDraft(
            operation=operation,
            content=content,
            timezone=timezone_name,
            schedule=schedule,
            run_at_local=run_at_local,
            rrule=rrule,
            confidence=lite.confidence,
            needs_confirmation=operation != OperationType.QUERY,
            clarification_question=clarification_question,
        )

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def health_status(self) -> tuple[bool, str, str | None]:
        provider = self.intent_providers[0]
        ok, error = provider.healthcheck()
        return ok, provider.name, error

    def capability(self) -> dict[str, str | bool | None]:
        provider = self.intent_providers[0]
        return {
            "enabled": True,
            "provider": provider.name,
            "model": getattr(provider, "model", None),
            "mode": getattr(provider, "mode", None),
            "fallback_enabled": self.settings.intent_fallback_enabled,
        }

    def _parse_fallback(self, text: str, timezone_name: str, now_local: datetime) -> IntentDraft:
        cleaned = text.strip()
        operation = self._guess_operation(cleaned)
        content = self._extract_content(cleaned, operation)
        schedule: ScheduleType | None = None
        run_at_local: str | None = None
        rrule: str | None = None
        clarification_question: str | None = None

        if operation in (OperationType.ADD, OperationType.UPDATE):
            rrule = self._build_rrule(cleaned)
            has_time = self._has_time_expression(cleaned) or bool(rrule)
            if not has_time:
                clarification_question = "请补充具体提醒时间，例如：明天早上9点。"
            else:
                parsed_dt = self._extract_datetime(cleaned, now_local)
                if rrule:
                    schedule = ScheduleType.RRULE
                    run_at_local = parsed_dt.isoformat()
                else:
                    schedule = ScheduleType.ONE_TIME
                    run_at_local = parsed_dt.isoformat()

        confidence = 0.65 if clarification_question is None else 0.4
        needs_confirmation = operation != OperationType.QUERY
        return IntentDraft(
            operation=operation,
            content=content,
            timezone=timezone_name,
            schedule=schedule,
            run_at_local=run_at_local,
            rrule=rrule,
            confidence=confidence,
            needs_confirmation=needs_confirmation,
            clarification_question=clarification_question,
        )

    @staticmethod
    def _guess_operation(text: str) -> OperationType:
        lower = text.lower()
        if any(k in lower for k in ("查询", "看看", "有哪些", "list", "query")):
            return OperationType.QUERY
        if any(k in lower for k in ("删除", "取消提醒", "remove", "delete")):
            return OperationType.DELETE
        if any(k in lower for k in ("修改", "改成", "延后", "update")):
            return OperationType.UPDATE
        return OperationType.ADD

    @staticmethod
    def _extract_content(text: str, operation: OperationType) -> str:
        content = text
        patterns = {
            OperationType.ADD: r"^(提醒我|帮我记|记得|memo)\s*",
            OperationType.DELETE: r"^(删除|取消提醒|remove|delete)\s*",
            OperationType.UPDATE: r"^(修改|改成|update)\s*",
            OperationType.QUERY: r"^(查询|看看|list|query)\s*",
        }
        pattern = patterns.get(operation)
        if pattern:
            content = re.sub(pattern, "", content, flags=re.IGNORECASE).strip()
        return content or "未命名提醒"

    def _extract_datetime(self, text: str, now_local: datetime) -> datetime:
        base_date = now_local.date()
        if "明天" in text:
            base_date = (now_local + timedelta(days=1)).date()
        elif "后天" in text:
            base_date = (now_local + timedelta(days=2)).date()
        elif "大后天" in text:
            base_date = (now_local + timedelta(days=3)).date()
        else:
            weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            week_match = re.search(r"下周([一二三四五六日天])", text)
            if week_match:
                target = weekday_map[week_match.group(1)]
                diff = (7 - now_local.weekday() + target) % 7
                diff = 7 if diff == 0 else diff
                base_date = (now_local + timedelta(days=diff)).date()

        hour = 9
        minute = 0
        hm = re.search(r"(\d{1,2})(?:点|:|：)(\d{1,2})?", text)
        if hm:
            hour = int(hm.group(1))
            minute = int(hm.group(2) or 0)
        else:
            only_hour = re.search(r"(\d{1,2})点", text)
            if only_hour:
                hour = int(only_hour.group(1))

        if any(k in text for k in ("下午", "晚上")) and hour < 12:
            hour += 12
        if "中午" in text and hour < 11:
            hour += 12

        candidate = datetime(
            year=base_date.year,
            month=base_date.month,
            day=base_date.day,
            hour=hour,
            minute=minute,
            tzinfo=now_local.tzinfo,
        )
        if "今天" in text and candidate < now_local:
            candidate += timedelta(days=1)

        explicit = re.search(r"\d{4}-\d{1,2}-\d{1,2}", text)
        if explicit:
            dt = date_parser.parse(explicit.group(0))
            candidate = candidate.replace(year=dt.year, month=dt.month, day=dt.day)

        return candidate

    @staticmethod
    def _build_rrule(text: str) -> str | None:
        if "每月最后一个工作日" in text:
            return "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"
        if "每天" in text or "每日" in text:
            return "FREQ=DAILY"
        weekday_map = {"一": "MO", "二": "TU", "三": "WE", "四": "TH", "五": "FR", "六": "SA", "日": "SU", "天": "SU"}
        week_match = re.search(r"每周([一二三四五六日天])", text)
        if week_match:
            return f"FREQ=WEEKLY;BYDAY={weekday_map[week_match.group(1)]}"
        if "每月" in text:
            return "FREQ=MONTHLY"
        if "每年" in text:
            return "FREQ=YEARLY"
        return None

    @staticmethod
    def _compose_time_source(*, text: str, when_text: str | None) -> str:
        parts = []
        if when_text and when_text.strip():
            parts.append(when_text.strip())
        if text and text.strip():
            parts.append(text.strip())
        return " ".join(parts).strip()

    @staticmethod
    def _has_time_expression(text: str) -> bool:
        if not text:
            return False
        keywords = (
            "今天",
            "明天",
            "后天",
            "大后天",
            "上午",
            "中午",
            "下午",
            "晚上",
            "早上",
            "凌晨",
            "下周",
            "每周",
            "每月",
            "每年",
            "每天",
            "每日",
        )
        if any(k in text for k in keywords):
            return True
        return re.search(r"\d{1,2}[:：点]\d{0,2}", text) is not None

    @classmethod
    def _looks_like_time_phrase(cls, text: str) -> bool:
        value = (text or "").strip()
        if not value:
            return False
        if len(value) <= 2:
            return False
        return cls._has_time_expression(value)
