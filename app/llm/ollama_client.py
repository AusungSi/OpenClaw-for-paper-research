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

    def generate_json(
        self,
        prompt: str,
        *,
        model: str | None = None,
        timeout_seconds: int | None = None,
        options: dict[str, Any] | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/generate"
        payload: dict[str, Any] = {
            "model": model or self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
        if options:
            payload["options"] = options

        last_error: Exception | None = None
        for _ in range(max(1, retries)):
            try:
                with httpx.Client(timeout=timeout_seconds or self.settings.ollama_timeout_seconds) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    data = response.json()
                raw = data.get("response", "{}")
                return orjson.loads(raw)
            except Exception as exc:
                last_error = exc
                logger.warning("ollama_generate_json_failed error=%s", exc)
        if last_error:
            raise last_error
        return {}

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
