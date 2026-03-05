from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MemoMate"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    db_url: str = "sqlite:///./memomate.db"
    scheduler_interval_seconds: int = 15
    reminder_retry_minutes: int = 5

    wecom_token: str = ""
    wecom_aes_key: str = ""
    wecom_corp_id: str = ""
    wecom_agent_id: int = 1000002
    wecom_secret: str = ""

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout_seconds: int = 30
    ollama_intent_temperature: float = 0.0
    ollama_nlg_temperature: float = 0.2
    ollama_nlg_model: str | None = None
    intent_provider: str = "openclaw"
    intent_local_backend: str = "ollama"
    intent_external_base_url: str = ""
    intent_external_api_key: str = ""
    intent_model: str | None = None
    intent_timeout_seconds: int = 30
    intent_retries: int = 2
    intent_fallback_enabled: bool = True
    reply_provider: str = "local"
    reply_local_backend: str = "ollama"
    reply_external_base_url: str = ""
    reply_external_api_key: str = ""
    reply_model: str | None = None
    reply_timeout_seconds: int = 30
    reply_retries: int = 2
    reply_fallback_enabled: bool = True

    asr_provider: str = "local"
    asr_enabled: bool = True
    asr_local_model: str = "large-v3"
    asr_local_device: str = "cpu"
    asr_local_compute_type: str = "int8"
    asr_timeout_seconds: int = 120
    asr_max_audio_seconds: int = 60
    asr_external_provider: str = "iflytek"
    asr_external_enabled: bool = False
    asr_iflytek_app_id: str = ""
    asr_iflytek_api_key: str = ""
    asr_iflytek_api_secret: str = ""
    asr_iflytek_base_url: str = ""
    asr_fallback_enabled: bool = True
    fallback_order: str = "external,local,template"

    jwt_secret: str = "change-this-secret-to-at-least-32-chars"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    refresh_token_days: int = 30

    default_timezone: str = "Asia/Shanghai"
    pending_action_minutes: int = 10
    pair_code_minutes: int = 10

    openclaw_enabled: bool = False
    openclaw_base_url: str = "http://127.0.0.1:18789"
    openclaw_gateway_token: str = ""
    openclaw_agent_id: str = "memomate"
    openclaw_timeout_seconds: int = 30
    openclaw_retries: int = 2
    openclaw_cli_path: str = "~/.openclaw/bin/openclaw"
    openclaw_cli_fallback_enabled: bool = True

    research_enabled: bool = False
    research_job_interval_seconds: int = 20
    research_job_max_attempts: int = 3
    research_job_backoff_seconds: int = 10
    research_queue_mode: str = "worker"
    research_queue_name: str = "research"
    research_worker_poll_seconds: int = 2
    research_worker_concurrency: int = 2
    research_job_lease_seconds: int = 120
    research_job_heartbeat_seconds: int = 15
    research_direction_min: int = 3
    research_direction_max: int = 8
    research_topn_default: int = 20
    research_seed_topn_default: int = 60
    research_seed_max_abstract_chars: int = 600
    research_round_topn_default: int = 12
    research_page_size: int = 10
    research_sources_default: str = "semantic_scholar,arxiv"
    research_artifact_dir: str = "./artifacts/research"
    research_save_base_dir: str = "./artifacts/research/saved"
    research_cache_enabled: bool = True
    research_cache_ttl_seconds: int = 86400
    research_export_send_file: bool = True
    research_metrics_enabled: bool = True
    research_fulltext_enabled: bool = True
    research_fulltext_max_file_mb: int = 30
    research_fulltext_timeout_seconds: int = 45
    research_fulltext_retries: int = 2
    research_graph_enabled: bool = True
    research_graph_depth_default: int = 1
    research_graph_seed_topn: int = 20
    research_graph_expand_limit_per_paper: int = 30
    research_graph_viz_enabled: bool = True
    research_exploration_enabled: bool = True
    research_max_rounds: int = 10
    research_round_candidate_default: int = 4
    research_citation_sources_default: str = "semantic_scholar,openalex,crossref"
    research_citation_on_demand_only: bool = True
    research_citation_cache_ttl_seconds: int = 86400
    research_wecom_lite_mode: bool = True
    research_web_base_url: str = ""
    research_ocr_enabled: bool = False
    research_graph_paper_limit_default: int = 8
    research_summary_enabled: bool = True
    research_summary_max_chars: int = 8000
    semantic_scholar_api_key: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
