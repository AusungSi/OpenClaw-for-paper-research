from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from time import perf_counter, sleep
from urllib.parse import quote_plus, urljoin
from xml.etree import ElementTree as ET
import re

import httpx
import orjson
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.enums import (
    ResearchActionType,
    ResearchGraphBuildStatus,
    ResearchGraphViewType,
    ResearchJobType,
    ResearchPaperFulltextStatus,
    ResearchRoundStatus,
    ResearchTaskStatus,
)
from app.domain.models import ResearchRound, ResearchTask, User
from app.infra.repos import (
    ResearchCitationFetchCacheRepo,
    ResearchCitationEdgeRepo,
    ResearchDirectionRepo,
    ResearchGraphSnapshotRepo,
    ResearchJobRepo,
    ResearchPaperRepo,
    ResearchPaperFulltextRepo,
    ResearchRoundCandidateRepo,
    ResearchRoundPaperRepo,
    ResearchRoundRepo,
    ResearchSeedPaperRepo,
    ResearchSearchCacheRepo,
    ResearchSessionRepo,
    ResearchTaskRepo,
    UserRepo,
)
from app.infra.wecom_client import WeComClient
from app.llm.openclaw_client import LLMCallResult, LLMTaskType, OpenClawClient


logger = get_logger("research")

try:
    import networkx as nx
except Exception:  # pragma: no cover
    nx = None

try:
    import fitz
except Exception:  # pragma: no cover
    fitz = None

try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
except Exception:  # pragma: no cover
    pdfminer_extract_text = None


@dataclass
class SearchFetchResult:
    papers: list[dict]
    status: str
    error: str | None = None


class ResearchService:
    def __init__(
        self,
        *,
        openclaw_client: OpenClawClient | None = None,
        wecom_client: WeComClient | None = None,
    ) -> None:
        self.settings = get_settings()
        self.openclaw_client = openclaw_client or OpenClawClient(settings=self.settings)
        self.wecom_client = wecom_client
        self.research_jobs_total = 0
        self.research_job_latency_ms = 0
        self.research_cache_hit = 0
        self.research_cache_miss = 0
        self.research_export_success = 0
        self.research_export_fail = 0
        self.research_search_source_status: dict[str, int] = {}

    def metrics_snapshot(self) -> dict[str, int | dict[str, int]]:
        return {
            "research_jobs_total": self.research_jobs_total,
            "research_job_latency_ms": self.research_job_latency_ms,
            "research_cache_hit": self.research_cache_hit,
            "research_cache_miss": self.research_cache_miss,
            "research_export_success": self.research_export_success,
            "research_export_fail": self.research_export_fail,
            "research_search_source_status": dict(self.research_search_source_status),
        }

    def create_task(
        self,
        db: Session,
        *,
        user_id: int,
        topic: str,
        constraints: dict | None = None,
    ) -> ResearchTask:
        task_repo = ResearchTaskRepo(db)
        session_repo = ResearchSessionRepo(db)
        now = datetime.now(timezone.utc)
        # Use global recent tasks to avoid collisions across users.
        task_id = self._next_task_id(task_repo.list_recent_all(limit=500))
        row = ResearchTask(
            task_id=task_id,
            user_id=user_id,
            topic=topic.strip(),
            constraints_json=orjson.dumps(constraints or {}).decode("utf-8"),
            status=ResearchTaskStatus.PLANNING,
            created_at=now,
            updated_at=now,
        )
        task_repo.create(row)
        ResearchJobRepo(db).enqueue(
            row.id,
            ResearchJobType.PLAN,
            {"topic": row.topic, "constraints": constraints or {}},
            queue_name=self.settings.research_queue_name,
        )
        session = session_repo.get_or_create(user_id, page_size=self.settings.research_page_size)
        session_repo.set_active_task(session, row.task_id)
        return row

    def enqueue_search(
        self,
        db: Session,
        *,
        user_id: int,
        direction_index: int,
        top_n: int | None = None,
        force_refresh: bool = False,
    ) -> ResearchTask:
        task = self.get_active_task(db, user_id)
        if not task:
            raise ValueError("no active research task")
        if direction_index < 1:
            raise ValueError("direction index must be >= 1")
        payload = {
            "direction_index": direction_index,
            "top_n": top_n or self.settings.research_topn_default,
            "force_refresh": bool(force_refresh),
        }
        ResearchJobRepo(db).enqueue(task.id, ResearchJobType.SEARCH, payload, queue_name=self.settings.research_queue_name)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return task

    def enqueue_plan(self, db: Session, *, user_id: int, task_id: str, force: bool = False) -> tuple[ResearchTask, bool]:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        direction_count = len(ResearchDirectionRepo(db).list_for_task(task.id))
        job_repo = ResearchJobRepo(db)
        if not force and direction_count > 0:
            return task, False
        if job_repo.has_pending(task.id, ResearchJobType.PLAN):
            return task, False
        payload = {
            "topic": task.topic,
            "constraints": _load_json_dict(task.constraints_json),
        }
        job_repo.enqueue(task.id, ResearchJobType.PLAN, payload, queue_name=self.settings.research_queue_name)
        task.status = ResearchTaskStatus.PLANNING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return task, True

    def enqueue_fulltext_build(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        force: bool = False,
        paper_ids: list[str] | None = None,
    ) -> tuple[ResearchTask, bool]:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        if not self.settings.research_fulltext_enabled:
            raise ValueError("research fulltext is disabled")
        job_repo = ResearchJobRepo(db)
        if not force and job_repo.has_pending(task.id, ResearchJobType.FULLTEXT):
            return task, False
        job_repo.enqueue(
            task.id,
            ResearchJobType.FULLTEXT,
            {"force": bool(force), "paper_ids": [str(x).strip() for x in (paper_ids or []) if str(x).strip()]},
            queue_name=self.settings.research_queue_name,
        )
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return task, True

    def enqueue_graph_build(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        direction_index: int | None = None,
        round_id: int | None = None,
        view: str = ResearchGraphViewType.CITATION.value,
        citation_sources: list[str] | None = None,
        seed_top_n: int | None = None,
        expand_limit_per_paper: int | None = None,
        force_refresh: bool = False,
        force: bool = False,
    ) -> tuple[ResearchTask, bool]:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        if not self.settings.research_graph_enabled:
            raise ValueError("research graph is disabled")
        job_repo = ResearchJobRepo(db)
        if not force and job_repo.has_pending(task.id, ResearchJobType.GRAPH_BUILD):
            return task, False
        payload = {
            "direction_index": direction_index,
            "round_id": round_id,
            "view": (view or ResearchGraphViewType.CITATION.value).strip().lower(),
            "citation_sources": citation_sources,
            "seed_top_n": seed_top_n,
            "expand_limit_per_paper": expand_limit_per_paper,
            "force": bool(force),
            "force_refresh": bool(force_refresh),
        }
        job_repo.enqueue(task.id, ResearchJobType.GRAPH_BUILD, payload, queue_name=self.settings.research_queue_name)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return task, True

    def start_exploration(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        direction_index: int,
        top_n: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        sources: list[str] | None = None,
    ) -> tuple[ResearchTask, ResearchRound]:
        if not self.settings.research_exploration_enabled:
            raise ValueError("research exploration is disabled")
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        direction = ResearchDirectionRepo(db).get_by_index(task.id, direction_index)
        if not direction:
            raise ValueError("direction not found")

        round_repo = ResearchRoundRepo(db)
        max_rounds = max(1, int(self.settings.research_max_rounds))
        if round_repo.count_for_task_direction(task.id, direction_index) >= max_rounds:
            raise ValueError(f"max rounds reached ({max_rounds})")

        query_terms = _load_json_list(direction.queries_json) or [direction.name]
        round_row = round_repo.create(
            task_id=task.id,
            direction_index=direction_index,
            parent_round_id=None,
            depth=1,
            action=ResearchActionType.EXPAND.value,
            feedback_text=None,
            query_terms=query_terms[:4],
            status=ResearchRoundStatus.QUEUED.value,
        )
        payload = {
            "direction_index": direction_index,
            "top_n": top_n or self.settings.research_round_topn_default,
            "force_refresh": False,
            "round_id": round_row.id,
            "explicit_queries": query_terms[:4],
            "constraints_override": {
                "year_from": year_from,
                "year_to": year_to,
                "sources": sources,
            },
        }
        ResearchJobRepo(db).enqueue(task.id, ResearchJobType.SEARCH, payload, queue_name=self.settings.research_queue_name)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return task, round_row

    def propose_round_candidates(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        round_id: int,
        action: str,
        feedback_text: str,
        candidate_count: int | None = None,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        round_repo = ResearchRoundRepo(db)
        round_row = round_repo.get(round_id)
        if not round_row or round_row.task_id != task.id:
            raise ValueError("round not found")
        action_norm = (action or "").strip().lower()
        valid_actions = {item.value for item in ResearchActionType}
        if action_norm not in valid_actions:
            raise ValueError("invalid action")
        if action_norm == ResearchActionType.STOP.value:
            round_repo.update_status(round_row, ResearchRoundStatus.STOPPED.value)
            return {"task_id": task.task_id, "round_id": round_id, "action": action_norm, "candidates": []}

        candidate_n = max(2, min(5, int(candidate_count or self.settings.research_round_candidate_default)))
        references = self._round_reference_titles(db, round_id=round_row.id, limit=8)
        generated = self._generate_round_candidates(
            task_topic=task.topic,
            action=action_norm,
            feedback_text=feedback_text,
            query_terms=_load_json_list(round_row.query_terms_json),
            references=references,
            candidate_count=candidate_n,
        )
        repo = ResearchRoundCandidateRepo(db)
        rows = repo.replace_for_round(round_row.id, generated)
        out = []
        for row in rows:
            out.append(
                {
                    "candidate_id": row.id,
                    "candidate_index": row.candidate_index,
                    "name": row.name,
                    "queries": _load_json_list(row.queries_json),
                    "reason": row.reason,
                }
            )
        round_row.action = ResearchActionType(action_norm)
        round_row.feedback_text = (feedback_text or "").strip()[:2000] or None
        round_row.updated_at = datetime.now(timezone.utc)
        db.add(round_row)
        db.flush()
        return {"task_id": task.task_id, "round_id": round_row.id, "action": action_norm, "candidates": out}

    def select_round_candidate(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        round_id: int,
        candidate_id: int,
        top_n: int | None = None,
        force_refresh: bool = False,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        round_repo = ResearchRoundRepo(db)
        parent = round_repo.get(round_id)
        if not parent or parent.task_id != task.id:
            raise ValueError("round not found")
        candidate_repo = ResearchRoundCandidateRepo(db)
        candidate = candidate_repo.get_by_id(candidate_id)
        if not candidate or candidate.round_id != parent.id:
            fallback = candidate_repo.get_by_index(parent.id, candidate_id)
            if not fallback:
                raise ValueError("candidate not found")
            candidate = fallback
        candidate_repo.mark_selected(candidate)

        max_rounds = max(1, int(self.settings.research_max_rounds))
        if parent.depth >= max_rounds:
            raise ValueError(f"max rounds reached ({max_rounds})")
        child = round_repo.create(
            task_id=task.id,
            direction_index=parent.direction_index,
            parent_round_id=parent.id,
            depth=parent.depth + 1,
            action=parent.action.value,
            feedback_text=parent.feedback_text,
            query_terms=_load_json_list(candidate.queries_json),
            status=ResearchRoundStatus.QUEUED.value,
        )
        payload = {
            "direction_index": parent.direction_index,
            "top_n": top_n or self.settings.research_round_topn_default,
            "force_refresh": bool(force_refresh),
            "round_id": child.id,
            "explicit_queries": _load_json_list(candidate.queries_json),
        }
        ResearchJobRepo(db).enqueue(task.id, ResearchJobType.SEARCH, payload, queue_name=self.settings.research_queue_name)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return {
            "task_id": task.task_id,
            "parent_round_id": parent.id,
            "child_round_id": child.id,
            "status": task.status.value,
            "queued": True,
        }

    def create_next_round_from_intent(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        round_id: int,
        intent_text: str,
        top_n: int | None = None,
        force_refresh: bool = False,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        parent = ResearchRoundRepo(db).get(round_id)
        if not parent or parent.task_id != task.id:
            raise ValueError("round not found")
        intent = (intent_text or "").strip()
        if not intent:
            raise ValueError("intent_text is required")
        max_rounds = max(1, int(self.settings.research_max_rounds))
        if parent.depth >= max_rounds:
            raise ValueError(f"max rounds reached ({max_rounds})")

        direction = ResearchDirectionRepo(db).get_by_index(task.id, parent.direction_index)
        references = self._round_reference_titles(db, round_id=parent.id, limit=8)
        queries = self._generate_queries_from_intent(
            task_topic=task.topic,
            direction_name=direction.name if direction else "",
            intent_text=intent,
            current_queries=_load_json_list(parent.query_terms_json),
            references=references,
        )
        child = ResearchRoundRepo(db).create(
            task_id=task.id,
            direction_index=parent.direction_index,
            parent_round_id=parent.id,
            depth=parent.depth + 1,
            action=parent.action.value,
            feedback_text=intent,
            query_terms=queries[:4],
            status=ResearchRoundStatus.QUEUED.value,
        )
        payload = {
            "direction_index": parent.direction_index,
            "top_n": top_n or self.settings.research_round_topn_default,
            "force_refresh": bool(force_refresh),
            "round_id": child.id,
            "explicit_queries": queries[:4],
        }
        ResearchJobRepo(db).enqueue(task.id, ResearchJobType.SEARCH, payload, queue_name=self.settings.research_queue_name)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return {
            "task_id": task.task_id,
            "parent_round_id": parent.id,
            "child_round_id": child.id,
            "status": task.status.value,
            "queued": True,
        }

    def enqueue_round_citation_build(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        round_id: int,
        seed_top_n: int | None = None,
        expand_limit_per_paper: int | None = None,
        citation_sources: list[str] | None = None,
        force_refresh: bool = False,
    ) -> tuple[ResearchTask, bool]:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        round_row = ResearchRoundRepo(db).get(round_id)
        if not round_row or round_row.task_id != task.id:
            raise ValueError("round not found")
        job_repo = ResearchJobRepo(db)
        if not force_refresh and job_repo.has_pending(task.id, ResearchJobType.GRAPH_BUILD):
            return task, False
        payload = {
            "round_id": round_id,
            "direction_index": round_row.direction_index,
            "view": ResearchGraphViewType.CITATION.value,
            "seed_top_n": seed_top_n,
            "expand_limit_per_paper": expand_limit_per_paper,
            "citation_sources": citation_sources,
            "force_refresh": bool(force_refresh),
        }
        job_repo.enqueue(task.id, ResearchJobType.GRAPH_BUILD, payload, queue_name=self.settings.research_queue_name)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return task, True

    def get_exploration_tree(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        include_papers: bool = False,
        paper_limit: int | None = None,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        return self._build_tree_graph(db, task, include_papers=include_papers, paper_limit=paper_limit)

    def list_graph_snapshots(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        view: str | None = None,
        limit: int = 10,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        rows = ResearchGraphSnapshotRepo(db).list_recent(task.id, view_type=view, limit=limit)
        items = []
        for row in rows:
            nodes = _load_json_list_of_dict(row.nodes_json)
            edges = _load_json_list_of_dict(row.edges_json)
            items.append(
                {
                    "snapshot_id": row.id,
                    "direction_index": row.direction_index,
                    "round_id": row.round_id,
                    "view": row.view_type.value,
                    "status": row.status.value,
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "updated_at": row.updated_at,
                }
            )
        return {"task_id": task.task_id, "items": items}

    def process_one_job(
        self,
        db: Session,
        *,
        worker_id: str | None = None,
        queue_name: str | None = None,
        lease_seconds: int | None = None,
    ) -> int:
        job_repo = ResearchJobRepo(db)
        queue = (queue_name or self.settings.research_queue_name).strip() or "research"
        if worker_id:
            job = job_repo.claim_next(
                worker_id=worker_id,
                lease_seconds=int(lease_seconds or self.settings.research_job_lease_seconds),
                queue_name=queue,
            )
        else:
            job = job_repo.next_queued(queue_name=queue)
        if not job:
            return 0
        started_at = perf_counter()
        task = db.get(ResearchTask, job.task_id)
        if not task:
            job_repo.mark_failed(job, "task_not_found")
            self._record_job_metric()
            return 1
        payload = {}
        try:
            payload = orjson.loads(job.payload_json)
        except Exception:
            payload = {}

        if not worker_id:
            job_repo.mark_running(job)

        last_beat = perf_counter()

        def touch_lease() -> None:
            nonlocal last_beat
            if not worker_id:
                return
            hb_interval = max(3, int(self.settings.research_job_heartbeat_seconds))
            if perf_counter() - last_beat < hb_interval:
                return
            job_repo.heartbeat(
                job,
                worker_id=worker_id,
                lease_seconds=int(lease_seconds or self.settings.research_job_lease_seconds),
            )
            last_beat = perf_counter()

        job_error: str | None = None
        try:
            if job.job_type == ResearchJobType.PLAN:
                self._run_plan_job(db, task, payload, touch_lease=touch_lease)
            elif job.job_type == ResearchJobType.SEARCH:
                self._run_search_job(db, task, payload, touch_lease=touch_lease)
            elif job.job_type == ResearchJobType.FULLTEXT:
                self._run_fulltext_job(db, task, payload, touch_lease=touch_lease)
            elif job.job_type == ResearchJobType.GRAPH_BUILD:
                self._run_graph_job(db, task, payload, touch_lease=touch_lease)
            elif job.job_type == ResearchJobType.PAPER_SUMMARY:
                self._run_paper_summary_job(db, task, payload, touch_lease=touch_lease)
            else:
                raise ValueError(f"unsupported job type: {job.job_type}")
            job_repo.mark_done(job)
        except Exception as exc:
            logger.exception("research_job_failed task_id=%s job_id=%s", task.task_id, job.id)
            job_error = self._normalize_job_error(exc)
            max_attempts = max(1, int(self.settings.research_job_max_attempts))
            if job.attempts < max_attempts:
                base_delay = max(1, int(self.settings.research_job_backoff_seconds))
                delay_seconds = min(300, base_delay * (2 ** max(0, job.attempts - 1)))
                task.status = self._task_status_for_retry(job.job_type)
                task.updated_at = datetime.now(timezone.utc)
                db.add(task)
                db.flush()
                job_repo.mark_retry(job, error=job_error, delay_seconds=delay_seconds)
                logger.warning(
                    "research_job_retry_scheduled task_id=%s job_id=%s attempt=%s/%s delay_s=%s",
                    task.task_id,
                    job.id,
                    job.attempts,
                    max_attempts,
                    delay_seconds,
                )
            else:
                task.status = ResearchTaskStatus.FAILED
                task.updated_at = datetime.now(timezone.utc)
                db.add(task)
                db.flush()
                job_repo.mark_failed(job, job_error)
                self._notify_user(db, task.user_id, f"调研任务 {task.task_id} 失败：{job_error[:120]}")
        finally:
            latency_ms = int((perf_counter() - started_at) * 1000)
            self._record_job_metric(latency_ms=latency_ms)
        return 1

    def get_active_task(self, db: Session, user_id: int) -> ResearchTask | None:
        task_repo = ResearchTaskRepo(db)
        session = ResearchSessionRepo(db).get_or_create(user_id, page_size=self.settings.research_page_size)
        if session.active_task_id:
            row = task_repo.get_by_task_id(session.active_task_id, user_id=user_id)
            if row:
                return row
        items = task_repo.list_recent(user_id=user_id, limit=1)
        return items[0] if items else None

    def switch_task(self, db: Session, *, user_id: int, task_id: str) -> ResearchTask:
        row = ResearchTaskRepo(db).get_by_task_id(task_id.strip(), user_id=user_id)
        if not row:
            raise ValueError("task not found")
        session = ResearchSessionRepo(db).get_or_create(user_id, page_size=self.settings.research_page_size)
        ResearchSessionRepo(db).set_active_task(session, row.task_id)
        return row

    def list_tasks(self, db: Session, *, user_id: int, limit: int = 10) -> list[dict]:
        rows = ResearchTaskRepo(db).list_recent(user_id=user_id, limit=limit)
        return [self._task_to_dict(db, row) for row in rows]

    def get_task(self, db: Session, *, user_id: int, task_id: str) -> dict:
        row = ResearchTaskRepo(db).get_by_task_id(task_id, user_id=user_id)
        if not row:
            raise ValueError("task not found")
        return self._task_to_dict(db, row)

    def get_active_task_snapshot(self, db: Session, *, user_id: int) -> dict | None:
        row = self.get_active_task(db, user_id)
        if not row:
            return None
        return self._task_to_dict(db, row)

    def get_fulltext_status(self, db: Session, *, user_id: int, task_id: str) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        repo = ResearchPaperFulltextRepo(db)
        rows = repo.list_for_task(task.id)
        items = []
        for row in rows:
            items.append(
                {
                    "paper_id": row.paper_id,
                    "status": row.status.value,
                    "source_url": row.source_url,
                    "pdf_path": row.pdf_path,
                    "text_path": row.text_path,
                    "text_chars": row.text_chars,
                    "parser": row.parser,
                    "quality_score": row.quality_score,
                    "sections": _load_json_dict(row.sections_json),
                    "fail_reason": row.fail_reason,
                    "fetched_at": row.fetched_at,
                    "parsed_at": row.parsed_at,
                }
            )
        return {
            "task_id": task.task_id,
            "summary": repo.summary_for_task(task.id),
            "items": items,
        }

    def upload_pdf_for_paper(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        paper_token: str,
        filename: str,
        content: bytes,
    ) -> dict:
        if not content:
            raise ValueError("empty pdf content")
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        paper = ResearchPaperRepo(db).get_by_token(task.id, paper_token)
        if not paper:
            raise ValueError("paper not found")
        paper_key = _paper_token(paper)
        pdf_dir = Path(self.settings.research_artifact_dir).expanduser().resolve() / task.task_id / "fulltext"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename or f"{paper_key}.pdf")
        if not safe_name.lower().endswith(".pdf"):
            safe_name = f"{safe_name}.pdf"
        pdf_path = pdf_dir / safe_name
        pdf_path.write_bytes(content)
        text, meta = self._parse_pdf_bytes(content)
        sections = _extract_sections_lite(text)
        quality = _estimate_text_quality(text)
        text_path = pdf_dir / f"{safe_name.rsplit('.', 1)[0]}.txt"
        text_path.write_text(text, encoding="utf-8")
        row = ResearchPaperFulltextRepo(db).upsert(
            task_id=task.id,
            paper_id=paper_key,
            source_url=paper.url,
            status=ResearchPaperFulltextStatus.PARSED.value,
            pdf_path=str(pdf_path),
            text_path=str(text_path),
            text_chars=len(text),
            parser=str(meta.get("parser") or "")[:32] or None,
            quality_score=quality,
            sections_json=orjson.dumps(sections).decode("utf-8"),
            fail_reason=None,
            fetched_at=datetime.now(timezone.utc),
            parsed_at=datetime.now(timezone.utc),
        )
        return {
            "paper_id": row.paper_id,
            "status": row.status.value,
            "pdf_path": row.pdf_path,
            "text_path": row.text_path,
            "text_chars": row.text_chars,
        }

    def get_paper_detail(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        paper_token: str,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        paper = ResearchPaperRepo(db).get_by_token(task.id, paper_token)
        if not paper:
            raise ValueError("paper not found")
        paper_id = _paper_token(paper)
        fulltext = ResearchPaperFulltextRepo(db).get(task.id, paper_id)
        return {
            "task_id": task.task_id,
            "paper_id": paper_id,
            "title": paper.title,
            "authors": _load_json_list(paper.authors_json),
            "year": paper.year,
            "venue": paper.venue,
            "doi": paper.doi,
            "url": paper.url,
            "abstract": paper.abstract,
            "method_summary": paper.method_summary,
            "source": paper.source,
            "fulltext_status": fulltext.status.value if fulltext else None,
            "saved": bool(paper.saved),
            "saved_path": paper.saved_path,
            "saved_bib_path": paper.saved_bib_path,
            "saved_at": paper.saved_at,
            "key_points_status": paper.key_points_status or "none",
            "key_points_source": paper.key_points_source,
            "key_points": paper.key_points,
            "key_points_error": paper.key_points_error,
            "key_points_updated_at": paper.key_points_updated_at,
        }

    def list_saved_papers(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        limit: int = 200,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        rows = ResearchPaperRepo(db).list_saved_for_task(task.id, limit=limit)
        return {
            "task_id": task.task_id,
            "items": [
                {
                    "paper_id": _paper_token(row),
                    "title": row.title,
                    "year": row.year,
                    "doi": row.doi,
                    "saved_path": row.saved_path,
                    "saved_bib_path": row.saved_bib_path,
                    "saved_at": row.saved_at,
                }
                for row in rows
            ],
        }

    def save_paper(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        paper_token: str,
        subdir: str | None = None,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        paper_repo = ResearchPaperRepo(db)
        paper = paper_repo.get_by_token(task.id, paper_token)
        if not paper:
            raise ValueError("paper not found")
        base_dir = Path(self.settings.research_save_base_dir).expanduser().resolve()
        safe_subdir = (subdir or "").strip()
        if safe_subdir:
            p = Path(safe_subdir)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("invalid subdir")
        target_dir = (base_dir / safe_subdir / task.task_id).resolve()
        if not str(target_dir).startswith(str(base_dir)):
            raise ValueError("invalid subdir")
        target_dir.mkdir(parents=True, exist_ok=True)

        paper_key = re.sub(r"[^a-zA-Z0-9._-]+", "_", _paper_token(paper))[:120] or f"paper_{paper.id}"
        md_path = target_dir / f"{paper_key}.md"
        bib_path = target_dir / f"{paper_key}.bib"
        md_path.write_text(self._render_saved_paper_markdown(task, paper), encoding="utf-8")
        bib_path.write_text(self._render_bib([paper]), encoding="utf-8")
        row = paper_repo.mark_saved(paper, md_path=str(md_path), bib_path=str(bib_path))
        return {
            "task_id": task.task_id,
            "paper_id": _paper_token(row),
            "saved": bool(row.saved),
            "saved_path": str(md_path),
            "saved_bib_path": str(bib_path),
            "saved_at": row.saved_at,
        }

    def enqueue_paper_summary(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        paper_token: str,
    ) -> dict:
        if not self.settings.research_summary_enabled:
            raise ValueError("research summary is disabled")
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        paper_repo = ResearchPaperRepo(db)
        paper = paper_repo.get_by_token(task.id, paper_token)
        if not paper:
            raise ValueError("paper not found")
        paper_repo.update_key_points(paper, status="queued", error=None)
        payload = {"paper_token": _paper_token(paper)}
        ResearchJobRepo(db).enqueue(task.id, ResearchJobType.PAPER_SUMMARY, payload, queue_name=self.settings.research_queue_name)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return {
            "task_id": task.task_id,
            "paper_id": _paper_token(paper),
            "key_points_status": "queued",
            "queued": True,
        }

    def get_graph_snapshot(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        direction_index: int | None = None,
        round_id: int | None = None,
        view: str = ResearchGraphViewType.CITATION.value,
        include_papers: bool = False,
        paper_limit: int | None = None,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        view_norm = (view or ResearchGraphViewType.CITATION.value).strip().lower()
        if view_norm == ResearchGraphViewType.TREE.value:
            data = self._build_tree_graph(db, task, include_papers=include_papers, paper_limit=paper_limit)
            return {"task_id": task.task_id, "view": view_norm, **data}
        row = ResearchGraphSnapshotRepo(db).latest_for_task(
            task.id,
            direction_index=direction_index,
            round_id=round_id,
            view_type=view_norm,
        )
        if not row:
            return {
                "task_id": task.task_id,
                "view": view_norm,
                "direction_index": direction_index,
                "round_id": round_id,
                "depth": int(self.settings.research_graph_depth_default),
                "status": ResearchGraphBuildStatus.QUEUED.value,
                "nodes": [],
                "edges": [],
                "stats": {},
            }
        return {
            "task_id": task.task_id,
            "view": row.view_type.value,
            "direction_index": row.direction_index,
            "round_id": row.round_id,
            "depth": row.depth,
            "status": row.status.value,
            "nodes": _load_json_list_of_dict(row.nodes_json),
            "edges": _load_json_list_of_dict(row.edges_json),
            "stats": _load_json_dict(row.stats_json),
        }

    def page_direction_papers(
        self,
        db: Session,
        *,
        user_id: int,
        direction_index: int,
        page: int,
    ) -> dict:
        task = self.get_active_task(db, user_id)
        if not task:
            raise ValueError("no active task")
        direction = ResearchDirectionRepo(db).get_by_index(task.id, direction_index)
        if not direction:
            raise ValueError("direction not found")
        items = ResearchPaperRepo(db).list_for_direction(direction.id)
        page_size = self.settings.research_page_size
        page = max(1, page)
        start = (page - 1) * page_size
        end = start + page_size
        sliced = items[start:end]
        ResearchSessionRepo(db).set_pagination(
            ResearchSessionRepo(db).get_or_create(user_id, page_size=page_size),
            direction_index=direction_index,
            page=page,
        )
        cards = []
        for idx, row in enumerate(sliced, start=start + 1):
            cards.append(
                {
                    "index": idx,
                    "title": row.title,
                    "authors": _load_json_list(row.authors_json),
                    "year": row.year,
                    "venue": row.venue,
                    "doi": row.doi,
                    "url": row.url,
                    "abstract": row.abstract,
                    "method_summary": row.method_summary,
                    "source": row.source,
                    "saved": bool(row.saved),
                }
            )
        return {
            "task_id": task.task_id,
            "direction_index": direction_index,
            "page": page,
            "page_size": page_size,
            "total": len(items),
            "items": cards,
        }

    def get_paper_by_index(self, db: Session, *, user_id: int, index: int) -> dict:
        task = self.get_active_task(db, user_id)
        if not task:
            raise ValueError("no active task")
        papers = ResearchPaperRepo(db).list_for_task(task.id)
        if index < 1 or index > len(papers):
            raise ValueError("paper index out of range")
        row = papers[index - 1]
        return {
            "index": index,
            "paper_id": _paper_token(row),
            "title": row.title,
            "authors": _load_json_list(row.authors_json),
            "year": row.year,
            "venue": row.venue,
            "doi": row.doi,
            "url": row.url,
            "abstract": row.abstract,
            "method_summary": row.method_summary,
            "source": row.source,
        }

    def get_paper_by_doi(self, db: Session, *, user_id: int, doi: str) -> dict:
        task = self.get_active_task(db, user_id)
        if not task:
            raise ValueError("no active task")
        doi_norm = doi.strip().lower()
        for row in ResearchPaperRepo(db).list_for_task(task.id):
            if (row.doi or "").strip().lower() == doi_norm:
                return {
                    "paper_id": _paper_token(row),
                    "title": row.title,
                    "authors": _load_json_list(row.authors_json),
                    "year": row.year,
                    "venue": row.venue,
                    "doi": row.doi,
                    "url": row.url,
                    "abstract": row.abstract,
                    "method_summary": row.method_summary,
                    "source": row.source,
                }
        raise ValueError("paper not found")

    def export_task(self, db: Session, *, user_id: int, fmt: str = "md") -> str:
        fmt_norm = (fmt or "md").lower().strip()
        if fmt_norm not in {"md", "bib", "json"}:
            raise ValueError("format must be one of md|bib|json")
        try:
            task = self.get_active_task(db, user_id)
            if not task:
                raise ValueError("no active task")
            base = Path(self.settings.research_artifact_dir).expanduser().resolve() / task.task_id
            base.mkdir(parents=True, exist_ok=True)
            directions = ResearchDirectionRepo(db).list_for_task(task.id)
            papers = ResearchPaperRepo(db).list_for_task(task.id)

            report_path = base / "report.md"
            bib_path = base / "papers.bib"
            json_path = base / "papers.json"
            report_path.write_text(self._render_report(task, directions, papers), encoding="utf-8")
            bib_path.write_text(self._render_bib(papers), encoding="utf-8")
            json_path.write_text(self._render_json(task, directions, papers), encoding="utf-8")

            if fmt_norm == "bib":
                self._record_export_metric(success=True)
                return str(bib_path)
            if fmt_norm == "json":
                self._record_export_metric(success=True)
                return str(json_path)
            self._record_export_metric(success=True)
            return str(report_path)
        except Exception:
            self._record_export_metric(success=False)
            raise

    def _run_plan_job(
        self,
        db: Session,
        task: ResearchTask,
        payload: dict,
        *,
        touch_lease: Callable[[], None] | None = None,
    ) -> None:
        constraints = _load_json_dict(task.constraints_json)
        seed_rows = self._build_seed_corpus_for_task(db, task=task, constraints=constraints)
        directions = self._plan_directions_from_seed(task.topic, constraints, seed_rows)
        ResearchDirectionRepo(db).replace_for_task(task, directions)
        task.status = ResearchTaskStatus.CREATED
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        lines = [f"调研任务 {task.task_id} 方向已生成："]
        for idx, item in enumerate(directions, start=1):
            lines.append(f"{idx}. {item['name']}")
        lines.append('回复“调研 选择 2”查看方向 2。')
        if touch_lease:
            touch_lease()
        self._notify_user(db, task.user_id, "\n".join(lines))

    def _run_search_job(
        self,
        db: Session,
        task: ResearchTask,
        payload: dict,
        *,
        touch_lease: Callable[[], None] | None = None,
    ) -> None:
        constraints = _load_json_dict(task.constraints_json)
        constraints_override = payload.get("constraints_override")
        if isinstance(constraints_override, dict):
            for key in ("year_from", "year_to", "sources"):
                value = constraints_override.get(key)
                if value:
                    constraints[key] = value
        direction_index = int(payload.get("direction_index") or 1)
        round_id = _to_int_or_none(payload.get("round_id"))
        default_top_n = int(constraints.get("top_n") or self.settings.research_topn_default)
        top_n = int(payload.get("top_n") or default_top_n)
        force_refresh = bool(payload.get("force_refresh") or False)
        top_n = max(1, min(100, top_n))
        direction = ResearchDirectionRepo(db).get_by_index(task.id, direction_index)
        if not direction:
            raise ValueError("direction not found")

        allowed_sources = _resolve_sources(constraints.get("sources"), self.settings.research_sources_default)
        if not allowed_sources:
            allowed_sources = {"semantic_scholar"}

        query_terms = _load_json_list(payload.get("explicit_queries") or "[]")
        if not query_terms and round_id:
            round_row = ResearchRoundRepo(db).get(round_id)
            if round_row and round_row.task_id == task.id:
                query_terms = _load_json_list(round_row.query_terms_json)
                ResearchRoundRepo(db).update_status(round_row, ResearchRoundStatus.RUNNING.value)
        if not query_terms:
            query_terms = _load_json_list(direction.queries_json) or [direction.name]

        exclude_terms = _load_json_list(direction.exclude_terms_json)
        all_papers: list[dict] = []
        cache_repo = ResearchSearchCacheRepo(db)
        for query in query_terms[:4]:
            effective_query = _merge_query_and_excludes(query, exclude_terms)
            ordered_sources = [src for src in ("semantic_scholar", "arxiv") if src in allowed_sources]
            for source in ordered_sources:
                result = self._search_with_cache(
                    cache_repo=cache_repo,
                    task=task,
                    direction_index=direction_index,
                    source=source,
                    query=effective_query,
                    top_n=top_n,
                    constraints=constraints,
                    force_refresh=force_refresh,
                    allow_semantic_fallback=("arxiv" not in allowed_sources),
                )
                self._record_source_status(source, result.status)
                if result.error:
                    logger.warning(
                        "research_source_fetch_error task_id=%s source=%s status=%s error=%s",
                        task.task_id,
                        source,
                        result.status,
                        result.error,
                    )
                all_papers.extend(result.papers)
                if touch_lease:
                    touch_lease()
        papers = self._dedupe_papers(all_papers)
        papers = papers[: max(1, top_n)]
        for row in papers:
            row["method_summary"] = self._summarize_method(row.get("abstract") or "")

        paper_repo = ResearchPaperRepo(db)
        if round_id:
            rows = paper_repo.upsert_direction_papers(direction, papers)
            ResearchRoundPaperRepo(db).replace_for_round(round_id=round_id, rows=rows, role="seed")
            round_row = ResearchRoundRepo(db).get(round_id)
            if round_row and round_row.task_id == task.id:
                ResearchRoundRepo(db).update_status(round_row, ResearchRoundStatus.DONE.value)
        else:
            rows = paper_repo.replace_direction_papers(direction, papers)
        ResearchDirectionRepo(db).update_papers_count(direction, len(paper_repo.list_for_direction(direction.id)))

        task.status = ResearchTaskStatus.DONE
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        if round_id:
            msg = (
                f"已完成方向 {direction_index} 的第 {round_id} 轮检索，新增 {len(rows)} 篇。"
                "请在本地调研页面继续下一轮。"
            )
        else:
            msg = f"已完成方向 {direction_index} 检索，收录 {len(rows)} 篇。回复“调研 下一页”浏览结果，回复“调研 导出”导出文件。"
        self._notify_user(db, task.user_id, msg)

    def _run_fulltext_job(
        self,
        db: Session,
        task: ResearchTask,
        payload: dict,
        *,
        touch_lease: Callable[[], None] | None = None,
    ) -> None:
        force = bool(payload.get("force") or False)
        paper_filter = {str(x).strip() for x in _load_json_list(payload.get("paper_ids")) if str(x).strip()}
        papers = ResearchPaperRepo(db).list_for_task(task.id)
        fulltext_repo = ResearchPaperFulltextRepo(db)
        base_dir = Path(self.settings.research_artifact_dir).expanduser().resolve() / task.task_id / "fulltext"
        base_dir.mkdir(parents=True, exist_ok=True)
        for paper in papers:
            paper_id = _paper_token(paper)
            if paper_filter and paper_id not in paper_filter:
                continue
            current = fulltext_repo.get(task.id, paper_id)
            if (
                current
                and current.status == ResearchPaperFulltextStatus.PARSED
                and not force
            ):
                continue
            fulltext_repo.upsert(
                task_id=task.id,
                paper_id=paper_id,
                source_url=paper.url,
                status=ResearchPaperFulltextStatus.FETCHING.value,
                fail_reason=None,
            )
            pdf_bytes, source_url, error = self._download_pdf_for_paper(paper)
            if not pdf_bytes:
                fulltext_repo.upsert(
                    task_id=task.id,
                    paper_id=paper_id,
                    source_url=source_url or paper.url,
                    status=ResearchPaperFulltextStatus.NEED_UPLOAD.value,
                    fail_reason=error or "pdf_unavailable",
                )
                continue
            file_stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", paper_id)[:80] or f"paper_{paper.id}"
            pdf_path = base_dir / f"{file_stem}.pdf"
            pdf_path.write_bytes(pdf_bytes)
            fetched_at = datetime.now(timezone.utc)
            fulltext_repo.upsert(
                task_id=task.id,
                paper_id=paper_id,
                source_url=source_url or paper.url,
                status=ResearchPaperFulltextStatus.FETCHED.value,
                pdf_path=str(pdf_path),
                fail_reason=None,
                fetched_at=fetched_at,
            )
            try:
                text, meta = self._parse_pdf_bytes(pdf_bytes)
                sections = _extract_sections_lite(text)
                quality = _estimate_text_quality(text)
                text_path = base_dir / f"{file_stem}.txt"
                text_path.write_text(text, encoding="utf-8")
                fulltext_repo.upsert(
                    task_id=task.id,
                    paper_id=paper_id,
                    source_url=source_url or paper.url,
                    status=ResearchPaperFulltextStatus.PARSED.value,
                    pdf_path=str(pdf_path),
                    text_path=str(text_path),
                    text_chars=len(text),
                    parser=str(meta.get("parser") or "")[:32] or None,
                    quality_score=quality,
                    sections_json=orjson.dumps(sections).decode("utf-8"),
                    fail_reason=None,
                    fetched_at=fetched_at,
                    parsed_at=datetime.now(timezone.utc),
                )
            except Exception as exc:
                fulltext_repo.upsert(
                    task_id=task.id,
                    paper_id=paper_id,
                    source_url=source_url or paper.url,
                    status=ResearchPaperFulltextStatus.NEED_UPLOAD.value,
                    pdf_path=str(pdf_path),
                    fail_reason=f"parse_failed:{exc}",
                    fetched_at=fetched_at,
                )
            if touch_lease:
                touch_lease()

        task.status = ResearchTaskStatus.DONE
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        summary = fulltext_repo.summary_for_task(task.id)
        self._notify_user(
            db,
            task.user_id,
            (
                f"调研任务 {task.task_id} 全文处理完成。"
                f"已解析 {summary.get('parsed', 0)} 篇，待上传 {summary.get('need_upload', 0)} 篇。"
            ),
        )

    def _run_graph_job(
        self,
        db: Session,
        task: ResearchTask,
        payload: dict,
        *,
        touch_lease: Callable[[], None] | None = None,
    ) -> None:
        view = str(payload.get("view") or ResearchGraphViewType.CITATION.value).strip().lower()
        direction_index = _to_int_or_none(payload.get("direction_index"))
        round_id = _to_int_or_none(payload.get("round_id"))
        seed_top_n = max(1, int(payload.get("seed_top_n") or self.settings.research_graph_seed_topn))
        expand_limit = max(1, int(payload.get("expand_limit_per_paper") or self.settings.research_graph_expand_limit_per_paper))
        citation_sources = _resolve_citation_sources(payload.get("citation_sources"), self.settings.research_citation_sources_default)
        depth = max(1, int(self.settings.research_graph_depth_default))

        if view == ResearchGraphViewType.TREE.value:
            tree = self._build_tree_graph(db, task)
            ResearchGraphSnapshotRepo(db).upsert_snapshot(
                task_id=task.id,
                direction_index=direction_index,
                round_id=round_id,
                view_type=ResearchGraphViewType.TREE.value,
                depth=depth,
                nodes=tree["nodes"],
                edges=tree["edges"],
                stats=tree["stats"],
                status=ResearchGraphBuildStatus.DONE.value,
            )
            task.status = ResearchTaskStatus.DONE
            task.updated_at = datetime.now(timezone.utc)
            db.add(task)
            db.flush()
            return

        paper_repo = ResearchPaperRepo(db)
        fulltext_map = {
            row.paper_id: row.status.value
            for row in ResearchPaperFulltextRepo(db).list_for_task(task.id)
        }
        direction_repo = ResearchDirectionRepo(db)
        all_directions = direction_repo.list_for_task(task.id)
        direction_by_id = {d.id: d for d in all_directions}
        source_coverage: dict[str, int] = {}
        provider_errors: dict[str, str] = {}
        fallback_used = False

        if round_id is not None:
            round_row = ResearchRoundRepo(db).get(round_id)
            if not round_row or round_row.task_id != task.id:
                raise ValueError("round not found")
            direction_index = round_row.direction_index
            refs = ResearchRoundPaperRepo(db).list_for_round(round_row.id)
            rank_map = {x.paper_id: x.rank for x in refs}
            seeds = paper_repo.list_by_ids([x.paper_id for x in refs])
            seeds.sort(key=lambda x: rank_map.get(x.id, 999999))
            seed_papers = seeds[:seed_top_n]
            direction_row = direction_repo.get_by_index(task.id, direction_index)
            used_directions = [direction_row] if direction_row else []
        elif direction_index is not None:
            direction_row = direction_repo.get_by_index(task.id, direction_index)
            if not direction_row:
                raise ValueError("direction not found")
            seed_papers = paper_repo.list_for_direction(direction_row.id)[:seed_top_n]
            used_directions = [direction_row]
        else:
            seed_papers = paper_repo.list_for_task(task.id)[:seed_top_n]
            used_directions = all_directions

        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        edge_seen: set[tuple[str, str, str]] = set()
        citation_edges: list[dict] = []

        topic_id = f"topic:{task.task_id}"
        nodes[topic_id] = {
            "id": topic_id,
            "type": "topic",
            "label": task.topic,
            "source": "memomate",
            "direction_index": None,
            "score": None,
            "fulltext_status": None,
        }
        for d in used_directions:
            d_node_id = f"direction:{task.task_id}:{d.direction_index}"
            nodes[d_node_id] = {
                "id": d_node_id,
                "type": "direction",
                "label": d.name,
                "source": "memomate",
                "direction_index": d.direction_index,
                "score": None,
                "fulltext_status": None,
            }
            key = (topic_id, d_node_id, "topic_direction")
            if key not in edge_seen:
                edges.append({"source": topic_id, "target": d_node_id, "type": "topic_direction", "weight": 1.0})
                edge_seen.add(key)

        for paper in seed_papers:
            p_id = _paper_token(paper)
            direction_idx = direction_by_id.get(paper.direction_id).direction_index if paper.direction_id in direction_by_id else None
            nodes[p_id] = {
                "id": p_id,
                "type": "paper",
                "label": paper.title[:240],
                "year": paper.year,
                "source": paper.source,
                "direction_index": direction_idx,
                "score": None,
                "fulltext_status": fulltext_map.get(p_id),
            }
            if direction_idx is not None:
                d_node_id = f"direction:{task.task_id}:{direction_idx}"
                key = (d_node_id, p_id, "direction_paper")
                if key not in edge_seen:
                    edges.append({"source": d_node_id, "target": p_id, "type": "direction_paper", "weight": 1.0})
                    edge_seen.add(key)

            neighbor_result = self._fetch_citation_neighbors_multi(
                db,
                task=task,
                paper=paper,
                limit=expand_limit,
                ordered_sources=citation_sources,
                force_refresh=bool(payload.get("force_refresh") or False),
            )
            for src, count in (neighbor_result.get("source_coverage") or {}).items():
                source_coverage[src] = source_coverage.get(src, 0) + int(count)
            if neighbor_result.get("fallback_used"):
                fallback_used = True
            for src, err in (neighbor_result.get("provider_errors") or {}).items():
                provider_errors[src] = err
            for item in neighbor_result["items"]:
                n_id = str(item.get("neighbor_id") or "").strip()
                if not n_id:
                    continue
                if n_id not in nodes:
                    nodes[n_id] = {
                        "id": n_id,
                        "type": "paper",
                        "label": str(item.get("title") or "Untitled")[:240],
                        "year": _to_int_or_none(item.get("year")),
                        "source": str(item.get("source") or "semantic_scholar"),
                        "direction_index": direction_idx,
                        "score": None,
                        "fulltext_status": fulltext_map.get(n_id),
                    }
                src = str(item.get("source_id") or "").strip()
                dst = str(item.get("target_id") or "").strip()
                edge_type = str(item.get("edge_type") or "cites").strip()
                if not src or not dst:
                    continue
                key = (src, dst, edge_type)
                if key in edge_seen:
                    continue
                edge_seen.add(key)
                edge_item = {
                    "source": src,
                    "target": dst,
                    "type": edge_type,
                    "weight": float(item.get("weight") or 1.0),
                    "source_name": str(item.get("source_name") or "semantic_scholar"),
                }
                edges.append(edge_item)
                if edge_type in {"cites", "cited_by"}:
                    citation_edges.append(edge_item)
            if touch_lease:
                touch_lease()

        stats = self._compute_graph_stats(nodes=list(nodes.values()), edges=edges)
        stats["source_coverage"] = source_coverage
        stats["fallback_used"] = fallback_used
        stats["provider_errors"] = provider_errors
        for node in nodes.values():
            if node["id"] in stats.get("scores", {}):
                node["score"] = float(stats["scores"][node["id"]])

        ResearchCitationEdgeRepo(db).replace_for_task(task.id, citation_edges)
        ResearchGraphSnapshotRepo(db).upsert_snapshot(
            task_id=task.id,
            direction_index=direction_index,
            round_id=round_id,
            view_type=ResearchGraphViewType.CITATION.value,
            depth=depth,
            nodes=list(nodes.values()),
            edges=edges,
            stats=stats,
            status=ResearchGraphBuildStatus.DONE.value,
        )
        task.status = ResearchTaskStatus.DONE
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        self._notify_user(
            db,
            task.user_id,
            (
                f"调研任务 {task.task_id} 图谱构建完成。"
                f"节点 {stats.get('node_count', 0)}，边 {stats.get('edge_count', 0)}。"
                "回复“调研 图谱 查看”查看。"
            ),
        )

    def _run_paper_summary_job(
        self,
        db: Session,
        task: ResearchTask,
        payload: dict,
        *,
        touch_lease: Callable[[], None] | None = None,
    ) -> None:
        paper_token = str(payload.get("paper_token") or "").strip()
        if not paper_token:
            raise ValueError("paper_token is required")
        paper_repo = ResearchPaperRepo(db)
        paper = paper_repo.get_by_token(task.id, paper_token)
        if not paper:
            raise ValueError("paper not found")
        paper_repo.update_key_points(paper, status="running", error=None)
        paper_key = _paper_token(paper)
        fulltext = ResearchPaperFulltextRepo(db).get(task.id, paper_key)
        source = "abstract"
        text = (paper.abstract or "").strip()
        if fulltext and fulltext.text_path:
            try:
                p = Path(fulltext.text_path)
                if p.exists():
                    raw = p.read_text(encoding="utf-8", errors="ignore")
                    if raw.strip():
                        source = "fulltext"
                        text = raw.strip()
            except Exception:
                logger.exception("paper_summary_read_fulltext_failed paper=%s", paper_key)
        if not text:
            paper_repo.update_key_points(paper, status="failed", error="no_text_for_summary")
            raise ValueError("no text available for summary")
        if touch_lease:
            touch_lease()
        points = self._summarize_key_points(text=text, source=source)
        paper_repo.update_key_points(
            paper,
            status="done",
            key_points=points,
            source=source,
            error=None,
        )
        if paper.saved and paper.saved_path:
            try:
                md_path = Path(paper.saved_path)
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(self._render_saved_paper_markdown(task, paper), encoding="utf-8")
            except Exception:
                logger.exception("paper_summary_refresh_saved_file_failed paper=%s", paper_key)
        task.status = ResearchTaskStatus.DONE
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()

    def _search_with_cache(
        self,
        *,
        cache_repo: ResearchSearchCacheRepo,
        task: ResearchTask,
        direction_index: int,
        source: str,
        query: str,
        top_n: int,
        constraints: dict,
        force_refresh: bool,
        allow_semantic_fallback: bool,
    ) -> SearchFetchResult:
        year_from = _to_int_or_none(constraints.get("year_from"))
        year_to = _to_int_or_none(constraints.get("year_to"))
        cache_enabled = bool(self.settings.research_cache_enabled)
        if cache_enabled and not force_refresh:
            cached = cache_repo.get_valid(
                task_id=task.id,
                direction_index=direction_index,
                source=source,
                query_text=query,
                year_from=year_from,
                year_to=year_to,
                top_n=top_n,
            )
            if cached is not None:
                self._record_cache_hit()
                logger.info(
                    "research_cache_hit task_id=%s direction=%s source=%s top_n=%s",
                    task.task_id,
                    direction_index,
                    source,
                    top_n,
                )
                return SearchFetchResult(papers=cached, status="cache_hit")
        self._record_cache_miss()
        fetched = self._search_by_source(
            source=source,
            query=query,
            top_n=top_n,
            constraints=constraints,
            allow_semantic_fallback=allow_semantic_fallback,
        )
        if cache_enabled and fetched.status in {"ok", "ok_empty"}:
            try:
                cache_repo.upsert(
                    task_id=task.id,
                    direction_index=direction_index,
                    source=source,
                    query_text=query,
                    year_from=year_from,
                    year_to=year_to,
                    top_n=top_n,
                    papers=fetched.papers,
                    ttl_seconds=max(1, int(self.settings.research_cache_ttl_seconds)),
                )
            except Exception:
                logger.exception(
                    "research_cache_upsert_failed task_id=%s direction=%s source=%s",
                    task.task_id,
                    direction_index,
                    source,
                )
        return fetched

    def _search_by_source(
        self,
        *,
        source: str,
        query: str,
        top_n: int,
        constraints: dict,
        allow_semantic_fallback: bool,
    ) -> SearchFetchResult:
        source_key = source.strip().lower()
        if source_key == "semantic_scholar":
            papers, status, error = _normalize_source_response(
                self._search_semantic_scholar(query, top_n=top_n, constraints=constraints)
            )
            if allow_semantic_fallback and status in {"rate_limited", "http_5xx"} and not papers:
                fallback_papers, fb_status, fb_error = _normalize_source_response(
                    self._search_arxiv(query, top_n=top_n, constraints=constraints)
                )
                if fallback_papers:
                    return SearchFetchResult(
                        papers=fallback_papers,
                        status=f"fallback_arxiv_from_{status}",
                        error=fb_error,
                    )
                return SearchFetchResult(
                    papers=[],
                    status=f"fallback_arxiv_from_{status}",
                    error=fb_error or error,
                )
            return SearchFetchResult(papers=papers, status=status, error=error)
        if source_key == "arxiv":
            papers, status, error = _normalize_source_response(
                self._search_arxiv(query, top_n=top_n, constraints=constraints)
            )
            return SearchFetchResult(papers=papers, status=status, error=error)
        return SearchFetchResult(papers=[], status="unsupported_source", error=f"unsupported_source:{source_key}")

    def _download_pdf_for_paper(self, paper) -> tuple[bytes | None, str | None, str | None]:
        candidates = self._candidate_pdf_urls(paper)
        max_file_size = max(1, int(self.settings.research_fulltext_max_file_mb)) * 1024 * 1024
        timeout = max(5, int(self.settings.research_fulltext_timeout_seconds))
        retries = max(1, int(self.settings.research_fulltext_retries))
        trust_env = False
        for candidate in candidates:
            for attempt in range(retries):
                try:
                    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=trust_env) as client:
                        resp = client.get(candidate)
                    if resp.status_code >= 400:
                        if attempt < retries - 1:
                            sleep(0.2 * (2**attempt))
                            continue
                        break
                    content_type = (resp.headers.get("content-type") or "").lower()
                    data = resp.content
                    if len(data) > max_file_size:
                        return None, candidate, "file_too_large"
                    if "pdf" in content_type or data[:4] == b"%PDF":
                        return data, candidate, None
                    links = _extract_pdf_links_from_html(resp.text, candidate)
                    for link in links:
                        if link not in candidates:
                            candidates.append(link)
                except Exception as exc:
                    if attempt < retries - 1:
                        sleep(0.2 * (2**attempt))
                        continue
                    logger.warning("fulltext_download_failed paper=%s url=%s error=%s", getattr(paper, "id", None), candidate, exc)
                    break
        return None, None, "no_pdf_url_found"

    def _candidate_pdf_urls(self, paper) -> list[str]:
        out: list[str] = []
        url = (paper.url or "").strip()
        doi = (paper.doi or "").strip()
        if url:
            out.append(url)
            if "arxiv.org/abs/" in url:
                out.append(url.replace("/abs/", "/pdf/") + ".pdf")
            if "arxiv.org/pdf/" in url and not url.endswith(".pdf"):
                out.append(f"{url}.pdf")
            if not url.lower().endswith(".pdf"):
                out.append(f"{url}.pdf")
        if doi:
            out.append(f"https://doi.org/{doi}")
        unique: list[str] = []
        seen = set()
        for item in out:
            if not item or item in seen:
                continue
            unique.append(item)
            seen.add(item)
        return unique

    def _parse_pdf_bytes(self, content: bytes) -> tuple[str, dict]:
        if fitz is not None:
            doc = fitz.open(stream=content, filetype="pdf")
            parts = [page.get_text("text") for page in doc]
            text = "\n".join(parts).strip()
            if text:
                return _normalize_pdf_text(text), {"parser": "pymupdf", "pages": len(doc)}
        if pdfminer_extract_text is not None:
            text = pdfminer_extract_text(BytesIO(content)) or ""
            text = text.strip()
            if text:
                return _normalize_pdf_text(text), {"parser": "pdfminer"}
        raise ValueError("pdf_parse_failed")

    def _fetch_citation_neighbors_multi(
        self,
        db: Session,
        *,
        task: ResearchTask,
        paper,
        limit: int,
        ordered_sources: list[str],
        force_refresh: bool,
    ) -> dict:
        paper_key = _paper_token(paper)
        cache_repo = ResearchCitationFetchCacheRepo(db)
        source_coverage: dict[str, int] = {}
        provider_errors: dict[str, str] = {}
        fallback_used = False
        merged: list[dict] = []

        for idx, source in enumerate(ordered_sources):
            source_key = (source or "").strip().lower()
            if not source_key:
                continue
            payload: dict | None = None
            if not force_refresh:
                payload = cache_repo.get_valid(task_id=task.id, paper_key=paper_key, source=source_key)
            if payload:
                items = _load_json_list_of_dict(payload.get("items"))
                status = str(payload.get("status") or "cached")
                error = payload.get("error")
            else:
                items, status, error = self._fetch_citation_by_source(source_key, paper=paper, limit=limit)
                cache_repo.upsert(
                    task_id=task.id,
                    paper_key=paper_key,
                    source=source_key,
                    payload={"items": items, "status": status, "error": error},
                    ttl_seconds=max(1, int(self.settings.research_citation_cache_ttl_seconds)),
                )
            if items:
                source_coverage[source_key] = len(items)
                merged.extend(items)
                if idx > 0:
                    fallback_used = True
                break
            provider_errors[source_key] = str(error or status or "empty")

        seen: set[tuple[str, str, str]] = set()
        unique: list[dict] = []
        for item in merged:
            src = str(item.get("source_id") or "").strip()
            dst = str(item.get("target_id") or "").strip()
            edge_type = str(item.get("edge_type") or "cites").strip()
            if not src or not dst:
                continue
            key = (src, dst, edge_type)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return {
            "items": unique,
            "source_coverage": source_coverage,
            "provider_errors": provider_errors,
            "fallback_used": fallback_used,
        }

    def _fetch_citation_by_source(self, source: str, *, paper, limit: int) -> tuple[list[dict], str, str | None]:
        if source == "semantic_scholar":
            return self._fetch_citation_neighbors_semantic(paper, limit=limit)
        if source == "openalex":
            return self._fetch_citation_neighbors_openalex(paper, limit=limit)
        if source == "crossref":
            return self._fetch_citation_neighbors_crossref(paper, limit=limit)
        return [], "unsupported_source", f"unsupported_source:{source}"

    def _fetch_citation_neighbors_semantic(self, paper, *, limit: int) -> tuple[list[dict], str, str | None]:
        identifier = _semantic_scholar_identifier_for_paper(paper)
        if not identifier:
            return [], "missing_identifier", "missing_identifier"
        api_key = self.settings.semantic_scholar_api_key.strip()
        headers = {"User-Agent": "MemoMate/0.1 (citation-graph)"}
        if api_key:
            headers["x-api-key"] = api_key
        url = f"https://api.semanticscholar.org/graph/v1/paper/{quote_plus(identifier)}"
        fields = (
            "title,year,externalIds,references.paperId,references.title,references.year,references.externalIds,"
            "citations.paperId,citations.title,citations.year,citations.externalIds"
        )
        try:
            with httpx.Client(timeout=20, trust_env=False) as client:
                resp = client.get(url, params={"fields": fields}, headers=headers)
            if resp.status_code >= 400:
                return [], f"http_{resp.status_code}", f"http_{resp.status_code}"
            payload = resp.json()
        except Exception as exc:
            return [], "transport_error", str(exc)

        base_id = _paper_token(paper)
        items: list[dict] = []
        references = payload.get("references") if isinstance(payload, dict) else []
        citations = payload.get("citations") if isinstance(payload, dict) else []
        for row in (references or [])[:limit]:
            neighbor = _normalize_neighbor_from_s2(row)
            if not neighbor:
                continue
            items.append(
                {
                    "source_id": base_id,
                    "target_id": neighbor["id"],
                    "neighbor_id": neighbor["id"],
                    "title": neighbor["title"],
                    "year": neighbor.get("year"),
                    "source": "semantic_scholar",
                    "edge_type": "cites",
                    "source_name": "semantic_scholar",
                    "weight": 1.0,
                }
            )
        for row in (citations or [])[:limit]:
            neighbor = _normalize_neighbor_from_s2(row)
            if not neighbor:
                continue
            items.append(
                {
                    "source_id": neighbor["id"],
                    "target_id": base_id,
                    "neighbor_id": neighbor["id"],
                    "title": neighbor["title"],
                    "year": neighbor.get("year"),
                    "source": "semantic_scholar",
                    "edge_type": "cited_by",
                    "source_name": "semantic_scholar",
                    "weight": 1.0,
                }
            )
        return items, ("ok" if items else "ok_empty"), None

    def _fetch_citation_neighbors_openalex(self, paper, *, limit: int) -> tuple[list[dict], str, str | None]:
        doi = (paper.doi or "").strip().lower()
        if not doi:
            return [], "missing_doi", "missing_doi"
        base_id = _paper_token(paper)
        items: list[dict] = []
        try:
            with httpx.Client(timeout=20, trust_env=False) as client:
                work_resp = client.get(
                    f"https://api.openalex.org/works/https://doi.org/{quote_plus(doi)}",
                    params={"select": "id,referenced_works,cited_by_api_url"},
                )
                if work_resp.status_code >= 400:
                    return [], f"http_{work_resp.status_code}", f"http_{work_resp.status_code}"
                work = work_resp.json()
                refs = work.get("referenced_works") if isinstance(work, dict) else []
                for ref in (refs or [])[:limit]:
                    ref_id = _normalize_openalex_id(ref)
                    if not ref_id:
                        continue
                    items.append(
                        {
                            "source_id": base_id,
                            "target_id": ref_id,
                            "neighbor_id": ref_id,
                            "title": ref_id,
                            "year": None,
                            "source": "openalex",
                            "edge_type": "cites",
                            "source_name": "openalex",
                            "weight": 1.0,
                        }
                    )
                cited_url = work.get("cited_by_api_url") if isinstance(work, dict) else None
                if isinstance(cited_url, str) and cited_url.strip():
                    cited_resp = client.get(cited_url, params={"per-page": limit, "select": "id,display_name,publication_year"})
                    if cited_resp.status_code < 400:
                        cited_data = cited_resp.json()
                        results = cited_data.get("results") if isinstance(cited_data, dict) else []
                        for row in (results or [])[:limit]:
                            cid = _normalize_openalex_id(row.get("id"))
                            if not cid:
                                continue
                            items.append(
                                {
                                    "source_id": cid,
                                    "target_id": base_id,
                                    "neighbor_id": cid,
                                    "title": str(row.get("display_name") or cid)[:240],
                                    "year": _to_int_or_none(row.get("publication_year")),
                                    "source": "openalex",
                                    "edge_type": "cited_by",
                                    "source_name": "openalex",
                                    "weight": 1.0,
                                }
                            )
        except Exception as exc:
            return [], "transport_error", str(exc)
        return items, ("ok" if items else "ok_empty"), None

    def _fetch_citation_neighbors_crossref(self, paper, *, limit: int) -> tuple[list[dict], str, str | None]:
        doi = (paper.doi or "").strip().lower()
        if not doi:
            return [], "missing_doi", "missing_doi"
        base_id = _paper_token(paper)
        items: list[dict] = []
        url = f"https://api.crossref.org/works/{quote_plus(doi)}"
        try:
            with httpx.Client(timeout=20, trust_env=False) as client:
                resp = client.get(url)
            if resp.status_code >= 400:
                return [], f"http_{resp.status_code}", f"http_{resp.status_code}"
            payload = resp.json()
        except Exception as exc:
            return [], "transport_error", str(exc)
        message = payload.get("message") if isinstance(payload, dict) else {}
        references = message.get("reference") if isinstance(message, dict) else []
        for row in (references or [])[:limit]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("article-title") or row.get("series-title") or "").strip()
            neighbor_doi = str(row.get("DOI") or "").strip().lower()
            neighbor_id = _citation_neighbor_id(title=title, doi=neighbor_doi, fallback_seed=str(row.get("key") or "ref"))
            if not neighbor_id:
                continue
            year = _to_int_or_none(row.get("year"))
            items.append(
                {
                    "source_id": base_id,
                    "target_id": neighbor_id,
                    "neighbor_id": neighbor_id,
                    "title": (title or neighbor_id)[:240],
                    "year": year,
                    "source": "crossref",
                    "edge_type": "cites",
                    "source_name": "crossref",
                    "weight": 1.0,
                }
            )
        return items, ("ok" if items else "ok_empty"), None

    def _compute_graph_stats(self, *, nodes: list[dict], edges: list[dict]) -> dict:
        paper_nodes = [n for n in nodes if n.get("type") == "paper"]
        citation_edges = [e for e in edges if e.get("type") in {"cites", "cited_by"}]
        scores: dict[str, float] = {}
        if nx is not None and paper_nodes:
            g = nx.DiGraph()
            for node in paper_nodes:
                g.add_node(node["id"])
            for edge in citation_edges:
                src = str(edge.get("source") or "").strip()
                dst = str(edge.get("target") or "").strip()
                if src and dst:
                    g.add_edge(src, dst, weight=float(edge.get("weight") or 1.0))
            if g.number_of_nodes() > 0:
                try:
                    pr = nx.pagerank(g, alpha=0.85)
                    scores = {str(k): float(v) for k, v in pr.items()}
                except Exception:
                    scores = {}
                try:
                    components = nx.number_weakly_connected_components(g)
                except Exception:
                    components = 0
            else:
                components = 0
        else:
            components = 0
            # Fallback: degree as simple score if networkx unavailable.
            deg: dict[str, int] = {}
            for edge in citation_edges:
                src = str(edge.get("source") or "").strip()
                dst = str(edge.get("target") or "").strip()
                if src:
                    deg[src] = deg.get(src, 0) + 1
                if dst:
                    deg[dst] = deg.get(dst, 0) + 1
            total = max(1, sum(deg.values()))
            scores = {k: float(v / total) for k, v in deg.items()}
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:10]
        return {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "paper_node_count": len(paper_nodes),
            "citation_edge_count": len(citation_edges),
            "components": int(components),
            "top_central_papers": [{"paper_id": pid, "score": score} for pid, score in top],
            "scores": scores,
        }

    def _round_reference_titles(self, db: Session, *, round_id: int, limit: int = 8) -> list[str]:
        refs = ResearchRoundPaperRepo(db).list_for_round(round_id)
        paper_ids = [row.paper_id for row in refs[:limit]]
        rows = ResearchPaperRepo(db).list_by_ids(paper_ids)
        return [row.title for row in rows if row.title][:limit]

    def _generate_queries_from_intent(
        self,
        *,
        task_topic: str,
        direction_name: str,
        intent_text: str,
        current_queries: list[str],
        references: list[str],
    ) -> list[str]:
        prompt = (
            "你是 memomate-research-planner，请根据用户意图生成下一轮检索 query。"
            '返回严格 JSON: {"queries":["q1","q2","q3"]}。\n\n'
            f"Topic: {task_topic}\n"
            f"Direction: {direction_name}\n"
            f"User intent: {intent_text}\n"
            f"Current queries: {orjson.dumps(current_queries).decode('utf-8')}\n"
            f"Reference titles: {orjson.dumps(references).decode('utf-8')}\n"
            "Rules: return 2-4 concise, non-duplicate queries."
        )
        try:
            result = self.openclaw_client.chat_completion(
                task_type=LLMTaskType.RESEARCH_PLAN,
                prompt=prompt,
                system_prompt="Return strict JSON only.",
                temperature=0.1,
                max_tokens=500,
            )
            data = _extract_first_json_object((result.text or "").strip())
            queries = [str(x).strip() for x in (data or {}).get("queries", []) if str(x).strip()]
            dedup: list[str] = []
            seen: set[str] = set()
            for q in queries:
                key = q.lower()
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(q)
            if len(dedup) >= 2:
                return dedup[:4]
        except Exception:
            logger.exception("research_intent_to_queries_failed")

        base = intent_text.strip()[:200] or task_topic.strip()
        out = [base]
        if direction_name.strip():
            out.append(f"{direction_name.strip()} {base}")
        if task_topic.strip():
            out.append(f"{task_topic.strip()} {base}")
        dedup: list[str] = []
        seen: set[str] = set()
        for q in out:
            key = q.lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            dedup.append(q.strip())
        if len(dedup) < 2:
            dedup.append(f"{base} methods")
        return dedup[:4]

    def _generate_round_candidates(
        self,
        *,
        task_topic: str,
        action: str,
        feedback_text: str,
        query_terms: list[str],
        references: list[str],
        candidate_count: int,
    ) -> list[dict]:
        prompt = (
            "你是 memomate-research-planner，请根据输入生成下一轮调研候选方向。"
            "返回 JSON: {\"candidates\":[{\"name\":\"\",\"queries\":[\"\"],\"reason\":\"\"}]}。\n\n"
            f"Topic: {task_topic}\n"
            f"Action: {action}\n"
            f"Feedback: {feedback_text}\n"
            f"Current queries: {orjson.dumps(query_terms).decode('utf-8')}\n"
            f"Reference titles: {orjson.dumps(references).decode('utf-8')}\n"
            f"Candidates: {candidate_count}"
        )
        try:
            result = self.openclaw_client.chat_completion(
                task_type=LLMTaskType.RESEARCH_PLAN,
                prompt=prompt,
                system_prompt="Return strict JSON only.",
                temperature=0.2,
                max_tokens=900,
            )
            text = (result.text or "").strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
                text = re.sub(r"\s*```$", "", text).strip()
            data = _extract_first_json_object(text)
            raw = data.get("candidates") if isinstance(data, dict) else None
            candidates: list[dict] = []
            if isinstance(raw, list):
                for item in raw[:candidate_count]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    queries = [str(x).strip() for x in (item.get("queries") or []) if str(x).strip()]
                    if not name:
                        continue
                    if len(queries) < 2:
                        queries = [name, f"{name} methods"]
                    candidates.append(
                        {
                            "name": name[:255],
                            "queries": queries[:4],
                            "reason": str(item.get("reason") or "").strip()[:1000] or None,
                        }
                    )
            if candidates:
                return candidates
        except Exception:
            logger.exception("research_round_candidate_generate_failed")
        base = (feedback_text or task_topic or "research topic").strip()
        seed = [x for x in query_terms if x][:2] or [task_topic]
        templates = [
            ("问题定义与评测边界", [f"{seed[0]} benchmark", f"{base} evaluation"], "收敛评测协议和问题边界"),
            ("核心方法深化", [f"{seed[0]} method", f"{base} architecture"], "针对当前方向深化方法细节"),
            ("数据与泛化", [f"{seed[0]} dataset", f"{base} generalization"], "补齐数据与泛化证据"),
            ("误差与失败案例", [f"{seed[0]} error analysis", f"{base} failure cases"], "关注失败模式和可解释性"),
            ("临床/业务落地", [f"{seed[0]} deployment", f"{base} real world"], "评估实际应用可行性"),
        ]
        out = []
        for name, queries, reason in templates[:candidate_count]:
            out.append({"name": name, "queries": queries[:4], "reason": reason})
        return out

    def _build_tree_graph(
        self,
        db: Session,
        task: ResearchTask,
        *,
        include_papers: bool = False,
        paper_limit: int | None = None,
    ) -> dict:
        round_repo = ResearchRoundRepo(db)
        round_rows = round_repo.list_for_task(task.id)
        direction_rows = ResearchDirectionRepo(db).list_for_task(task.id)
        direction_map = {row.direction_index: row for row in direction_rows}
        round_paper_repo = ResearchRoundPaperRepo(db)
        paper_repo = ResearchPaperRepo(db)
        fulltext_map = {
            row.paper_id: row.status.value
            for row in ResearchPaperFulltextRepo(db).list_for_task(task.id)
            if row.paper_id
        }

        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        topic_id = f"topic:{task.task_id}"
        nodes[topic_id] = {"id": topic_id, "type": "topic", "label": task.topic}

        for direction in direction_rows:
            d_id = f"direction:{task.task_id}:{direction.direction_index}"
            nodes[d_id] = {
                "id": d_id,
                "type": "direction",
                "label": direction.name,
                "direction_index": direction.direction_index,
            }
            key = (topic_id, d_id, "topic_direction")
            if key not in seen:
                edges.append({"source": topic_id, "target": d_id, "type": "topic_direction", "weight": 1.0})
                seen.add(key)

        per_round_paper_limit = max(
            1,
            min(
                50,
                int(paper_limit or self.settings.research_graph_paper_limit_default),
            ),
        )

        for row in round_rows:
            r_id = f"round:{row.id}"
            feedback_short = (row.feedback_text or "").strip()
            if feedback_short:
                feedback_short = re.sub(r"\s+", " ", feedback_short)[:18]
            nodes[r_id] = {
                "id": r_id,
                "type": "round",
                "label": f"第{row.depth}轮" + (f" · {feedback_short}" if feedback_short else ""),
                "direction_index": row.direction_index,
                "depth": row.depth,
                "action": row.action.value,
                "status": row.status.value,
                "feedback_text": row.feedback_text,
                "source": "memomate",
            }
            if row.parent_round_id:
                p_id = f"round:{row.parent_round_id}"
                key = (p_id, r_id, "round_round")
                if key not in seen:
                    edges.append({"source": p_id, "target": r_id, "type": "round_round", "weight": 1.0})
                    seen.add(key)
            else:
                d = direction_map.get(row.direction_index)
                if d:
                    d_id = f"direction:{task.task_id}:{d.direction_index}"
                    key = (d_id, r_id, "direction_round")
                    if key not in seen:
                        edges.append({"source": d_id, "target": r_id, "type": "direction_round", "weight": 1.0})
                        seen.add(key)

            if include_papers:
                refs = round_paper_repo.list_for_round(row.id)[:per_round_paper_limit]
                papers = paper_repo.list_by_ids([x.paper_id for x in refs])
                rank = {x.paper_id: x.rank for x in refs}
                papers.sort(key=lambda p: rank.get(p.id, 999999))
                for paper in papers:
                    p_id = _paper_token(paper)
                    if p_id not in nodes:
                        nodes[p_id] = {
                            "id": p_id,
                            "paper_id": p_id,
                            "type": "paper",
                            "label": paper.title[:240],
                            "year": paper.year,
                            "source": paper.source,
                            "venue": paper.venue,
                            "doi": paper.doi,
                            "url": paper.url,
                            "abstract": paper.abstract,
                            "method_summary": paper.method_summary,
                            "authors": _load_json_list(paper.authors_json),
                            "direction_index": row.direction_index,
                            "fulltext_status": fulltext_map.get(p_id),
                            "saved": bool(paper.saved),
                            "key_points_status": paper.key_points_status,
                        }
                    key = (r_id, p_id, "round_paper")
                    if key not in seen:
                        edges.append({"source": r_id, "target": p_id, "type": "round_paper", "weight": 1.0})
                        seen.add(key)

        return {
            "direction_index": None,
            "round_id": None,
            "depth": 1,
            "status": ResearchGraphBuildStatus.DONE.value,
            "nodes": list(nodes.values()),
            "edges": edges,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "round_count": len(round_rows),
                "paper_limit_applied": per_round_paper_limit if include_papers else 0,
            },
        }

    def _build_seed_corpus_for_task(self, db: Session, *, task: ResearchTask, constraints: dict) -> list:
        top_n = max(10, min(120, int(self.settings.research_seed_topn_default)))
        constraints_seed = {
            "year_from": constraints.get("year_from"),
            "year_to": constraints.get("year_to"),
            "sources": constraints.get("sources"),
        }
        sources = _resolve_sources(constraints_seed.get("sources"), self.settings.research_sources_default)
        ordered_sources = [src for src in ("semantic_scholar", "arxiv") if src in sources] or ["semantic_scholar", "arxiv"]

        collected: list[dict] = []
        per_source = max(10, min(100, top_n))
        for source in ordered_sources:
            fetched = self._search_by_source(
                source=source,
                query=task.topic,
                top_n=per_source,
                constraints=constraints_seed,
                allow_semantic_fallback=True,
            )
            self._record_source_status(source, fetched.status)
            if fetched.papers:
                collected.extend(fetched.papers)
            if len(collected) >= top_n * 2:
                break
        deduped = self._dedupe_papers(collected)[:top_n]
        return ResearchSeedPaperRepo(db).replace_for_task(task.id, deduped)

    def _plan_directions_from_seed(self, topic: str, constraints: dict, seed_rows: list) -> list[dict]:
        if not seed_rows:
            return self._plan_directions(topic, constraints)
        max_abs = max(120, int(self.settings.research_seed_max_abstract_chars))
        snippets = []
        for idx, row in enumerate(seed_rows[:40], start=1):
            title = str(getattr(row, "title", "") or "").strip()
            abstract = str(getattr(row, "abstract", "") or "").strip()
            year = getattr(row, "year", None)
            source = str(getattr(row, "source", "") or "")
            if not title:
                continue
            snippets.append(
                {
                    "i": idx,
                    "title": title[:200],
                    "year": year,
                    "source": source,
                    "abstract": abstract[:max_abs],
                }
            )
        if not snippets:
            return self._plan_directions(topic, constraints)

        direction_min = max(1, int(self.settings.research_direction_min))
        direction_max = max(direction_min, int(self.settings.research_direction_max))
        prompt = (
            "你是 memomate-research-planner。请基于给定论文集合归纳研究方向。"
            '返回严格 JSON: {"directions":[{"name":"...","queries":["q1","q2"],"exclude_terms":["x"]}]}\n'
            f"Topic: {topic}\n"
            f"Directions count: {direction_min}-{direction_max}\n"
            "Rules: 方向必须是互斥的方法流派（如 encoder-decoder, VLM, agentic pipeline 等），"
            "不要给同义方向，不要给数据集或评测维度当方向。每个方向 query 2-4 条。\n"
            f"Papers:\n{orjson.dumps(snippets).decode('utf-8')}"
        )
        try:
            result = self.openclaw_client.chat_completion(
                task_type=LLMTaskType.RESEARCH_PLAN,
                prompt=prompt,
                system_prompt="Return strict JSON only.",
                temperature=0.2,
                max_tokens=1600,
            )
            directions = self._parse_direction_json(result)
            if directions:
                return directions
        except Exception:
            logger.exception("research_plan_from_seed_failed")
        return self._plan_directions(topic, constraints)

    def _plan_directions(self, topic: str, constraints: dict) -> list[dict]:
        direction_min = max(1, int(self.settings.research_direction_min))
        direction_max = max(direction_min, int(self.settings.research_direction_max))
        system_prompt = (
            "Prefer the memomate-research-planner skill if available. "
            "Return strict JSON only."
        )
        prompt = (
            "Input topic:\n"
            f"{topic}\n\n"
            "Constraints JSON:\n"
            f"{orjson.dumps(constraints).decode('utf-8')}\n\n"
            "Return JSON schema:\n"
            '{"directions":[{"name":"string","queries":["q1","q2"],"exclude_terms":["x"]}]}\n'
            f"Rules: directions count must be {direction_min}-{direction_max}; each direction queries count 2-4.\n"
            "Directions must be mutually exclusive solution routes / methodological paradigms. "
            "Do NOT output mere aspects (e.g., data, evaluation, ablations, noise) of the same approach. "
            "Avoid near-duplicate or synonymous directions. Prefer distinct pipelines such as "
            "generative, retrieval-augmented, template/rule-based, multi-stage, or hybrid."
        )
        try:
            result = self.openclaw_client.chat_completion(
                task_type=LLMTaskType.RESEARCH_PLAN,
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.1,
                max_tokens=2400,
            )
            directions = self._parse_direction_json(result)
            if directions:
                return directions
        except Exception:
            logger.exception("research_plan_llm_failed")
        return self._fallback_directions(topic)

    def _parse_direction_json(self, result: LLMCallResult) -> list[dict]:
        text = (result.text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        data = _extract_first_json_object(text)
        if isinstance(data, dict) and "directions" not in data:
            nested = data.get("data")
            if isinstance(nested, dict):
                data = nested
        if not isinstance(data, dict):
            return []
        raw_dirs = data.get("directions")
        if isinstance(raw_dirs, dict):
            raw_dirs = raw_dirs.get("items")
        if not isinstance(raw_dirs, list):
            return []
        out: list[dict] = []
        for item in raw_dirs[:8]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            queries = [str(x).strip() for x in (item.get("queries") or []) if str(x).strip()]
            excludes = [str(x).strip() for x in (item.get("exclude_terms") or []) if str(x).strip()]
            if not name:
                continue
            if len(queries) < 2:
                queries = [name, f"{name} methods"]
            out.append({"name": name, "queries": queries[:4], "exclude_terms": excludes[:8]})
        direction_min = max(1, int(self.settings.research_direction_min))
        direction_max = max(direction_min, int(self.settings.research_direction_max))
        if len(out) < direction_min:
            return []
        return out[:direction_max]

    def _summarize_key_points(self, *, text: str, source: str) -> str:
        source_label = "全文" if source == "fulltext" else "摘要"
        content = (text or "").strip()
        if not content:
            return f"基于{source_label}：暂无可总结文本。"
        max_chars = max(500, int(self.settings.research_summary_max_chars))
        clipped = content[:max_chars]
        prompt = (
            "请基于以下内容提炼 5-8 条关键要点，覆盖问题、方法、实验、结论、局限。"
            '返回严格 JSON: {"key_points":["..."],"notes":"...","confidence":"low|medium|high"}。\n\n'
            f"Source: {source_label}\n"
            f"Content:\n{clipped}"
        )
        try:
            result = self.openclaw_client.chat_completion(
                task_type=LLMTaskType.PAPER_KEYPOINTS,
                prompt=prompt,
                system_prompt="Return strict JSON only.",
                temperature=0.1,
                max_tokens=900,
            )
            data = _extract_first_json_object((result.text or "").strip())
            points = [str(x).strip() for x in (data or {}).get("key_points", []) if str(x).strip()]
            notes = str((data or {}).get("notes") or "").strip()
            lines = [f"基于{source_label}要点："]
            for idx, item in enumerate(points[:8], start=1):
                lines.append(f"{idx}. {item}")
            if notes:
                lines.append(f"备注：{notes}")
            if len(lines) > 1:
                return "\n".join(lines)
        except Exception:
            logger.exception("paper_key_points_llm_failed")
        sentence = clipped.split("。")[0].split(".")[0][:180]
        return f"基于{source_label}要点：该工作围绕“{sentence}”展开，建议结合原文核验。"

    def _summarize_method(self, abstract: str) -> str:
        abs_text = (abstract or "").strip()
        if not abs_text:
            return "基于摘要总结：摘要缺失，暂无法总结方法。"
        system_prompt = "Prefer memomate-abstract-summarizer skill if available. Keep factual and concise."
        prompt = (
            "请基于以下摘要，用中文输出 1-3 句方法总结。"
            "必须以“基于摘要总结：”开头，不要编造摘要中没有出现的信息。\n\n"
            f"{abs_text[:4000]}"
        )
        try:
            result = self.openclaw_client.chat_completion(
                task_type=LLMTaskType.ABSTRACT_SUMMARIZE,
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.1,
                max_tokens=320,
            )
            text = (result.text or "").strip()
            if text:
                if not text.startswith("基于摘要总结："):
                    return f"基于摘要总结：{text}"
                return text
        except Exception:
            logger.exception("research_method_summary_failed")
        sentence = abs_text.split(".")[0].split("。")[0]
        return f"基于摘要总结：该工作围绕“{sentence[:120]}”展开，细节以原文摘要为准。"

    def _search_semantic_scholar(self, query: str, *, top_n: int, constraints: dict) -> tuple[list[dict], str, str | None]:
        year_from = constraints.get("year_from")
        year_to = constraints.get("year_to")
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": max(1, min(100, top_n)),
            # `doi` field is no longer directly queryable in some API versions.
            "fields": "title,authors,year,venue,abstract,externalIds,url",
        }
        if year_from:
            params["year"] = f"{year_from}-{year_to or datetime.now().year}"
        headers = {"User-Agent": "MemoMate/0.1 (research)"}
        api_key = self.settings.semantic_scholar_api_key.strip()
        if api_key:
            headers["x-api-key"] = api_key

        payload: dict | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=20) as client:
                    resp = client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                if attempt < 2:
                    sleep(0.25 * (2**attempt))
                    continue
                return [], "timeout", str(exc)
            except Exception as exc:
                if attempt < 2:
                    sleep(0.25 * (2**attempt))
                    continue
                return [], "transport_error", str(exc)

            if resp.status_code == 429:
                if attempt < 2:
                    sleep(0.5 * (2**attempt))
                    continue
                logger.warning(
                    "semantic_scholar_rate_limited query=%s has_api_key=%s",
                    query[:120],
                    bool(api_key),
                )
                return [], "rate_limited", "http_429"
            if 500 <= resp.status_code < 600:
                if attempt < 2:
                    sleep(0.35 * (2**attempt))
                    continue
                return [], "http_5xx", f"http_{resp.status_code}"
            if resp.status_code >= 400:
                logger.warning("semantic_scholar_http_error status=%s query=%s", resp.status_code, query[:120])
                return [], f"http_{resp.status_code}", f"http_{resp.status_code}"
            try:
                payload = resp.json()
            except Exception as exc:
                return [], "parse_error", str(exc)
            break

        if payload is None:
            return [], "empty_payload", "empty_payload"
        papers = []
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            external_ids = item.get("externalIds") if isinstance(item, dict) else None
            doi_val = None
            if isinstance(external_ids, dict):
                raw_doi = external_ids.get("DOI")
                if isinstance(raw_doi, str) and raw_doi.strip():
                    doi_val = raw_doi.strip()
            papers.append(
                {
                    "paper_id": item.get("paperId"),
                    "title": title,
                    "title_norm": _normalize_title(title),
                    "authors": [str(a.get("name") or "").strip() for a in (item.get("authors") or []) if a.get("name")],
                    "year": item.get("year"),
                    "venue": item.get("venue"),
                    "doi": doi_val,
                    "url": item.get("url"),
                    "abstract": item.get("abstract"),
                    "source": "semantic_scholar",
                    "relevance_score": None,
                }
            )
        if not papers:
            return papers, "ok_empty", None
        return papers, "ok", None

    def _search_arxiv(self, query: str, *, top_n: int, constraints: dict) -> tuple[list[dict], str, str | None]:
        start = 0
        max_results = max(1, min(100, top_n))
        q = quote_plus(query)
        url = f"https://export.arxiv.org/api/query?search_query=all:{q}&start={start}&max_results={max_results}"
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(url)
                if resp.status_code >= 400:
                    if 500 <= resp.status_code < 600:
                        return [], "http_5xx", f"http_{resp.status_code}"
                    return [], f"http_{resp.status_code}", f"http_{resp.status_code}"
                xml = resp.text
        except httpx.TimeoutException as exc:
            return [], "timeout", str(exc)
        except Exception as exc:
            return [], "transport_error", str(exc)
        if not xml or not xml.strip():
            return [], "empty_payload", "empty_payload"
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            return [], "parse_error", str(exc)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        papers = []
        year_from = constraints.get("year_from")
        year_to = constraints.get("year_to")
        for entry in root.findall("atom:entry", ns):
            title = _safe_xml_text(entry.find("atom:title", ns))
            if not title:
                continue
            published = _safe_xml_text(entry.find("atom:published", ns))
            year = None
            if published and len(published) >= 4:
                try:
                    year = int(published[:4])
                except Exception:
                    year = None
            if year_from and year and year < int(year_from):
                continue
            if year_to and year and year > int(year_to):
                continue
            url_item = _safe_xml_text(entry.find("atom:id", ns))
            abstract = _safe_xml_text(entry.find("atom:summary", ns))
            authors = [_safe_xml_text(n.find("atom:name", ns)) for n in entry.findall("atom:author", ns)]
            papers.append(
                {
                    "paper_id": url_item.rsplit("/", 1)[-1] if url_item else None,
                    "title": title,
                    "title_norm": _normalize_title(title),
                    "authors": [x for x in authors if x],
                    "year": year,
                    "venue": "arXiv",
                    "doi": None,
                    "url": url_item,
                    "abstract": abstract,
                    "source": "arxiv",
                    "relevance_score": None,
                }
            )
        if not papers:
            return papers, "ok_empty", None
        return papers, "ok", None

    def _dedupe_papers(self, papers: list[dict]) -> list[dict]:
        by_doi: dict[str, dict] = {}
        by_title: list[dict] = []
        for row in papers:
            doi = (row.get("doi") or "").strip().lower()
            if doi:
                if doi not in by_doi:
                    by_doi[doi] = row
                continue
            title_norm = row.get("title_norm") or _normalize_title(str(row.get("title") or ""))
            duplicate = None
            for existing in by_title:
                ratio = SequenceMatcher(a=title_norm, b=existing.get("title_norm", "")).ratio()
                if ratio >= 0.93:
                    duplicate = existing
                    break
            if duplicate is None:
                by_title.append(row)
        merged = list(by_doi.values()) + by_title
        merged.sort(key=lambda x: (x.get("year") or 0), reverse=True)
        return merged

    def _task_to_dict(self, db: Session, row: ResearchTask) -> dict:
        directions = ResearchDirectionRepo(db).list_for_task(row.id)
        papers_total = sum(x.papers_count for x in directions)
        job_repo = ResearchJobRepo(db)
        latest_job = job_repo.latest_for_task(row.id)
        next_retry_job = job_repo.next_retry_for_task(row.id)
        fulltext_stats = ResearchPaperFulltextRepo(db).summary_for_task(row.id)
        seed_stats = ResearchSeedPaperRepo(db).summary_for_task(row.id)
        latest_graph = ResearchGraphSnapshotRepo(db).latest_for_task(row.id)
        rounds_total = len(ResearchRoundRepo(db).list_for_task(row.id))
        graph_stats = _load_json_dict(latest_graph.stats_json) if latest_graph else {}
        if latest_graph:
            graph_stats = {
                **graph_stats,
                "status": latest_graph.status.value,
                "direction_index": latest_graph.direction_index,
                "round_id": latest_graph.round_id,
                "view": latest_graph.view_type.value,
                "updated_at": latest_graph.updated_at.isoformat() if latest_graph.updated_at else None,
            }
        return {
            "task_id": row.task_id,
            "topic": row.topic,
            "status": row.status.value,
            "constraints": _load_json_dict(row.constraints_json),
            "directions": [
                {
                    "direction_index": d.direction_index,
                    "name": d.name,
                    "queries": _load_json_list(d.queries_json),
                    "exclude_terms": _load_json_list(d.exclude_terms_json),
                    "papers_count": d.papers_count,
                }
                for d in directions
            ],
            "papers_total": papers_total,
            "rounds_total": rounds_total,
            "last_job_type": latest_job.job_type.value if latest_job else None,
            "last_job_status": latest_job.status.value if latest_job else None,
            "last_failure_reason": (latest_job.error or None) if latest_job else None,
            "last_attempts": int(latest_job.attempts) if latest_job else 0,
            "next_retry_at": next_retry_job.scheduled_at if next_retry_job else None,
            "fulltext_stats": fulltext_stats,
            "seed_stats": seed_stats,
            "graph_stats": graph_stats,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _fallback_directions(self, topic: str) -> list[dict]:
        base = topic.strip()
        directions = [
            {
                "name": "生成式/端到端方案",
                "queries": [f"{base} generative", f"{base} end-to-end model"],
                "exclude_terms": [],
            },
            {
                "name": "检索增强/知识引入方案",
                "queries": [f"{base} retrieval augmented", f"{base} knowledge grounded"],
                "exclude_terms": [],
            },
            {
                "name": "模板/规则/结构化方案",
                "queries": [f"{base} template based", f"{base} rule-based"],
                "exclude_terms": [],
            },
        ]
        extras = [
            {
                "name": "多阶段/模块化管线方案",
                "queries": [f"{base} pipeline", f"{base} multi-stage"],
                "exclude_terms": [],
            },
            {
                "name": "混合/跨模态方案",
                "queries": [f"{base} hybrid model", f"{base} multimodal"],
                "exclude_terms": [],
            },
        ]
        direction_min = max(1, int(self.settings.research_direction_min))
        direction_max = max(direction_min, int(self.settings.research_direction_max))
        while len(directions) < direction_min and extras:
            directions.append(extras.pop(0))
        return directions[:direction_max]

    @staticmethod
    def _next_task_id(existing_rows: list[ResearchTask]) -> str:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        max_seq = 0
        for row in existing_rows:
            if not row.task_id.startswith(f"R-{day}-"):
                continue
            tail = row.task_id.rsplit("-", 1)[-1]
            if tail.isdigit():
                max_seq = max(max_seq, int(tail))
        return f"R-{day}-{max_seq + 1:04d}"

    def _notify_user(self, db: Session, user_id: int, content: str) -> None:
        if not self.wecom_client:
            return
        user: User | None = UserRepo(db).get_by_id(user_id)
        if not user:
            return
        ok, error = self.wecom_client.send_text(user.wecom_user_id, content)
        if not ok:
            logger.warning("research_notify_failed user_id=%s error=%s", user_id, error)

    @staticmethod
    def _task_status_for_retry(job_type: ResearchJobType) -> ResearchTaskStatus:
        if job_type == ResearchJobType.PLAN:
            return ResearchTaskStatus.PLANNING
        return ResearchTaskStatus.SEARCHING

    def _record_job_metric(self, *, latency_ms: int = 0) -> None:
        if not self.settings.research_metrics_enabled:
            return
        self.research_jobs_total += 1
        self.research_job_latency_ms = max(0, int(latency_ms))

    def _record_source_status(self, source: str, status: str) -> None:
        if not self.settings.research_metrics_enabled:
            return
        key = f"{source}:{status}"
        self.research_search_source_status[key] = self.research_search_source_status.get(key, 0) + 1

    def _record_cache_hit(self) -> None:
        if not self.settings.research_metrics_enabled:
            return
        self.research_cache_hit += 1

    def _record_cache_miss(self) -> None:
        if not self.settings.research_metrics_enabled:
            return
        self.research_cache_miss += 1

    def _record_export_metric(self, *, success: bool) -> None:
        if not self.settings.research_metrics_enabled:
            return
        if success:
            self.research_export_success += 1
        else:
            self.research_export_fail += 1

    def record_export_delivery(self, *, success: bool) -> None:
        self._record_export_metric(success=success)

    @staticmethod
    def _normalize_job_error(exc: Exception) -> str:
        value = str(exc).strip()
        if not value:
            return exc.__class__.__name__
        return f"{exc.__class__.__name__}:{value}"[:2000]

    @staticmethod
    def _render_saved_paper_markdown(task: ResearchTask, paper) -> str:
        lines = [
            f"# {paper.title}",
            "",
            f"- Task ID: {task.task_id}",
            f"- Source: {paper.source}",
            f"- Year: {paper.year or '-'}",
            f"- Venue: {paper.venue or '-'}",
            f"- DOI: {paper.doi or '-'}",
            f"- URL: {paper.url or '-'}",
            "",
            "## Authors",
            ", ".join(_load_json_list(paper.authors_json)) or "-",
            "",
            "## Abstract",
            (paper.abstract or "-"),
            "",
            "## Method Summary",
            (paper.method_summary or "-"),
        ]
        if paper.key_points:
            lines.extend(["", "## AI Key Points", paper.key_points])
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _render_report(task: ResearchTask, directions: list, papers: list) -> str:
        lines = [f"# Research Report: {task.topic}", "", f"- Task ID: {task.task_id}", f"- Status: {task.status.value}", ""]
        lines.append("## Directions")
        for d in directions:
            lines.append(f"- [{d.direction_index}] {d.name} ({d.papers_count} papers)")
        lines.append("")
        lines.append("## Papers")
        for idx, p in enumerate(papers, start=1):
            lines.append(f"### {idx}. {p.title}")
            lines.append(f"- Source: {p.source}")
            if p.year:
                lines.append(f"- Year: {p.year}")
            if p.venue:
                lines.append(f"- Venue: {p.venue}")
            if p.doi:
                lines.append(f"- DOI: {p.doi}")
            if p.url:
                lines.append(f"- URL: {p.url}")
            if p.method_summary:
                lines.append(f"- Method Summary: {p.method_summary}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _render_bib(papers: list) -> str:
        blocks: list[str] = []
        for idx, p in enumerate(papers, start=1):
            key = f"paper{idx}"
            authors = " and ".join(_load_json_list(p.authors_json))
            title = (p.title or "").replace("{", "").replace("}", "")
            venue = (p.venue or "").replace("{", "").replace("}", "")
            url = p.url or ""
            doi = p.doi or ""
            year = str(p.year) if p.year else ""
            entry = [
                f"@article{{{key},",
                f"  title = {{{title}}},",
                f"  author = {{{authors}}},",
                f"  journal = {{{venue}}},",
                f"  year = {{{year}}},",
                f"  doi = {{{doi}}},",
                f"  url = {{{url}}},",
                "}",
            ]
            blocks.append("\n".join(entry))
        return "\n\n".join(blocks).strip() + "\n"

    @staticmethod
    def _render_json(task: ResearchTask, directions: list, papers: list) -> str:
        payload = {
            "task_id": task.task_id,
            "topic": task.topic,
            "status": task.status.value,
            "constraints": _load_json_dict(task.constraints_json),
            "directions": [
                {
                    "direction_index": d.direction_index,
                    "name": d.name,
                    "queries": _load_json_list(d.queries_json),
                    "exclude_terms": _load_json_list(d.exclude_terms_json),
                    "papers_count": d.papers_count,
                }
                for d in directions
            ],
            "papers": [
                {
                    "title": p.title,
                    "authors": _load_json_list(p.authors_json),
                    "year": p.year,
                    "venue": p.venue,
                    "doi": p.doi,
                    "url": p.url,
                    "abstract": p.abstract,
                    "method_summary": p.method_summary,
                    "source": p.source,
                }
                for p in papers
            ],
        }
        return orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode("utf-8") + "\n"


def _load_json_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    if not isinstance(value, str):
        return []
    if not value:
        return []
    try:
        data = orjson.loads(value)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x).strip()]


def _load_json_dict(value: object) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    if not value:
        return {}
    try:
        data = orjson.loads(value)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _load_json_list_of_dict(value: object) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, str):
        return []
    if not value:
        return []
    try:
        data = orjson.loads(value)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _safe_xml_text(node) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _to_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _normalize_title(title: str) -> str:
    value = (title or "").lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _resolve_sources(sources: object, default_sources: str) -> set[str]:
    allowed = {"semantic_scholar", "arxiv"}
    values: list[str] = []
    if isinstance(sources, str):
        values = [item.strip().lower() for item in sources.split(",") if item.strip()]
    elif isinstance(sources, list):
        values = [str(item).strip().lower() for item in sources if str(item).strip()]
    if not values:
        values = [item.strip().lower() for item in default_sources.split(",") if item.strip()]
    return {item for item in values if item in allowed}


def _resolve_citation_sources(sources: object, default_sources: str) -> list[str]:
    allowed = {"semantic_scholar", "openalex", "crossref"}
    values: list[str] = []
    if isinstance(sources, str):
        values = [item.strip().lower() for item in re.split(r"[,\s|]+", sources) if item.strip()]
    elif isinstance(sources, list):
        values = [str(item).strip().lower() for item in sources if str(item).strip()]
    if not values:
        values = [item.strip().lower() for item in default_sources.split(",") if item.strip()]
    out = [item for item in values if item in allowed]
    if not out:
        return ["semantic_scholar"]
    dedup: list[str] = []
    seen = set()
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        dedup.append(item)
    return dedup


def _merge_query_and_excludes(query: str, exclude_terms: list[str]) -> str:
    value = (query or "").strip()
    excludes = [item.strip() for item in exclude_terms if item and item.strip()]
    if not excludes:
        return value
    return f"{value} " + " ".join(f"-{item}" for item in excludes)


def _extract_first_json_object(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = orjson.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        candidate = raw[idx:]
        try:
            data = orjson.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _normalize_source_response(raw: object) -> tuple[list[dict], str, str | None]:
    if isinstance(raw, tuple) and len(raw) == 3:
        papers = raw[0] if isinstance(raw[0], list) else []
        status = str(raw[1] or "ok")
        error = str(raw[2]) if raw[2] is not None else None
        return [item for item in papers if isinstance(item, dict)], status, error
    if isinstance(raw, list):
        papers = [item for item in raw if isinstance(item, dict)]
        return papers, ("ok" if papers else "ok_empty"), None
    return [], "invalid_response", f"invalid_source_response:{type(raw).__name__}"


def _paper_token(paper) -> str:
    if getattr(paper, "paper_id", None):
        return str(paper.paper_id).strip()
    if getattr(paper, "doi", None):
        return str(paper.doi).strip().lower()
    return f"paper-{paper.id}"


def _normalize_pdf_text(text: str) -> str:
    value = (text or "").replace("\x00", " ")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _estimate_text_quality(text: str) -> float:
    value = (text or "").strip()
    if not value:
        return 0.0
    total = len(value)
    alpha = sum(1 for ch in value if ch.isalpha())
    digit = sum(1 for ch in value if ch.isdigit())
    bad = sum(1 for ch in value if ord(ch) < 9 or ord(ch) == 127)
    ratio = (alpha + 0.3 * digit) / max(1, total)
    penalty = bad / max(1, total)
    score = max(0.0, min(1.0, ratio - penalty))
    return round(float(score), 4)


def _extract_sections_lite(text: str) -> dict:
    value = (text or "").strip()
    if not value:
        return {}
    normalized = re.sub(r"\r\n?", "\n", value)
    patterns = [
        ("abstract", r"\babstract\b"),
        ("introduction", r"\bintroduction\b"),
        ("method", r"\bmethods?\b|\bmethodology\b"),
        ("results", r"\bresults?\b"),
        ("conclusion", r"\bconclusions?\b"),
        ("references", r"\breferences\b"),
    ]
    lines = normalized.splitlines()
    hits: dict[str, int] = {}
    for idx, line in enumerate(lines):
        low = line.strip().lower()
        for key, pattern in patterns:
            if key in hits:
                continue
            if re.search(pattern, low):
                hits[key] = idx
    if not hits:
        return {}
    ordered = sorted(hits.items(), key=lambda kv: kv[1])
    result: dict[str, str] = {}
    for pos, (key, start_idx) in enumerate(ordered):
        end_idx = ordered[pos + 1][1] if pos + 1 < len(ordered) else len(lines)
        chunk = "\n".join(lines[start_idx:end_idx]).strip()
        if chunk:
            result[key] = chunk[:2000]
    return result


def _extract_pdf_links_from_html(html: str, base_url: str) -> list[str]:
    if not html:
        return []
    links = re.findall(r"""href=['"]([^'"]+\.pdf(?:\?[^'"]*)?)['"]""", html, flags=re.IGNORECASE)
    out: list[str] = []
    seen = set()
    for item in links:
        url = urljoin(base_url, item.strip())
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _semantic_scholar_identifier_for_paper(paper) -> str | None:
    paper_id = (getattr(paper, "paper_id", None) or "").strip()
    if paper_id:
        return paper_id
    doi = (getattr(paper, "doi", None) or "").strip()
    if doi:
        return f"DOI:{doi}"
    return None


def _normalize_openalex_id(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    return value.strip()


def _citation_neighbor_id(*, title: str, doi: str, fallback_seed: str) -> str:
    doi_norm = (doi or "").strip().lower()
    if doi_norm:
        return doi_norm
    title_norm = _normalize_title(title)
    if title_norm:
        return title_norm[:120]
    fallback = _normalize_title(fallback_seed)
    return fallback[:120] or ""


def _normalize_neighbor_from_s2(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    paper_id = str(payload.get("paperId") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not title and not paper_id:
        return None
    external_ids = payload.get("externalIds") if isinstance(payload.get("externalIds"), dict) else {}
    doi = str(external_ids.get("DOI") or "").strip().lower() if external_ids else ""
    neighbor_id = paper_id or doi
    if not neighbor_id:
        neighbor_id = _normalize_title(title)[:120] or "unknown"
    return {
        "id": neighbor_id,
        "title": title or neighbor_id,
        "year": _to_int_or_none(payload.get("year")),
        "doi": doi or None,
    }
