from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.schemas import CapabilitiesResponse, CapabilityItem, HealthResponse
from app.infra.db import get_db
from app.infra.wecom_client import WeComClient
from app.llm.ollama_client import OllamaClient
from app.services.asr_service import AsrService
from app.services.intent_service import IntentService
from app.services.message_ingest import MessageIngestService
from app.services.research_service import ResearchService
from app.services.reply_generation_service import ReplyGenerationService
from app.services.scheduler_service import SchedulerService


router = APIRouter(prefix="/api/v1")


def get_scheduler(request: Request) -> SchedulerService:
    return request.app.state.scheduler_service


def get_ollama(request: Request) -> OllamaClient:
    return request.app.state.ollama_client


def get_wecom(request: Request) -> WeComClient:
    return request.app.state.wecom_client


def get_ingest_service(request: Request) -> MessageIngestService:
    return request.app.state.message_ingest_service


def get_intent_service(request: Request) -> IntentService:
    return request.app.state.intent_service


def get_asr(request: Request) -> AsrService:
    return request.app.state.asr_service


def get_reply_generation(request: Request) -> ReplyGenerationService:
    return request.app.state.reply_generation_service


def get_research_service(request: Request) -> ResearchService:
    return request.app.state.research_service


@router.get("/health", response_model=HealthResponse)
def healthcheck(
    request: Request,
    db: Session = Depends(get_db),
    scheduler_service: SchedulerService = Depends(get_scheduler),
    ollama_client: OllamaClient = Depends(get_ollama),
    wecom_client: WeComClient = Depends(get_wecom),
    ingest_service: MessageIngestService = Depends(get_ingest_service),
    intent_service: IntentService = Depends(get_intent_service),
    asr_service: AsrService = Depends(get_asr),
    reply_generation_service: ReplyGenerationService = Depends(get_reply_generation),
    research_service: ResearchService = Depends(get_research_service),
) -> HealthResponse:
    db_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    wecom_send_ok, wecom_last_error = wecom_client.last_send_status()
    intent_ok, intent_provider_name, intent_last_error = intent_service.health_status()
    reply_ok, reply_provider_name, reply_last_error = reply_generation_service.health_status()
    asr_ok, asr_provider_name, asr_last_error = asr_service.health_status()
    metrics = {"openclaw_http_ok": 0, "openclaw_http_fail": 0, "openclaw_cli_fallback_count": 0, "openclaw_latency_ms": 0}
    openclaw_client = getattr(request.app.state, "openclaw_client", None)
    if openclaw_client and hasattr(openclaw_client, "metrics_snapshot"):
        try:
            metrics = openclaw_client.metrics_snapshot()
        except Exception:
            metrics = metrics
    research_metrics: dict[str, int | dict[str, int]] = {
        "research_jobs_total": 0,
        "research_job_latency_ms": 0,
        "research_cache_hit": 0,
        "research_cache_miss": 0,
        "research_export_success": 0,
        "research_export_fail": 0,
        "research_search_source_status": {},
    }
    if research_service and hasattr(research_service, "metrics_snapshot"):
        try:
            research_metrics = research_service.metrics_snapshot()
        except Exception:
            research_metrics = research_metrics

    return HealthResponse(
        db_ok=db_ok,
        ollama_ok=ollama_client.healthcheck(),
        scheduler_ok=scheduler_service.started,
        wecom_send_ok=wecom_send_ok,
        wecom_last_error=wecom_last_error,
        webhook_dedup_ok=ingest_service.webhook_dedup_ok,
        asr_ok=asr_ok,
        asr_provider=asr_provider_name,
        asr_last_error=asr_last_error,
        nlg_last_error=reply_generation_service.last_error or reply_last_error,
        intent_provider_ok=intent_ok,
        intent_provider_name=intent_provider_name,
        intent_last_error=intent_last_error,
        reply_provider_ok=reply_ok,
        reply_provider_name=reply_provider_name,
        reply_last_error=reply_last_error,
        asr_provider_ok=asr_ok,
        asr_provider_name=asr_provider_name,
        openclaw_http_ok=int(metrics.get("openclaw_http_ok", 0)),
        openclaw_http_fail=int(metrics.get("openclaw_http_fail", 0)),
        openclaw_cli_fallback_count=int(metrics.get("openclaw_cli_fallback_count", 0)),
        openclaw_latency_ms=int(metrics.get("openclaw_latency_ms", 0)),
        research_jobs_total=int(research_metrics.get("research_jobs_total", 0)),
        research_job_latency_ms=int(research_metrics.get("research_job_latency_ms", 0)),
        research_cache_hit=int(research_metrics.get("research_cache_hit", 0)),
        research_cache_miss=int(research_metrics.get("research_cache_miss", 0)),
        research_export_success=int(research_metrics.get("research_export_success", 0)),
        research_export_fail=int(research_metrics.get("research_export_fail", 0)),
        research_search_source_status=dict(research_metrics.get("research_search_source_status", {})),
    )


@router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities(
    intent_service: IntentService = Depends(get_intent_service),
    reply_generation_service: ReplyGenerationService = Depends(get_reply_generation),
    asr_service: AsrService = Depends(get_asr),
) -> CapabilitiesResponse:
    intent_cap = intent_service.capability()
    reply_cap = reply_generation_service.capability()
    asr_cap = asr_service.capability()
    return CapabilitiesResponse(
        intent=CapabilityItem(**intent_cap),
        reply=CapabilityItem(**reply_cap),
        asr=CapabilityItem(**asr_cap),
    )
