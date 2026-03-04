from __future__ import annotations

import orjson
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.mobile import get_current_user_id
from app.api.research import router as research_router
from app.domain.models import Base
from app.infra.db import get_db
from app.infra.repos import ResearchJobRepo, UserRepo
from app.llm.openclaw_client import LLMCallResult, LLMTaskType
from app.services.research_service import ResearchService


class FakeOpenClawClient:
    def chat_completion(self, *, task_type, prompt, system_prompt=None, user=None, temperature=0.0, max_tokens=0):
        if task_type == LLMTaskType.RESEARCH_PLAN:
            return LLMCallResult(
                text='{"directions":[{"name":"方向A","queries":["q1","q2"],"exclude_terms":[]}]}',
                provider="fake",
                model="fake",
                latency_ms=1,
                via_fallback=False,
            )
        if task_type == LLMTaskType.ABSTRACT_SUMMARIZE:
            return LLMCallResult(
                text="基于摘要总结：方法有效。",
                provider="fake",
                model="fake",
                latency_ms=1,
                via_fallback=False,
            )
        raise AssertionError(f"unexpected task type: {task_type}")


def _build_test_client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db_session = session_local()
    app = FastAPI()
    service = ResearchService(openclaw_client=FakeOpenClawClient(), wecom_client=None)
    user = UserRepo(db_session).get_or_create("research-api-user", timezone_name="Asia/Shanghai")
    app.state.research_service = service
    app.include_router(research_router)

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user_id] = lambda: user.id
    return TestClient(app), service, user, db_session


def test_post_plan_endpoint_is_idempotent():
    client, service, user, db_session = _build_test_client()
    try:
        task = service.create_task(db_session, user_id=user.id, topic="api topic", constraints={})
        service.process_one_job(db_session)

        resp1 = client.post(f"/api/v1/research/tasks/{task.task_id}/plan")
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert body1["task_id"] == task.task_id
        assert body1["queued"] is False

        resp2 = client.post(f"/api/v1/research/tasks/{task.task_id}/plan")
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["queued"] is False
    finally:
        client.close()
        db_session.close()


def test_search_endpoint_supports_force_refresh():
    client, service, user, db_session = _build_test_client()
    try:
        task = service.create_task(db_session, user_id=user.id, topic="api search topic", constraints={"top_n": 5})
        service.process_one_job(db_session)

        resp = client.post(
            f"/api/v1/research/tasks/{task.task_id}/search",
            json={"direction_index": 1, "top_n": 10, "force_refresh": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == task.task_id
        assert body["direction_index"] == 1
        assert body["force_refresh"] is True

        latest_job = ResearchJobRepo(db_session).latest_for_task(task.id)
        assert latest_job is not None
        payload = orjson.loads(latest_job.payload_json)
        assert payload["force_refresh"] is True
    finally:
        client.close()
        db_session.close()


def test_fulltext_build_status_and_upload_endpoint():
    client, service, user, db_session = _build_test_client()
    try:
        task = service.create_task(
            db_session,
            user_id=user.id,
            topic="api fulltext topic",
            constraints={"top_n": 5},
        )
        service.process_one_job(db_session)
        service._search_semantic_scholar = lambda query, *, top_n, constraints: (  # noqa: E731
            [
                {
                    "paper_id": "s2-fulltext-1",
                    "title": "Paper for Fulltext",
                    "title_norm": "paper for fulltext",
                    "authors": ["A"],
                    "year": 2025,
                    "venue": "MICCAI",
                    "doi": "10.1000/fulltext-1",
                    "url": "https://example.org/no-pdf",
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

        service._download_pdf_for_paper = lambda paper: (None, None, "no_pdf_url_found")  # noqa: E731
        resp = client.post(f"/api/v1/research/tasks/{task.task_id}/fulltext/build")
        assert resp.status_code == 200
        assert resp.json()["queued"] is True
        service.process_one_job(db_session)

        status_resp = client.get(f"/api/v1/research/tasks/{task.task_id}/fulltext/status")
        assert status_resp.status_code == 200
        status_body = status_resp.json()
        assert status_body["summary"]["need_upload"] >= 1
        paper_id = status_body["items"][0]["paper_id"]

        service._parse_pdf_bytes = lambda content: ("hello fulltext", {"parser": "fake"})  # noqa: E731
        upload_resp = client.post(
            f"/api/v1/research/tasks/{task.task_id}/papers/{paper_id}/pdf/upload",
            files={"file": ("manual.pdf", b"%PDF-1.4 fake content", "application/pdf")},
        )
        assert upload_resp.status_code == 200
        upload_body = upload_resp.json()
        assert upload_body["status"] == "parsed"
        assert upload_body["text_chars"] > 0
    finally:
        client.close()
        db_session.close()


def test_graph_build_and_view_endpoint():
    client, service, user, db_session = _build_test_client()
    try:
        task = service.create_task(
            db_session,
            user_id=user.id,
            topic="api graph topic",
            constraints={"top_n": 5},
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
                    "doi": "10.1000/seed1",
                    "url": "https://example.org/seed1",
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

        service._fetch_citation_neighbors = lambda paper, *, limit: [  # noqa: E731
            {
                "source_id": "seed-1",
                "target_id": "nbr-1",
                "neighbor_id": "nbr-1",
                "title": "Neighbor Paper",
                "year": 2024,
                "source": "semantic_scholar",
                "edge_type": "cites",
                "source_name": "semantic_scholar",
                "weight": 1.0,
            }
        ]
        build_resp = client.post(
            f"/api/v1/research/tasks/{task.task_id}/graph/build",
            json={"direction_index": 1},
        )
        assert build_resp.status_code == 200
        assert build_resp.json()["queued"] is True
        service.process_one_job(db_session)

        graph_resp = client.get(f"/api/v1/research/tasks/{task.task_id}/graph?direction_index=1")
        assert graph_resp.status_code == 200
        graph = graph_resp.json()
        assert graph["status"] == "done"
        assert len(graph["nodes"]) >= 3
        assert len(graph["edges"]) >= 3

        view_resp = client.get(f"/api/v1/research/tasks/{task.task_id}/graph/view?direction_index=1")
        assert view_resp.status_code == 200
        assert "cytoscape" in view_resp.text.lower()
    finally:
        client.close()
        db_session.close()
