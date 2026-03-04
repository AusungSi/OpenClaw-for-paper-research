from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.domain.enums import ResearchJobStatus, ResearchJobType
import orjson

from app.domain.models import ResearchJob
from app.infra.repos import ResearchJobRepo, ResearchTaskRepo, UserRepo
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
        self.file_fail = False

    def send_text(self, user_id: str, content: str):
        self.messages.append((user_id, content))
        return True, None

    def send_file(self, user_id: str, _path: str):
        if self.file_fail:
            return False, "external:wecom_file_send_failed"
        self.messages.append((user_id, "[file]"))
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
    original_lite_mode = settings.research_wecom_lite_mode
    settings.research_enabled = True
    settings.research_wecom_lite_mode = False

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
        settings.research_wecom_lite_mode = original_lite_mode


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


def test_research_job_claim_and_reclaim(db_session):
    service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=None)
    user = UserRepo(db_session).get_or_create("research-worker-user", timezone_name="Asia/Shanghai")
    task = service.create_task(
        db_session,
        user_id=user.id,
        topic="worker queue topic",
        constraints={},
    )
    job_repo = ResearchJobRepo(db_session)
    plan_job = job_repo.latest_for_task(task.id)
    assert plan_job is not None
    plan_job.status = ResearchJobStatus.DONE
    db_session.add(plan_job)
    db_session.flush()

    queued = job_repo.enqueue(task.id, ResearchJobType.SEARCH, {"direction_index": 1}, queue_name="research")
    assert queued.status == ResearchJobStatus.QUEUED

    first = job_repo.claim_next(worker_id="worker-a", lease_seconds=1, queue_name="research")
    assert first is not None
    assert first.status == ResearchJobStatus.RUNNING
    assert first.worker_id == "worker-a"

    first.lease_until = datetime.now(timezone.utc) - timedelta(seconds=2)
    db_session.add(first)
    db_session.flush()

    second = job_repo.claim_next(worker_id="worker-b", lease_seconds=30, queue_name="research")
    assert second is not None
    assert second.id == first.id
    assert second.worker_id == "worker-b"
    assert second.attempts >= 2


def test_research_command_topic_with_constraints(db_session):
    settings = get_settings()
    original_research_enabled = settings.research_enabled
    settings.research_enabled = True
    try:
        wecom = FakeWeCom()
        service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=wecom)
        user = UserRepo(db_session).get_or_create("research-constraints-user", timezone_name="Asia/Shanghai")
        command = ResearchCommandService(research_service=service, wecom_client=wecom)
        captured: list[str] = []
        handled = command.handle(
            db=db_session,
            user_id=user.id,
            wecom_user_id="research-constraints-user",
            text="调研 主题：ultrasound report generation 年份：2021-2026 领域：medical imaging 数量：20 来源：arxiv",
            reply_sink=captured.append,
        )
        assert handled is True
        assert captured
        assert "年份=2021-2026" in captured[0]
        assert "来源=arxiv" in captured[0]
        snapshot = service.get_active_task_snapshot(db_session, user_id=user.id)
        assert snapshot is not None
        assert snapshot["constraints"]["year_from"] == 2021
        assert snapshot["constraints"]["year_to"] == 2026
        assert snapshot["constraints"]["top_n"] == 20
        assert snapshot["constraints"]["sources"] == ["arxiv"]
    finally:
        settings.research_enabled = original_research_enabled


def test_research_cache_hit_and_force_refresh(db_session):
    service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=None)
    user = UserRepo(db_session).get_or_create("research-cache-user", timezone_name="Asia/Shanghai")
    task = service.create_task(
        db_session,
        user_id=user.id,
        topic="cache topic",
        constraints={"sources": ["semantic_scholar"], "top_n": 5},
    )
    service._plan_directions = lambda _topic, _constraints: [  # noqa: E731
        {"name": "方向A", "queries": ["cache query"], "exclude_terms": []},
    ]
    service.process_one_job(db_session)

    calls = {"semantic": 0}

    def fake_search(query: str, *, top_n: int, constraints: dict):
        calls["semantic"] += 1
        return (
            [
                {
                    "paper_id": f"id-{calls['semantic']}",
                    "title": "Cacheable Paper",
                    "title_norm": "cacheable paper",
                    "authors": ["A"],
                    "year": 2025,
                    "venue": "MICCAI",
                    "doi": f"10.1000/cache-{calls['semantic']}",
                    "url": "https://example.org/cache",
                    "abstract": "abstract",
                    "source": "semantic_scholar",
                    "relevance_score": None,
                }
            ],
            "ok",
            None,
        )

    service._search_semantic_scholar = fake_search
    service._search_arxiv = lambda query, *, top_n, constraints: ([], "ok_empty", None)  # noqa: E731

    service.enqueue_search(db_session, user_id=user.id, direction_index=1, top_n=5, force_refresh=False)
    service.process_one_job(db_session)
    assert calls["semantic"] == 1

    service.enqueue_search(db_session, user_id=user.id, direction_index=1, top_n=5, force_refresh=False)
    service.process_one_job(db_session)
    assert calls["semantic"] == 1
    assert service.metrics_snapshot()["research_cache_hit"] >= 1

    service.enqueue_search(db_session, user_id=user.id, direction_index=1, top_n=5, force_refresh=True)
    service.process_one_job(db_session)
    assert calls["semantic"] == 2

    queued_job = ResearchJobRepo(db_session).latest_for_task(task.id)
    payload = orjson.loads(queued_job.payload_json)
    assert payload["force_refresh"] is True


def test_research_semantic_429_fallback_to_arxiv(db_session):
    service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=None)
    user = UserRepo(db_session).get_or_create("research-fallback-user", timezone_name="Asia/Shanghai")
    service._plan_directions = lambda _topic, _constraints: [  # noqa: E731
        {"name": "方向A", "queries": ["fallback query"], "exclude_terms": []},
    ]
    task = service.create_task(
        db_session,
        user_id=user.id,
        topic="fallback topic",
        constraints={"sources": ["semantic_scholar"], "top_n": 5},
    )
    service.process_one_job(db_session)
    service._search_semantic_scholar = lambda query, *, top_n, constraints: ([], "rate_limited", "http_429")  # noqa: E731
    service._search_arxiv = lambda query, *, top_n, constraints: (  # noqa: E731
        [
            {
                "paper_id": "arxiv-1",
                "title": "Fallback Arxiv Paper",
                "title_norm": "fallback arxiv paper",
                "authors": ["A"],
                "year": 2024,
                "venue": "arXiv",
                "doi": None,
                "url": "https://arxiv.org/abs/0000.00001",
                "abstract": "abstract",
                "source": "arxiv",
                "relevance_score": None,
            }
        ],
        "ok",
        None,
    )

    service.enqueue_search(db_session, user_id=user.id, direction_index=1)
    service.process_one_job(db_session)
    page = service.page_direction_papers(db_session, user_id=user.id, direction_index=1, page=1)
    assert page["total"] == 1
    assert page["items"][0]["source"] == "arxiv"
    metrics = service.metrics_snapshot()
    assert any(key.startswith("semantic_scholar:fallback_arxiv_from_") for key in metrics["research_search_source_status"])


def test_research_export_file_fallback_to_text_path(db_session):
    settings = get_settings()
    original_research_enabled = settings.research_enabled
    original_export_send_file = settings.research_export_send_file
    original_lite_mode = settings.research_wecom_lite_mode
    settings.research_enabled = True
    settings.research_export_send_file = True
    settings.research_wecom_lite_mode = False
    try:
        wecom = FakeWeCom()
        wecom.file_fail = True
        service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=wecom)
        user = UserRepo(db_session).get_or_create("research-export-user", timezone_name="Asia/Shanghai")
        service._plan_directions = lambda _topic, _constraints: [  # noqa: E731
            {"name": "方向A", "queries": ["export query"], "exclude_terms": []},
        ]
        task = service.create_task(
            db_session,
            user_id=user.id,
            topic="export topic",
            constraints={"sources": ["semantic_scholar"], "top_n": 5},
        )
        service.process_one_job(db_session)
        service._search_semantic_scholar = lambda query, *, top_n, constraints: (  # noqa: E731
            [
                {
                    "paper_id": "p-1",
                    "title": "Export Paper",
                    "title_norm": "export paper",
                    "authors": ["A"],
                    "year": 2024,
                    "venue": "MICCAI",
                    "doi": "10.1000/export-1",
                    "url": "https://example.org/export",
                    "abstract": "abstract",
                    "source": "semantic_scholar",
                    "relevance_score": None,
                }
            ],
            "ok",
            None,
        )
        service._search_arxiv = lambda query, *, top_n, constraints: ([], "ok_empty", None)  # noqa: E731
        service.enqueue_search(db_session, user_id=user.id, direction_index=1)
        service.process_one_job(db_session)

        command = ResearchCommandService(research_service=service, wecom_client=wecom)
        captured: list[str] = []
        handled = command.handle(
            db=db_session,
            user_id=user.id,
            wecom_user_id="research-export-user",
            text="调研 导出 格式：bib",
            reply_sink=captured.append,
        )
        assert handled is True
        assert captured
        assert "回退文本路径" in captured[0]
        assert task.task_id in captured[0]
    finally:
        settings.research_enabled = original_research_enabled
        settings.research_export_send_file = original_export_send_file
        settings.research_wecom_lite_mode = original_lite_mode


def test_research_command_fulltext_and_graph_commands(db_session):
    settings = get_settings()
    original_research_enabled = settings.research_enabled
    original_lite_mode = settings.research_wecom_lite_mode
    settings.research_enabled = True
    settings.research_wecom_lite_mode = False
    try:
        wecom = FakeWeCom()
        service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=wecom)
        user = UserRepo(db_session).get_or_create("research-graph-cmd-user", timezone_name="Asia/Shanghai")
        service._plan_directions = lambda _topic, _constraints: [  # noqa: E731
            {"name": "方向A", "queries": ["graph query"], "exclude_terms": []},
        ]
        task = service.create_task(
            db_session,
            user_id=user.id,
            topic="graph cmd topic",
            constraints={"sources": ["semantic_scholar"], "top_n": 5},
        )
        service.process_one_job(db_session)

        service._search_semantic_scholar = lambda query, *, top_n, constraints: (  # noqa: E731
            [
                {
                    "paper_id": "seed-1",
                    "title": "Seed Paper",
                    "title_norm": "seed paper",
                    "authors": ["A"],
                    "year": 2025,
                    "venue": "MICCAI",
                    "doi": "10.1000/seed-1",
                    "url": "https://example.org/seed",
                    "abstract": "abstract",
                    "source": "semantic_scholar",
                    "relevance_score": None,
                }
            ],
            "ok",
            None,
        )
        service._search_arxiv = lambda query, *, top_n, constraints: ([], "ok_empty", None)  # noqa: E731
        service.enqueue_search(db_session, user_id=user.id, direction_index=1)
        service.process_one_job(db_session)

        command = ResearchCommandService(research_service=service, wecom_client=wecom)
        captured: list[str] = []
        assert command.handle(
            db=db_session,
            user_id=user.id,
            wecom_user_id="research-graph-cmd-user",
            text="调研 全文 构建",
            reply_sink=captured.append,
        )
        assert "全文抓取任务" in captured[-1]

        captured = []
        assert command.handle(
            db=db_session,
            user_id=user.id,
            wecom_user_id="research-graph-cmd-user",
            text="调研 图谱 构建 方向 1",
            reply_sink=captured.append,
        )
        assert "图谱构建" in captured[-1]

        captured = []
        assert command.handle(
            db=db_session,
            user_id=user.id,
            wecom_user_id="research-graph-cmd-user",
            text="调研 图谱 查看 方向 1",
            reply_sink=captured.append,
        )
        assert "/graph/view?direction_index=1" in captured[-1]

        captured = []
        assert command.handle(
            db=db_session,
            user_id=user.id,
            wecom_user_id="research-graph-cmd-user",
            text="调研 上传PDF 1",
            reply_sink=captured.append,
        )
        assert "/pdf/upload" in captured[-1]
        assert task.task_id in captured[-1]
    finally:
        settings.research_enabled = original_research_enabled
        settings.research_wecom_lite_mode = original_lite_mode
