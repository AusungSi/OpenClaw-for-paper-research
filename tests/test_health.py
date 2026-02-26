from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.health import router as health_router
from app.infra.db import get_db


class DummyScheduler:
    started = True


class DummyOllama:
    def healthcheck(self):
        return True


class DummyWeCom:
    def last_send_status(self):
        return False, "external:wecom_60020:not allow to access from your ip"


class DummyIngest:
    webhook_dedup_ok = True


class DummyIntent:
    last_error = None

    def health_status(self):
        return True, "ollama", None

    def capability(self):
        return {
            "enabled": True,
            "provider": "ollama",
            "model": "qwen3:8b",
            "mode": "local",
            "fallback_enabled": True,
        }


class DummyAsr:
    def health_status(self):
        return True, "local", None

    def capability(self):
        return {
            "enabled": True,
            "provider": "local",
            "model": "large-v3",
            "mode": "local",
            "fallback_enabled": True,
        }


class DummyReplyGeneration:
    last_error = None

    def health_status(self):
        return True, "ollama", None

    def capability(self):
        return {
            "enabled": True,
            "provider": "ollama",
            "model": "qwen3:8b",
            "mode": "local",
            "fallback_enabled": True,
        }


def fake_db():
    class _DB:
        def execute(self, _):
            return 1

    yield _DB()


def test_health_extended_fields():
    app = FastAPI()
    app.state.scheduler_service = DummyScheduler()
    app.state.ollama_client = DummyOllama()
    app.state.wecom_client = DummyWeCom()
    app.state.message_ingest_service = DummyIngest()
    app.state.intent_service = DummyIntent()
    app.state.asr_service = DummyAsr()
    app.state.reply_generation_service = DummyReplyGeneration()
    app.include_router(health_router)
    app.dependency_overrides[get_db] = fake_db

    client = TestClient(app)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db_ok"] is True
    assert body["ollama_ok"] is True
    assert body["scheduler_ok"] is True
    assert body["wecom_send_ok"] is False
    assert "wecom_60020" in body["wecom_last_error"]
    assert body["webhook_dedup_ok"] is True
    assert body["asr_ok"] is True
    assert body["asr_provider"] == "local"
    assert body["asr_last_error"] is None
    assert body["nlg_last_error"] is None
    assert body["intent_provider_ok"] is True
    assert body["intent_provider_name"] == "ollama"
    assert body["reply_provider_ok"] is True
    assert body["reply_provider_name"] == "ollama"
    assert body["asr_provider_ok"] is True
    assert body["asr_provider_name"] == "local"


def test_capabilities_endpoint():
    app = FastAPI()
    app.state.scheduler_service = DummyScheduler()
    app.state.ollama_client = DummyOllama()
    app.state.wecom_client = DummyWeCom()
    app.state.message_ingest_service = DummyIngest()
    app.state.intent_service = DummyIntent()
    app.state.asr_service = DummyAsr()
    app.state.reply_generation_service = DummyReplyGeneration()
    app.include_router(health_router)

    client = TestClient(app)
    resp = client.get("/api/v1/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"]["provider"] == "ollama"
    assert body["reply"]["provider"] == "ollama"
    assert body["asr"]["provider"] == "local"
