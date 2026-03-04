from __future__ import annotations

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
    ResearchGraphBuildStatus,
    ResearchJobType,
    ResearchPaperFulltextStatus,
    ResearchTaskStatus,
)
from app.domain.models import ResearchTask, User
from app.infra.repos import (
    ResearchCitationEdgeRepo,
    ResearchDirectionRepo,
    ResearchGraphSnapshotRepo,
    ResearchJobRepo,
    ResearchPaperRepo,
    ResearchPaperFulltextRepo,
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
        task_id = self._next_task_id(task_repo.list_recent(user_id, limit=100))
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
        ResearchJobRepo(db).enqueue(task.id, ResearchJobType.SEARCH, payload)
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
        job_repo.enqueue(task.id, ResearchJobType.PLAN, payload)
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
    ) -> tuple[ResearchTask, bool]:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        if not self.settings.research_fulltext_enabled:
            raise ValueError("research fulltext is disabled")
        job_repo = ResearchJobRepo(db)
        if not force and job_repo.has_pending(task.id, ResearchJobType.FULLTEXT):
            return task, False
        job_repo.enqueue(task.id, ResearchJobType.FULLTEXT, {"force": bool(force)})
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
        force: bool = False,
    ) -> tuple[ResearchTask, bool]:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        if not self.settings.research_graph_enabled:
            raise ValueError("research graph is disabled")
        job_repo = ResearchJobRepo(db)
        if not force and job_repo.has_pending(task.id, ResearchJobType.GRAPH_BUILD):
            return task, False
        payload = {"direction_index": direction_index, "force": bool(force)}
        job_repo.enqueue(task.id, ResearchJobType.GRAPH_BUILD, payload)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return task, True

    def process_one_job(self, db: Session) -> int:
        job_repo = ResearchJobRepo(db)
        job = job_repo.next_queued()
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

        job_repo.mark_running(job)
        job_error: str | None = None
        try:
            if job.job_type == ResearchJobType.PLAN:
                self._run_plan_job(db, task, payload)
            elif job.job_type == ResearchJobType.SEARCH:
                self._run_search_job(db, task, payload)
            elif job.job_type == ResearchJobType.FULLTEXT:
                self._run_fulltext_job(db, task, payload)
            elif job.job_type == ResearchJobType.GRAPH_BUILD:
                self._run_graph_job(db, task, payload)
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
        text, _meta = self._parse_pdf_bytes(content)
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

    def get_graph_snapshot(
        self,
        db: Session,
        *,
        user_id: int,
        task_id: str,
        direction_index: int | None = None,
    ) -> dict:
        task = self.switch_task(db, user_id=user_id, task_id=task_id)
        row = ResearchGraphSnapshotRepo(db).latest_for_task(task.id, direction_index=direction_index)
        if not row:
            return {
                "task_id": task.task_id,
                "direction_index": direction_index,
                "depth": int(self.settings.research_graph_depth_default),
                "status": ResearchGraphBuildStatus.QUEUED.value,
                "nodes": [],
                "edges": [],
                "stats": {},
            }
        return {
            "task_id": task.task_id,
            "direction_index": row.direction_index,
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

    def _run_plan_job(self, db: Session, task: ResearchTask, payload: dict) -> None:
        constraints = _load_json_dict(task.constraints_json)
        directions = self._plan_directions(task.topic, constraints)
        ResearchDirectionRepo(db).replace_for_task(task, directions)
        task.status = ResearchTaskStatus.CREATED
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        lines = [f"调研任务 {task.task_id} 方向已生成："]
        for idx, item in enumerate(directions, start=1):
            lines.append(f"{idx}. {item['name']}")
        lines.append('回复“调研 选择 2”查看方向 2。')
        self._notify_user(db, task.user_id, "\n".join(lines))

    def _run_search_job(self, db: Session, task: ResearchTask, payload: dict) -> None:
        constraints = _load_json_dict(task.constraints_json)
        direction_index = int(payload.get("direction_index") or 1)
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
        papers = self._dedupe_papers(all_papers)
        papers = papers[: max(1, top_n)]
        for row in papers:
            row["method_summary"] = self._summarize_method(row.get("abstract") or "")
        rows = ResearchPaperRepo(db).replace_direction_papers(direction, papers)
        ResearchDirectionRepo(db).update_papers_count(direction, len(rows))

        task.status = ResearchTaskStatus.DONE
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        self._notify_user(
            db,
            task.user_id,
            f"已完成方向 {direction_index} 检索，收录 {len(rows)} 篇。回复“调研 下一页”浏览结果，回复“调研 导出”导出文件。",
        )

    def _run_fulltext_job(self, db: Session, task: ResearchTask, payload: dict) -> None:
        force = bool(payload.get("force") or False)
        papers = ResearchPaperRepo(db).list_for_task(task.id)
        fulltext_repo = ResearchPaperFulltextRepo(db)
        base_dir = Path(self.settings.research_artifact_dir).expanduser().resolve() / task.task_id / "fulltext"
        base_dir.mkdir(parents=True, exist_ok=True)
        for paper in papers:
            paper_id = _paper_token(paper)
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
                text, _meta = self._parse_pdf_bytes(pdf_bytes)
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

    def _run_graph_job(self, db: Session, task: ResearchTask, payload: dict) -> None:
        direction_index = _to_int_or_none(payload.get("direction_index"))
        seed_top_n = max(1, int(self.settings.research_graph_seed_topn))
        depth = max(1, int(self.settings.research_graph_depth_default))
        paper_repo = ResearchPaperRepo(db)
        fulltext_map = {
            row.paper_id: row.status.value
            for row in ResearchPaperFulltextRepo(db).list_for_task(task.id)
        }
        direction_repo = ResearchDirectionRepo(db)
        all_directions = direction_repo.list_for_task(task.id)
        direction_by_id = {d.id: d for d in all_directions}

        if direction_index is not None:
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

            for item in self._fetch_citation_neighbors(paper, limit=max(1, int(self.settings.research_graph_expand_limit_per_paper))):
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

        stats = self._compute_graph_stats(nodes=list(nodes.values()), edges=edges)
        for node in nodes.values():
            if node["id"] in stats.get("scores", {}):
                node["score"] = float(stats["scores"][node["id"]])

        ResearchCitationEdgeRepo(db).replace_for_task(task.id, citation_edges)
        ResearchGraphSnapshotRepo(db).upsert_snapshot(
            task_id=task.id,
            direction_index=direction_index,
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

    def _fetch_citation_neighbors(self, paper, *, limit: int) -> list[dict]:
        identifier = _semantic_scholar_identifier_for_paper(paper)
        if not identifier:
            return []
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
                return []
            payload = resp.json()
        except Exception:
            return []

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
        return items

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
            f"Rules: directions count must be {direction_min}-{direction_max}; each direction queries count 2-4."
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
        latest_graph = ResearchGraphSnapshotRepo(db).latest_for_task(row.id)
        graph_stats = _load_json_dict(latest_graph.stats_json) if latest_graph else {}
        if latest_graph:
            graph_stats = {
                **graph_stats,
                "status": latest_graph.status.value,
                "direction_index": latest_graph.direction_index,
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
            "last_job_type": latest_job.job_type.value if latest_job else None,
            "last_job_status": latest_job.status.value if latest_job else None,
            "last_failure_reason": (latest_job.error or None) if latest_job else None,
            "last_attempts": int(latest_job.attempts) if latest_job else 0,
            "next_retry_at": next_retry_job.scheduled_at if next_retry_job else None,
            "fulltext_stats": fulltext_stats,
            "graph_stats": graph_stats,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _fallback_directions(self, topic: str) -> list[dict]:
        base = topic.strip()
        directions = [
            {
                "name": "问题定义与评测设定",
                "queries": [f"{base} benchmark", f"{base} evaluation metrics"],
                "exclude_terms": [],
            },
            {
                "name": "核心方法与模型架构",
                "queries": [f"{base} method", f"{base} model architecture"],
                "exclude_terms": [],
            },
            {
                "name": "鲁棒性与泛化分析",
                "queries": [f"{base} robustness", f"{base} generalization"],
                "exclude_terms": [],
            },
        ]
        extras = [
            {
                "name": "数据集与标注策略",
                "queries": [f"{base} dataset", f"{base} annotation protocol"],
                "exclude_terms": [],
            },
            {
                "name": "临床/业务落地与误差分析",
                "queries": [f"{base} deployment", f"{base} error analysis"],
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


def _load_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = orjson.loads(value)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x).strip()]


def _load_json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        data = orjson.loads(value)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _load_json_list_of_dict(value: str | None) -> list[dict]:
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
