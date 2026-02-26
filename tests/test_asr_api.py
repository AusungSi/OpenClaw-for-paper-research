from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.mobile import router as mobile_router
from app.services.asr_service import AsrTimeoutError, AsrValidationError, TranscriptionResult


class DummyToken:
    def __init__(self, sub: str):
        self.sub = sub


class DummyAuthService:
    def verify_access_token(self, token: str):
        return DummyToken(sub="1")


class DummyAsrService:
    def transcribe_bytes(self, data: bytes, *, filename=None, mime_type=None, language_hint=None):
        if data == b"timeout":
            raise AsrTimeoutError("timeout")
        if data == b"invalid":
            raise AsrValidationError("invalid")
        return TranscriptionResult(
            text="这是转写文本",
            language="zh",
            provider="local",
            model="large-v3",
            request_id="req-1",
            latency_ms=88,
            used_fallback=False,
        )


def build_client() -> TestClient:
    app = FastAPI()
    app.state.mobile_auth_service = DummyAuthService()
    app.state.asr_service = DummyAsrService()
    app.state.reminder_service = object()
    app.include_router(mobile_router)
    return TestClient(app)


def test_asr_transcribe_success():
    client = build_client()
    resp = client.post(
        "/api/v1/asr/transcribe",
        headers={"Authorization": "Bearer token"},
        files={"file": ("test.wav", b"abc", "audio/wav")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "这是转写文本"
    assert body["provider"] == "local"
    assert body["model"] == "large-v3"
    assert body["request_id"] == "req-1"


def test_asr_transcribe_empty_file():
    client = build_client()
    resp = client.post(
        "/api/v1/asr/transcribe",
        headers={"Authorization": "Bearer token"},
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    assert resp.status_code == 400


def test_asr_transcribe_non_audio_file():
    client = build_client()
    resp = client.post(
        "/api/v1/asr/transcribe",
        headers={"Authorization": "Bearer token"},
        files={"file": ("a.txt", b"abc", "text/plain")},
    )
    assert resp.status_code == 400


def test_asr_transcribe_timeout():
    client = build_client()
    resp = client.post(
        "/api/v1/asr/transcribe",
        headers={"Authorization": "Bearer token"},
        files={"file": ("timeout.wav", b"timeout", "audio/wav")},
    )
    assert resp.status_code == 504
