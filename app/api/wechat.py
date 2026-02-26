from __future__ import annotations

from xml.etree import ElementTree as ET

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from starlette.requests import ClientDisconnect
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infra.db import session_scope
from app.services.message_ingest import MessageIngestService


router = APIRouter()
logger = get_logger("wechat")


def get_ingest_service(request: Request) -> MessageIngestService:
    return request.app.state.message_ingest_service


def _get_crypto() -> WeChatCrypto:
    settings = get_settings()
    if not settings.wecom_token or not settings.wecom_aes_key or not settings.wecom_corp_id:
        raise HTTPException(status_code=500, detail="WeCom crypto settings are incomplete")
    return WeChatCrypto(settings.wecom_token, settings.wecom_aes_key, settings.wecom_corp_id)


@router.get("/wechat")
def wechat_verify(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> PlainTextResponse:
    crypto = _get_crypto()
    try:
        decrypted = crypto.check_signature(msg_signature, timestamp, nonce, echostr)
    except InvalidSignatureException as exc:
        raise HTTPException(status_code=403, detail="signature validation failed") from exc
    text = decrypted.decode("utf-8") if isinstance(decrypted, bytes) else str(decrypted)
    return PlainTextResponse(text)


@router.post("/wechat")
async def wechat_receive(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    ingest_service: MessageIngestService = Depends(get_ingest_service),
) -> PlainTextResponse:
    try:
        raw_body = await request.body()
    except ClientDisconnect:
        # The client (or tunnel) closed early; ignore and avoid noisy traceback.
        logger.warning("wechat request disconnected before body was fully read")
        return PlainTextResponse("success")
    if not raw_body:
        logger.warning("wechat request body is empty")
        return PlainTextResponse("success")

    crypto = _get_crypto()
    try:
        decrypted_xml = crypto.decrypt_message(raw_body, msg_signature, timestamp, nonce)
    except InvalidSignatureException as exc:
        raise HTTPException(status_code=403, detail="signature validation failed") from exc

    xml_text = decrypted_xml.decode("utf-8") if isinstance(decrypted_xml, bytes) else str(decrypted_xml)
    try:
        message = parse_xml(xml_text)
    except ET.ParseError:
        logger.warning("failed to parse decrypted wechat xml")
        return PlainTextResponse("success")
    msg_type = message.get("MsgType", "").lower()
    msg_id = message.get("MsgId") or f"{message.get('CreateTime', '0')}-{message.get('FromUserName', '')}"
    from_user = message.get("FromUserName", "")
    content = message.get("Content", "").strip()
    media_id = message.get("MediaId", "").strip()
    voice_format = message.get("Format", "").strip()
    recognition = message.get("Recognition", "").strip()

    logger.info("wechat inbound type=%s from=%s msg_id=%s", msg_type, from_user, msg_id)
    if msg_type == "text" and from_user:
        # Acknowledge webhook quickly, then process the message asynchronously.
        background_tasks.add_task(
            _process_text_message_task,
            ingest_service=ingest_service,
            wecom_user_id=from_user,
            msg_id=msg_id,
            raw_xml=xml_text,
            content=content,
        )
    elif msg_type == "voice" and from_user:
        background_tasks.add_task(
            _process_voice_message_task,
            ingest_service=ingest_service,
            wecom_user_id=from_user,
            msg_id=msg_id,
            raw_xml=xml_text,
            media_id=media_id,
            voice_format=voice_format,
            recognition=recognition,
        )

    return PlainTextResponse("success")


def parse_xml(xml_text: str) -> dict[str, str]:
    root = ET.fromstring(xml_text)
    out: dict[str, str] = {}
    for child in root:
        out[child.tag] = child.text or ""
    return out


def _process_text_message_task(
    ingest_service: MessageIngestService,
    wecom_user_id: str,
    msg_id: str,
    raw_xml: str,
    content: str,
) -> None:
    try:
        with session_scope() as bg_db:
            ingest_service.process_text_message(
                db=bg_db,
                wecom_user_id=wecom_user_id,
                msg_id=msg_id,
                raw_xml=raw_xml,
                text=content,
            )
    except Exception:
        logger.exception("failed to process wechat message in background")


def _process_voice_message_task(
    ingest_service: MessageIngestService,
    wecom_user_id: str,
    msg_id: str,
    raw_xml: str,
    media_id: str,
    voice_format: str,
    recognition: str,
) -> None:
    try:
        with session_scope() as bg_db:
            ingest_service.process_voice_message(
                db=bg_db,
                wecom_user_id=wecom_user_id,
                msg_id=msg_id,
                raw_xml=raw_xml,
                media_id=media_id,
                audio_format=voice_format,
                recognition=recognition,
            )
    except Exception:
        logger.exception("failed to process wechat voice message in background")
