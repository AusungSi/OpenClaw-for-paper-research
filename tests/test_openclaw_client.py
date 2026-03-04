from __future__ import annotations

import pytest
import httpx

from app.core.config import Settings
from app.llm.openclaw_client import LLMCallResult, LLMTaskType, OpenClawClient, OpenClawClientError


def _build_settings() -> Settings:
    return Settings(
        openclaw_enabled=True,
        openclaw_base_url="http://127.0.0.1:18789",
        openclaw_agent_id="memomate",
        openclaw_retries=1,
        openclaw_timeout_seconds=3,
    )


def test_chat_completion_http_success(monkeypatch):
    class DummyResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "{\"ok\":true}"}}]}

    class DummyClient:
        def __init__(self, timeout, trust_env=True):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers, json):
            assert url.endswith("/v1/chat/completions")
            assert json["model"].startswith("openclaw:")
            return DummyResponse()

    monkeypatch.setattr("app.llm.openclaw_client.httpx.Client", DummyClient)

    client = OpenClawClient(settings=_build_settings())
    result = client.chat_completion(task_type=LLMTaskType.INTENT_PARSE, prompt="hello")

    assert result.text == '{"ok":true}'
    assert result.provider == "openclaw_http"
    assert result.via_fallback is False
    assert client.openclaw_http_ok == 1


def test_chat_completion_timeout_falls_back_to_cli(monkeypatch):
    client = OpenClawClient(settings=_build_settings())

    def raise_timeout(**kwargs):
        raise httpx.TimeoutException("timeout")

    def fake_cli_fallback(**kwargs):
        return LLMCallResult(
            text='{"operation":"add","content":"开会","when_text":"明天9点","confidence":0.9,"clarification_question":null}',
            provider="openclaw_cli",
            model="openclaw:memomate",
            latency_ms=8,
            via_fallback=True,
        )

    monkeypatch.setattr(client, "_chat_completion_http", raise_timeout)
    monkeypatch.setattr(client, "chat_completion_cli_fallback", fake_cli_fallback)

    result = client.chat_completion(task_type=LLMTaskType.INTENT_PARSE, prompt="提醒我明天9点开会")

    assert result.via_fallback is True
    assert result.provider == "openclaw_cli"
    assert client.openclaw_cli_fallback_count == 1


def test_chat_completion_http_401_does_not_fallback(monkeypatch):
    client = OpenClawClient(settings=_build_settings())

    def raise_401(**kwargs):
        raise OpenClawClientError(
            "openclaw_http_4xx",
            "http_401:unauthorized",
            retriable=False,
            status_code=401,
        )

    called = {"fallback": False}

    def fake_cli_fallback(**kwargs):
        called["fallback"] = True
        raise AssertionError("fallback should not be called on 4xx")

    monkeypatch.setattr(client, "_chat_completion_http", raise_401)
    monkeypatch.setattr(client, "chat_completion_cli_fallback", fake_cli_fallback)

    with pytest.raises(OpenClawClientError) as exc:
        client.chat_completion(task_type=LLMTaskType.INTENT_PARSE, prompt="hello")

    assert exc.value.status_code == 401
    assert called["fallback"] is False


def test_local_openclaw_http_bypasses_proxy_env(monkeypatch):
    captured = {"trust_env": None}

    class DummyResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}}]}

    class DummyClient:
        def __init__(self, timeout, trust_env):
            captured["trust_env"] = trust_env

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers, json):
            return DummyResponse()

    monkeypatch.setattr("app.llm.openclaw_client.httpx.Client", DummyClient)

    settings = Settings(
        openclaw_enabled=True,
        openclaw_base_url="http://127.0.0.1:18789",
        openclaw_gateway_token="x",
        openclaw_agent_id="memomate",
        openclaw_retries=1,
    )
    client = OpenClawClient(settings=settings)
    _ = client.chat_completion(task_type=LLMTaskType.INTENT_PARSE, prompt="ping")
    assert captured["trust_env"] is False
