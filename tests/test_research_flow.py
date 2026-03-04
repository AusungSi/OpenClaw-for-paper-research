from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.domain.enums import ResearchJobStatus
from app.domain.models import ResearchJob
from app.infra.repos import ResearchTaskRepo, UserRepo
from app.llm.openclaw_client import LLMCallResult, LLMTaskType
from app.services.research_command_service import ResearchCommandService
from app.services.research_service import ResearchService


class FakeOpenClawClient:
    def chat_completion(self, *, task_type, prompt, system_prompt=None, user=None, temperature=0.0, max_tokens=0):
        if task_type == LLMTaskType.RESEARCH_PLAN:
            return LLMCallResult(
                text=(
                    '{"directions":['
                    '{"name":"任务定义与评测","queries":["ultrasound report generation benchmark","ultrasound report generation metrics"],"exclude_terms":[]},'
                    '{"name":"核心生成方法","queries":["ultrasound report generation model","ultrasound report generation transformer"],"exclude_terms":[]},'
                    '{"name":"幻觉检测与缓解","queries":["ultrasound report hallucination detection","ultrasound report hallucination mitigation"],"exclude_terms":[]}'
                    ']}'
                ),
                provider="fake",
                model="fake",
                latency_ms=1,
                via_fallback=False,
            )
        if task_type == LLMTaskType.ABSTRACT_SUMMARIZE:
            return LLMCallResult(
                text="基于摘要总结：该方法通过多阶段生成与一致性约束降低报告幻觉。",
                provider="fake",
                model="fake",
                latency_ms=1,
                via_fallback=False,
            )
        raise AssertionError(f"unexpected task type: {task_type}")


class FakeWeCom:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def send_text(self, user_id: str, content: str):
        self.messages.append((user_id, content))
        return True, None


def test_research_planner_contract():
    service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=None)
    directions = service._plan_directions("ultrasound report generation hallucination", {})

    assert 3 <= len(directions) <= 8
    for item in directions:
        assert 2 <= len(item["queries"]) <= 4


def test_research_flow_create_plan_search_and_select(db_session):
    settings = get_settings()
    original_research_enabled = settings.research_enabled
    settings.research_enabled = True

    try:
        wecom = FakeWeCom()
        service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=wecom)
        user = UserRepo(db_session).get_or_create("research-user", timezone_name="Asia/Shanghai")

        task = service.create_task(
            db_session,
            user_id=user.id,
            topic="ultrasound report generation hallucination",
            constraints={"sources": ["semantic_scholar"], "top_n": 10},
        )
        assert task.status.value == "planning"

        processed = service.process_one_job(db_session)
        assert processed == 1

        snapshot = service.get_task(db_session, user_id=user.id, task_id=task.task_id)
        assert len(snapshot["directions"]) >= 3

        def fake_search(query: str, *, top_n: int, constraints: dict):
            return [
                {
                    "paper_id": "s2-1",
                    "title": "Reducing Hallucination in Ultrasound Report Generation",
                    "title_norm": "reducing hallucination in ultrasound report generation",
                    "authors": ["A", "B"],
                    "year": 2025,
                    "venue": "MICCAI",
                    "doi": "10.1000/test-doi-1",
                    "url": "https://example.org/paper1",
                    "abstract": "We propose a staged generation model with consistency constraints.",
                    "source": "semantic_scholar",
                    "relevance_score": None,
                },
                {
                    "paper_id": "s2-dup",
                    "title": "Reducing Hallucination in Ultrasound Report Generation",
                    "title_norm": "reducing hallucination in ultrasound report generation",
                    "authors": ["A", "B"],
                    "year": 2025,
                    "venue": "MICCAI",
                    "doi": "10.1000/test-doi-1",
                    "url": "https://example.org/paper1",
                    "abstract": "We propose a staged generation model with consistency constraints.",
                    "source": "semantic_scholar",
                    "relevance_score": None,
                },
            ]

        service._search_semantic_scholar = fake_search
        service._search_arxiv = lambda query, *, top_n, constraints: []

        queued_task = service.enqueue_search(db_session, user_id=user.id, direction_index=1)
        assert queued_task.status.value == "searching"

        processed = service.process_one_job(db_session)
        assert processed == 1

        page = service.page_direction_papers(db_session, user_id=user.id, direction_index=1, page=1)
        assert page["total"] == 1
        assert page["items"][0]["method_summary"].startswith("基于摘要总结：")

        command = ResearchCommandService(research_service=service, wecom_client=wecom)
        captured: list[str] = []
        handled = command.handle(
            db=db_session,
            user_id=user.id,
            wecom_user_id="research-user",
            text="调研 选择 1",
            reply_sink=captured.append,
        )
        assert handled is True
        assert captured
        assert "Reducing Hallucination" in captured[0]
    finally:
        settings.research_enabled = original_research_enabled


def test_research_job_retry_then_fail(db_session):
    settings = get_settings()
    original_max_attempts = settings.research_job_max_attempts
    original_backoff = settings.research_job_backoff_seconds
    try:
        settings.research_job_max_attempts = 2
        settings.research_job_backoff_seconds = 1

        service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=None)
        user = UserRepo(db_session).get_or_create("research-retry-user", timezone_name="Asia/Shanghai")
        task = service.create_task(
            db_session,
            user_id=user.id,
            topic="retry topic",
            constraints={},
        )

        def fail_plan(topic: str, constraints: dict):
            raise RuntimeError("plan boom")

        service._plan_directions = fail_plan

        processed = service.process_one_job(db_session)
        assert processed == 1

        first_job = db_session.query(ResearchJob).filter(ResearchJob.task_id == task.id).one()
        assert first_job.status == ResearchJobStatus.QUEUED
        assert first_job.attempts == 1
        assert ResearchTaskRepo(db_session).get_by_task_id(task.task_id, user_id=user.id).status.value == "planning"

        first_job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db_session.add(first_job)
        db_session.flush()

        processed = service.process_one_job(db_session)
        assert processed == 1

        failed_job = db_session.query(ResearchJob).filter(ResearchJob.task_id == task.id).one()
        assert failed_job.status == ResearchJobStatus.FAILED
        assert failed_job.attempts == 2
        assert ResearchTaskRepo(db_session).get_by_task_id(task.task_id, user_id=user.id).status.value == "failed"
    finally:
        settings.research_job_max_attempts = original_max_attempts
        settings.research_job_backoff_seconds = original_backoff
