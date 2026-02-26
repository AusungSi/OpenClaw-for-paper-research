from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.mobile import router as mobile_router
from app.api.wechat import router as wechat_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.infra.db import init_db
from app.infra.wecom_client import WeComClient
from app.llm.ollama_client import OllamaClient
from app.services.confirm_service import ConfirmService
from app.services.intent_service import IntentService
from app.services.asr_service import AsrService
from app.services.message_ingest import MessageIngestService
from app.services.mobile_auth_service import MobileAuthService
from app.services.reply_generation_service import ReplyGenerationService
from app.services.reply_renderer import ReplyRenderer
from app.services.reminder_service import ReminderService
from app.services.scheduler_service import SchedulerService
from app.services.provider_factory import build_asr_providers, build_intent_providers, build_reply_providers
from app.workers.dispatcher import Dispatcher


setup_logging()
logger = get_logger("main")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    wecom_client = WeComClient()
    ollama_client = OllamaClient()
    intent_providers = build_intent_providers(settings, ollama_client=ollama_client)
    reply_providers = build_reply_providers(settings, ollama_client=ollama_client)
    asr_providers = build_asr_providers(settings)
    asr_service = AsrService(providers=asr_providers)
    reply_renderer = ReplyRenderer()
    reply_generation_service = ReplyGenerationService(reply_providers=reply_providers)
    intent_service = IntentService(intent_providers=intent_providers)
    confirm_service = ConfirmService()
    reminder_service = ReminderService(
        reply_renderer=reply_renderer,
        reply_generation_service=reply_generation_service,
    )
    mobile_auth_service = MobileAuthService()
    message_ingest_service = MessageIngestService(
        intent_service=intent_service,
        confirm_service=confirm_service,
        reminder_service=reminder_service,
        wecom_client=wecom_client,
        asr_service=asr_service,
        reply_generation_service=reply_generation_service,
        reply_renderer=reply_renderer,
    )

    dispatcher = Dispatcher(wecom_client=wecom_client)
    scheduler_service = SchedulerService(dispatcher=dispatcher)

    app.state.wecom_client = wecom_client
    app.state.ollama_client = ollama_client
    app.state.asr_service = asr_service
    app.state.intent_service = intent_service
    app.state.confirm_service = confirm_service
    app.state.reminder_service = reminder_service
    app.state.reply_generation_service = reply_generation_service
    app.state.mobile_auth_service = mobile_auth_service
    app.state.message_ingest_service = message_ingest_service
    app.state.scheduler_service = scheduler_service

    await scheduler_service.start()
    logger.info("application startup complete")
    try:
        yield
    finally:
        await scheduler_service.shutdown()
        logger.info("application shutdown complete")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(wechat_router)
app.include_router(health_router)
app.include_router(mobile_router)
