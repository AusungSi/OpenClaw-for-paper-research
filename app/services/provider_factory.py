from __future__ import annotations

from app.core.config import Settings
from app.llm.ollama_client import OllamaClient
from app.llm.providers import (
    ExternalIntentProvider,
    ExternalReplyProvider,
    IntentLLMProvider,
    LocalOllamaIntentProvider,
    LocalOllamaReplyProvider,
    ReplyLLMProvider,
)
from app.services.asr_service import AsrProvider, IflytekAsrProvider, LocalAsrProvider


def parse_fallback_order(settings: Settings) -> list[str]:
    values = [part.strip().lower() for part in settings.fallback_order.split(",") if part.strip()]
    if not values:
        return ["external", "local", "template"]
    out: list[str] = []
    for item in values:
        if item not in out:
            out.append(item)
    if "template" not in out:
        out.append("template")
    return out


def build_intent_providers(settings: Settings, ollama_client: OllamaClient | None = None) -> list[IntentLLMProvider]:
    local_provider = LocalOllamaIntentProvider(settings=settings, ollama_client=ollama_client)
    external_provider = ExternalIntentProvider(settings=settings)
    return _ordered_llm_providers(
        primary=settings.intent_provider.lower().strip(),
        fallback_enabled=settings.intent_fallback_enabled,
        fallback_order=parse_fallback_order(settings),
        local_provider=local_provider,
        external_provider=external_provider,
    )


def build_reply_providers(settings: Settings, ollama_client: OllamaClient | None = None) -> list[ReplyLLMProvider]:
    local_provider = LocalOllamaReplyProvider(settings=settings, ollama_client=ollama_client)
    external_provider = ExternalReplyProvider(settings=settings)
    return _ordered_llm_providers(
        primary=settings.reply_provider.lower().strip(),
        fallback_enabled=settings.reply_fallback_enabled,
        fallback_order=parse_fallback_order(settings),
        local_provider=local_provider,
        external_provider=external_provider,
    )


def build_asr_providers(settings: Settings) -> list[AsrProvider]:
    local_provider = LocalAsrProvider()
    external_provider = IflytekAsrProvider() if settings.asr_external_enabled else None
    providers: list[AsrProvider] = []
    primary = settings.asr_provider.lower().strip()
    if primary == "external":
        if external_provider is not None:
            providers.append(external_provider)
        else:
            providers.append(local_provider)
    else:
        providers.append(local_provider)

    if settings.asr_fallback_enabled:
        for order in parse_fallback_order(settings):
            if order == "local":
                _append_unique(providers, local_provider)
            elif order == "external" and external_provider is not None:
                _append_unique(providers, external_provider)
    return providers


def _ordered_llm_providers(
    *,
    primary: str,
    fallback_enabled: bool,
    fallback_order: list[str],
    local_provider,
    external_provider,
):
    providers = []
    if primary == "external":
        providers.append(external_provider)
    else:
        providers.append(local_provider)
    if fallback_enabled:
        for order in fallback_order:
            if order == "local":
                _append_unique(providers, local_provider)
            elif order == "external":
                _append_unique(providers, external_provider)
    return providers


def _append_unique(providers: list, candidate) -> None:
    if candidate not in providers:
        providers.append(candidate)
