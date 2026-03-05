from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import perf_counter, sleep
import subprocess
from urllib.parse import urlparse

import httpx
import orjson

from app.core.config import Settings, get_settings
from app.core.logging import get_logger


logger = get_logger("openclaw")


class LLMTaskType(str, Enum):
    INTENT_PARSE = "intent_parse"
    RESEARCH_PLAN = "research_plan"
    ABSTRACT_SUMMARIZE = "abstract_summarize"
    PAPER_KEYPOINTS = "paper_keypoints"


@dataclass
class LLMCallResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    via_fallback: bool = False


class OpenClawClientError(Exception):
    def __init__(self, code: str, message: str, *, retriable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable
        self.status_code = status_code


class OpenClawClient:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.model = f"openclaw:{self.settings.openclaw_agent_id}"
        self.openclaw_http_ok = 0
        self.openclaw_http_fail = 0
        self.openclaw_cli_fallback_count = 0
        self.openclaw_latency_ms = 0

    def metrics_snapshot(self) -> dict[str, int]:
        return {
            "openclaw_http_ok": self.openclaw_http_ok,
            "openclaw_http_fail": self.openclaw_http_fail,
            "openclaw_cli_fallback_count": self.openclaw_cli_fallback_count,
            "openclaw_latency_ms": self.openclaw_latency_ms,
        }

    def chat_completion(
        self,
        *,
        task_type: LLMTaskType,
        prompt: str,
        system_prompt: str | None = None,
        user: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMCallResult:
        if not self.settings.openclaw_enabled:
            raise OpenClawClientError(
                "openclaw_disabled",
                "openclaw is disabled",
                retriable=False,
                status_code=503,
            )
        retries = max(1, self.settings.openclaw_retries)
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                return self._chat_completion_http(
                    task_type=task_type,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    user=user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except OpenClawClientError as exc:
                self.openclaw_http_fail += 1
                last_error = exc
                if not exc.retriable or attempt >= retries - 1:
                    if exc.status_code is not None and 400 <= exc.status_code < 500:
                        # 4xx is explicit config/auth/request error: no CLI fallback.
                        raise
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.openclaw_http_fail += 1
                last_error = exc
                if attempt >= retries - 1:
                    break
            if attempt < retries - 1:
                sleep(min(1.6, 0.2 * (2**attempt)))

        if not self.settings.openclaw_cli_fallback_enabled:
            err = str(last_error) if last_error else "unknown_openclaw_error"
            raise OpenClawClientError(
                "openclaw_http_failed",
                f"http_failed_no_cli_fallback:{err}",
                retriable=False,
            )

        self.openclaw_cli_fallback_count += 1
        try:
            return self.chat_completion_cli_fallback(
                task_type=task_type,
                prompt=prompt,
                system_prompt=system_prompt,
                user=user,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            err = str(last_error) if last_error else "unknown_openclaw_error"
            raise OpenClawClientError(
                "openclaw_unavailable",
                f"http_then_cli_failed:http={err};cli={exc}",
                retriable=False,
            ) from exc

    def _chat_completion_http(
        self,
        *,
        task_type: LLMTaskType,
        prompt: str,
        system_prompt: str | None,
        user: str | None,
        temperature: float,
        max_tokens: int,
    ) -> LLMCallResult:
        url = f"{self.settings.openclaw_base_url.rstrip('/')}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        token = self.settings.openclaw_gateway_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        messages: list[dict[str, str]] = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": prompt.strip()})
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if user and user.strip():
            payload["user"] = user.strip()

        started = perf_counter()
        trust_env = not _is_local_openclaw_base(self.settings.openclaw_base_url)
        with httpx.Client(timeout=max(1, self.settings.openclaw_timeout_seconds), trust_env=trust_env) as client:
            resp = client.post(url, headers=headers, json=payload)

        if resp.status_code >= 400:
            message = _error_message(resp)
            if 400 <= resp.status_code < 500:
                raise OpenClawClientError(
                    "openclaw_http_4xx",
                    f"http_{resp.status_code}:{message}",
                    retriable=False,
                    status_code=resp.status_code,
                )
            raise OpenClawClientError(
                "openclaw_http_5xx",
                f"http_{resp.status_code}:{message}",
                retriable=True,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise OpenClawClientError(
                "openclaw_http_bad_json",
                f"invalid_http_json:{exc}",
                retriable=False,
            ) from exc
        text = _extract_chat_content(data)
        latency_ms = int((perf_counter() - started) * 1000)
        self.openclaw_http_ok += 1
        self.openclaw_latency_ms = latency_ms
        logger.info("openclaw_http_ok task=%s latency_ms=%s", task_type.value, latency_ms)
        return LLMCallResult(
            text=text,
            provider="openclaw_http",
            model=self.model,
            latency_ms=latency_ms,
            via_fallback=False,
        )

    def chat_completion_cli_fallback(
        self,
        *,
        task_type: LLMTaskType,
        prompt: str,
        system_prompt: str | None,
        user: str | None,
        max_tokens: int,
    ) -> LLMCallResult:
        message = prompt.strip()
        if system_prompt and system_prompt.strip():
            message = f"{system_prompt.strip()}\n\n{message}"
        if user and user.strip():
            message = f"[session_user:{user.strip()}]\n{message}"
        if max_tokens > 0:
            message = f"{message}\n\n[output_limit_hint:{max_tokens}]"

        cmd = [
            str(Path(self.settings.openclaw_cli_path).expanduser()),
            "agent",
            "--agent",
            self.settings.openclaw_agent_id,
            "--local",
            "--message",
            message,
            "--json",
            "--timeout",
            str(max(5, self.settings.openclaw_timeout_seconds)),
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            started = perf_counter()
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=max(6, self.settings.openclaw_timeout_seconds + 5),
                    check=False,
                )
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    sleep(0.15)
                    continue
                break

            if completed.returncode != 0:
                last_error = OpenClawClientError(
                    "openclaw_cli_non_zero",
                    f"cli_return_code={completed.returncode}",
                    retriable=(attempt == 0),
                )
                if attempt == 0:
                    sleep(0.15)
                    continue
                break
            try:
                payload = _extract_json_from_output(completed.stdout)
                text = _extract_cli_text(payload)
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    sleep(0.15)
                    continue
                break
            latency_ms = int((perf_counter() - started) * 1000)
            logger.warning("openclaw_cli_fallback_used task=%s latency_ms=%s", task_type.value, latency_ms)
            return LLMCallResult(
                text=text,
                provider="openclaw_cli",
                model=self.model,
                latency_ms=latency_ms,
                via_fallback=True,
            )
        raise OpenClawClientError(
            "openclaw_cli_failed",
            f"cli_failed:{last_error}",
            retriable=False,
        )

    def healthcheck(self) -> tuple[bool, str | None]:
        if not self.settings.openclaw_enabled:
            return False, "openclaw_disabled"
        url = f"{self.settings.openclaw_base_url.rstrip('/')}/health"
        headers: dict[str, str] = {}
        token = self.settings.openclaw_gateway_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            trust_env = not _is_local_openclaw_base(self.settings.openclaw_base_url)
            with httpx.Client(timeout=3, trust_env=trust_env) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return True, None
            if resp.status_code == 401:
                return False, "openclaw_auth_failed"
            return False, f"openclaw_http_{resp.status_code}"
        except Exception as exc:
            return False, f"openclaw_unreachable:{exc.__class__.__name__}"


def _extract_chat_content(data: dict) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise OpenClawClientError(
            "openclaw_http_invalid_response",
            "missing choices",
            retriable=False,
        )
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        value = content.strip()
        if value:
            return value
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return "\n".join(parts)
    raise OpenClawClientError(
        "openclaw_http_empty_content",
        "response content is empty",
        retriable=False,
    )


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return resp.text[:200]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str):
                return msg[:200]
        msg = data.get("message")
        if isinstance(msg, str):
            return msg[:200]
    return str(data)[:200]


def _extract_json_from_output(output: str) -> dict:
    text = (output or "").strip()
    for idx in (i for i, c in enumerate(text) if c == "{"):
        candidate = text[idx:]
        try:
            parsed = orjson.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("openclaw cli output does not contain valid json object")


def _extract_cli_text(payload: dict) -> str:
    items = payload.get("payloads")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    raise ValueError("openclaw cli output contains no text payload")


def _is_local_openclaw_base(base_url: str) -> bool:
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}
