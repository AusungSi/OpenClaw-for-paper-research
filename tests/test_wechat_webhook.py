from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import wechat as wechat_api


class DummyCrypto:
    def check_signature(self, signature, timestamp, nonce, echostr):
        return "ok"

    def decrypt_message(self, msg, signature, timestamp, nonce):
        return (
            "<xml>"
            "<ToUserName><![CDATA[toUser]]></ToUserName>"
            "<FromUserName><![CDATA[fromUser]]></FromUserName>"
            "<CreateTime>1348831860</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType>"
            "<Content><![CDATA[hello]]></Content>"
            "<MsgId>1234567890123456</MsgId>"
            "</xml>"
        )


class DummyVoiceCrypto:
    def check_signature(self, signature, timestamp, nonce, echostr):
        return "ok"

    def decrypt_message(self, msg, signature, timestamp, nonce):
        return (
            "<xml>"
            "<ToUserName><![CDATA[toUser]]></ToUserName>"
            "<FromUserName><![CDATA[fromUser]]></FromUserName>"
            "<CreateTime>1348831860</CreateTime>"
            "<MsgType><![CDATA[voice]]></MsgType>"
            "<MediaId><![CDATA[media-1]]></MediaId>"
            "<Format><![CDATA[amr]]></Format>"
            "<Recognition><![CDATA[明天早上9点提醒我开会]]></Recognition>"
            "<MsgId>2234567890123456</MsgId>"
            "</xml>"
        )


class DummyIngest:
    def __init__(self):
        self.called = False
        self.voice_called = False

    def process_text_message(self, db, wecom_user_id, msg_id, raw_xml, text):
        self.called = True
        assert wecom_user_id == "fromUser"
        assert text == "hello"

    def process_voice_message(self, db, wecom_user_id, msg_id, raw_xml, media_id, audio_format, recognition):
        self.voice_called = True
        assert wecom_user_id == "fromUser"
        assert media_id == "media-1"
        assert audio_format == "amr"
        assert "提醒我开会" in recognition


def test_wechat_get_and_post(monkeypatch):
    ingest = DummyIngest()
    app = FastAPI()
    app.state.message_ingest_service = ingest
    app.include_router(wechat_api.router)

    monkeypatch.setattr(wechat_api, "_get_crypto", lambda: DummyCrypto())

    client = TestClient(app)
    verify = client.get(
        "/wechat",
        params={"msg_signature": "s", "timestamp": "1", "nonce": "n", "echostr": "e"},
    )
    assert verify.status_code == 200
    assert verify.text == "ok"

    resp = client.post(
        "/wechat",
        params={"msg_signature": "s", "timestamp": "1", "nonce": "n"},
        content="<xml><Encrypt>x</Encrypt></xml>",
    )
    assert resp.status_code == 200
    assert resp.text == "success"
    assert ingest.called


def test_wechat_voice_post(monkeypatch):
    ingest = DummyIngest()
    app = FastAPI()
    app.state.message_ingest_service = ingest
    app.include_router(wechat_api.router)

    monkeypatch.setattr(wechat_api, "_get_crypto", lambda: DummyVoiceCrypto())

    client = TestClient(app)
    resp = client.post(
        "/wechat",
        params={"msg_signature": "s", "timestamp": "1", "nonce": "n"},
        content="<xml><Encrypt>x</Encrypt></xml>",
    )
    assert resp.status_code == 200
    assert resp.text == "success"
    assert ingest.voice_called
