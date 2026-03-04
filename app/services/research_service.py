from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from time import sleep
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET
import re

import httpx
import orjson
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.enums import ResearchJobType, ResearchTaskStatus
from app.domain.models import ResearchTask, User
from app.infra.repos import (
    ResearchDirectionRepo,
    ResearchJobRepo,
    ResearchPaperRepo,
    ResearchSessionRepo,
    ResearchTaskRepo,
    UserRepo,
)
from app.infra.wecom_client import WeComClient
from app.llm.openclaw_client import LLMCallResult, LLMTaskType, OpenClawClient


logger = get_logger("research")


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
    ) -> ResearchTask:
        task = self.get_active_task(db, user_id)
        if not task:
            raise ValueError("no active research task")
        if direction_index < 1:
            raise ValueError("direction index must be >= 1")
        payload = {"direction_index": direction_index, "top_n": top_n or self.settings.research_topn_default}
        ResearchJobRepo(db).enqueue(task.id, ResearchJobType.SEARCH, payload)
        task.status = ResearchTaskStatus.SEARCHING
        task.updated_at = datetime.now(timezone.utc)
        db.add(task)
        db.flush()
        return task

    def process_one_job(self, db: Session) -> int:
        job_repo = ResearchJobRepo(db)
        job = job_repo.next_queued()
        if not job:
            return 0
        task = db.get(ResearchTask, job.task_id)
        if not task:
            job_repo.mark_failed(job, "task_not_found")
            return 1
        payload = {}
        try:
            payload = orjson.loads(job.payload_json)
        except Exception:
            payload = {}

        job_repo.mark_running(job)
        try:
            if job.job_type == ResearchJobType.PLAN:
                self._run_plan_job(db, task, payload)
            else:
                self._run_search_job(db, task, payload)
            job_repo.mark_done(job)
        except Exception as exc:
            logger.exception("research_job_failed task_id=%s job_id=%s", task.task_id, job.id)
            max_attempts = max(1, int(self.settings.research_job_max_attempts))
            if job.attempts < max_attempts:
                base_delay = max(1, int(self.settings.research_job_backoff_seconds))
                delay_seconds = min(300, base_delay * (2 ** max(0, job.attempts - 1)))
                task.status = self._task_status_for_retry(job.job_type)
                task.updated_at = datetime.now(timezone.utc)
                db.add(task)
                db.flush()
                job_repo.mark_retry(job, error=str(exc), delay_seconds=delay_seconds)
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
                job_repo.mark_failed(job, str(exc))
                self._notify_user(db, task.user_id, f"调研任务 {task.task_id} 失败：{str(exc)[:120]}")
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

        fmt_norm = (fmt or "md").lower().strip()
        if fmt_norm == "bib":
            return str(bib_path)
        if fmt_norm == "json":
            return str(json_path)
        return str(report_path)

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
        for query in query_terms[:4]:
            effective_query = _merge_query_and_excludes(query, exclude_terms)
            if "semantic_scholar" in allowed_sources:
                all_papers.extend(self._search_semantic_scholar(effective_query, top_n=top_n, constraints=constraints))
            if "arxiv" in allowed_sources:
                all_papers.extend(self._search_arxiv(effective_query, top_n=top_n, constraints=constraints))
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
        if not isinstance(data, dict):
            return []
        raw_dirs = data.get("directions")
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

    def _search_semantic_scholar(self, query: str, *, top_n: int, constraints: dict) -> list[dict]:
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
            except Exception:
                if attempt < 2:
                    sleep(0.25 * (2**attempt))
                    continue
                return []

            if resp.status_code == 429:
                if attempt < 2:
                    sleep(0.5 * (2**attempt))
                    continue
                logger.warning(
                    "semantic_scholar_rate_limited query=%s has_api_key=%s",
                    query[:120],
                    bool(api_key),
                )
                return []
            if 500 <= resp.status_code < 600:
                if attempt < 2:
                    sleep(0.35 * (2**attempt))
                    continue
                return []
            if resp.status_code >= 400:
                logger.warning("semantic_scholar_http_error status=%s query=%s", resp.status_code, query[:120])
                return []
            try:
                payload = resp.json()
            except Exception:
                return []
            break

        if payload is None:
            return []
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
        return papers

    def _search_arxiv(self, query: str, *, top_n: int, constraints: dict) -> list[dict]:
        start = 0
        max_results = max(1, min(100, top_n))
        q = quote_plus(query)
        url = f"https://export.arxiv.org/api/query?search_query=all:{q}&start={start}&max_results={max_results}"
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(url)
                if resp.status_code >= 400:
                    return []
                xml = resp.text
        except Exception:
            return []
        if not xml or not xml.strip():
            return []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
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
        return papers

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


def _safe_xml_text(node) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


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
