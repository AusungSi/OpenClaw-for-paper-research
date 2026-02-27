from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import orjson

from app.core.config import get_settings
from app.core.logging import get_logger


logger = get_logger("ollama")


class OllamaClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def generate_text(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
        options: dict[str, Any] | None = None,
        retries: int = 2,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
        }
        if options:
            payload["options"] = options
        data = self._request_generate(payload, timeout_seconds=timeout_seconds, retries=retries)
        return str(data.get("response", "")).strip()

    def generate_json(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
        options: dict[str, Any] | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
        if options:
            payload["options"] = options

        data = self._request_generate(payload, timeout_seconds=timeout_seconds, retries=retries)
        raw = data.get("response", "{}")
        return orjson.loads(raw)

    def _request_generate(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: int | None,
        retries: int,
    ) -> dict[str, Any]:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/generate"
        last_error: Exception | None = None
        for _ in range(max(1, retries)):
            try:
                with httpx.Client(timeout=timeout_seconds or self.settings.ollama_timeout_seconds) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    return response.json()
            except Exception as exc:
                last_error = exc
                logger.warning("ollama_generate_failed error=%s", exc)
        if last_error:
            raise last_error
        return {"response": ""}

    def healthcheck(self) -> bool:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/tags"
        try:
            with httpx.Client(timeout=5) as client:
                response = client.get(url)
                response.raise_for_status()
            return True
        except Exception:
            return False


def load_prompt_template() -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "intent_v1.txt"
    return prompt_path.read_text(encoding="utf-8")
