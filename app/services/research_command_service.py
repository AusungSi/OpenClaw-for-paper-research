from __future__ import annotations

from collections.abc import Callable
import re

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infra.repos import ResearchSessionRepo
from app.infra.wecom_client import WeComClient
from app.services.research_service import ResearchService


logger = get_logger("research_command")


class ResearchCommandService:
    def __init__(self, *, research_service: ResearchService, wecom_client: WeComClient) -> None:
        self.settings = get_settings()
        self.research_service = research_service
        self.wecom_client = wecom_client

    @staticmethod
    def is_research_command(text: str) -> bool:
        normalized = (text or "").strip().lower()
        return normalized.startswith("调研") or normalized.startswith("research")

    def handle(
        self,
        *,
        db: Session,
        user_id: int,
        wecom_user_id: str,
        text: str,
        reply_sink: Callable[[str], None] | None = None,
    ) -> bool:
        if not self.is_research_command(text):
            return False
        if not self.settings.research_enabled:
            self._send("调研功能尚未启用。请联系管理员打开 RESEARCH_ENABLED。", wecom_user_id, reply_sink)
            return True

        normalized = (text or "").strip()
        if normalized in {"调研", "调研 帮助", "research", "research help"}:
            self._send(self._help_text(), wecom_user_id, reply_sink)
            return True

        m_topic = re.match(r"^调研\s*主题[:：]\s*(.+)$", normalized)
        if m_topic:
            parsed = self._parse_topic_payload(m_topic.group(1))
            if isinstance(parsed, str):
                self._send(f"创建失败：{parsed}", wecom_user_id, reply_sink)
                return True
            topic, constraints = parsed
            task = self.research_service.create_task(db, user_id=user_id, topic=topic, constraints=constraints)
            constraints_tip = self._render_constraints(constraints)
            self._send(
                f"已创建调研任务 {task.task_id}，正在生成方向。\n{constraints_tip}\n你可以稍后发送“调研 状态”查看进度。",
                wecom_user_id,
                reply_sink,
            )
            return True

        if normalized == "调研 状态":
            snapshot = self.research_service.get_active_task_snapshot(db, user_id=user_id)
            if not snapshot:
                self._send("你还没有调研任务。先发：调研 主题：xxx", wecom_user_id, reply_sink)
                return True
            lines = [
                f"任务ID：{snapshot['task_id']}",
                f"主题：{snapshot['topic']}",
                f"状态：{snapshot['status']}",
                f"方向数：{len(snapshot['directions'])}",
                f"论文数：{snapshot['papers_total']}",
            ]
            if snapshot.get("last_failure_reason"):
                lines.append(f"最近失败：{snapshot['last_failure_reason']}")
            if snapshot.get("last_attempts"):
                lines.append(f"最近重试次数：{snapshot['last_attempts']}")
            if snapshot.get("next_retry_at"):
                lines.append(f"下次重试：{snapshot['next_retry_at']}")
            self._send("\n".join(lines), wecom_user_id, reply_sink)
            return True

        if normalized == "调研 全文 构建":
            task = self.research_service.get_active_task(db, user_id)
            if not task:
                self._send("你还没有调研任务。先发：调研 主题：xxx", wecom_user_id, reply_sink)
                return True
            try:
                task, queued = self.research_service.enqueue_fulltext_build(
                    db,
                    user_id=user_id,
                    task_id=task.task_id,
                )
                if queued:
                    self._send(f"已提交任务 {task.task_id} 的全文抓取任务。完成后会推送通知。", wecom_user_id, reply_sink)
                else:
                    self._send(f"任务 {task.task_id} 已有全文任务在执行。", wecom_user_id, reply_sink)
            except ValueError as exc:
                self._send(f"提交失败：{exc}", wecom_user_id, reply_sink)
            return True

        if normalized == "调研 全文 状态":
            task = self.research_service.get_active_task(db, user_id)
            if not task:
                self._send("你还没有调研任务。先发：调研 主题：xxx", wecom_user_id, reply_sink)
                return True
            try:
                data = self.research_service.get_fulltext_status(db, user_id=user_id, task_id=task.task_id)
                summary = data.get("summary") or {}
                lines = [
                    f"任务ID：{task.task_id}",
                    f"总计：{summary.get('total', 0)}",
                    f"已解析：{summary.get('parsed', 0)}",
                    f"待上传：{summary.get('need_upload', 0)}",
                    f"失败：{summary.get('failed', 0)}",
                ]
                self._send("\n".join(lines), wecom_user_id, reply_sink)
            except ValueError as exc:
                self._send(f"查询失败：{exc}", wecom_user_id, reply_sink)
            return True

        m_graph_build = re.match(r"^调研\s*图谱\s*构建(?:\s+方向\s*(\d+))?$", normalized)
        if m_graph_build:
            task = self.research_service.get_active_task(db, user_id)
            if not task:
                self._send("你还没有调研任务。先发：调研 主题：xxx", wecom_user_id, reply_sink)
                return True
            direction_index = int(m_graph_build.group(1)) if m_graph_build.group(1) else None
            try:
                task, queued = self.research_service.enqueue_graph_build(
                    db,
                    user_id=user_id,
                    task_id=task.task_id,
                    direction_index=direction_index,
                )
                suffix = f"（方向 {direction_index}）" if direction_index else ""
                if queued:
                    self._send(f"已提交任务 {task.task_id} 的图谱构建{suffix}。", wecom_user_id, reply_sink)
                else:
                    self._send(f"任务 {task.task_id} 已有图谱任务在执行。", wecom_user_id, reply_sink)
            except ValueError as exc:
                self._send(f"提交失败：{exc}", wecom_user_id, reply_sink)
            return True

        m_graph_view = re.match(r"^调研\s*图谱\s*查看(?:\s+方向\s*(\d+))?$", normalized)
        if m_graph_view:
            task = self.research_service.get_active_task(db, user_id)
            if not task:
                self._send("你还没有调研任务。先发：调研 主题：xxx", wecom_user_id, reply_sink)
                return True
            direction_index = int(m_graph_view.group(1)) if m_graph_view.group(1) else None
            url = self._graph_view_url(task.task_id, direction_index)
            self._send(f"图谱链接：{url}", wecom_user_id, reply_sink)
            return True

        if normalized == "调研 任务 列表":
            items = self.research_service.list_tasks(db, user_id=user_id, limit=8)
            if not items:
                self._send("暂无调研任务。", wecom_user_id, reply_sink)
                return True
            lines = ["最近调研任务："]
            for item in items:
                lines.append(f"- {item['task_id']} | {item['status']} | {item['topic']}")
            self._send("\n".join(lines), wecom_user_id, reply_sink)
            return True

        m_switch = re.match(r"^调研\s*任务\s*切换\s+(\S+)$", normalized)
        if m_switch:
            task_id = m_switch.group(1).strip()
            try:
                item = self.research_service.switch_task(db, user_id=user_id, task_id=task_id)
                self._send(f"已切换到任务 {item.task_id}。", wecom_user_id, reply_sink)
            except ValueError as exc:
                self._send(f"切换失败：{exc}", wecom_user_id, reply_sink)
            return True

        m_select = re.match(r"^调研\s*(选择|继续)\s+(\d+)$", normalized)
        if m_select:
            action = m_select.group(1)
            idx = int(m_select.group(2))
            session_repo = ResearchSessionRepo(db)
            session = session_repo.get_or_create(user_id, page_size=self.settings.research_page_size)
            session_repo.set_pagination(session, direction_index=idx, page=1)
            if action == "选择":
                try:
                    page = self.research_service.page_direction_papers(
                        db,
                        user_id=user_id,
                        direction_index=idx,
                        page=1,
                    )
                except ValueError as exc:
                    self._send(f"提交失败：{exc}", wecom_user_id, reply_sink)
                    return True
                if page["total"] > 0:
                    self._send(self._render_paper_page(page), wecom_user_id, reply_sink)
                    return True
            try:
                task = self.research_service.enqueue_search(db, user_id=user_id, direction_index=idx)
                label = "扩展检索" if action == "继续" else "检索"
                self._send(
                    f"已提交任务 {task.task_id} 的方向 {idx} {label}请求，稍后会推送完成通知。",
                    wecom_user_id,
                    reply_sink,
                )
            except ValueError as exc:
                self._send(f"提交失败：{exc}", wecom_user_id, reply_sink)
            return True

        m_search = re.match(r"^调研\s*检索\s+(\d+)(?:\s+数量[:：]?\s*(\d+))?$", normalized)
        if m_search:
            idx = int(m_search.group(1))
            top_n = int(m_search.group(2)) if m_search.group(2) else None
            session_repo = ResearchSessionRepo(db)
            session = session_repo.get_or_create(user_id, page_size=self.settings.research_page_size)
            session_repo.set_pagination(session, direction_index=idx, page=1)
            try:
                task = self.research_service.enqueue_search(
                    db,
                    user_id=user_id,
                    direction_index=idx,
                    top_n=top_n,
                    force_refresh=False,
                )
                self._send(
                    f"已提交任务 {task.task_id} 的方向 {idx} 检索（topN={top_n or self.settings.research_topn_default}）。",
                    wecom_user_id,
                    reply_sink,
                )
            except ValueError as exc:
                self._send(f"提交失败：{exc}", wecom_user_id, reply_sink)
            return True

        m_refresh = re.match(r"^调研\s*重新检索\s+(\d+)(?:\s+数量[:：]?\s*(\d+))?$", normalized)
        if m_refresh:
            idx = int(m_refresh.group(1))
            top_n = int(m_refresh.group(2)) if m_refresh.group(2) else None
            session_repo = ResearchSessionRepo(db)
            session = session_repo.get_or_create(user_id, page_size=self.settings.research_page_size)
            session_repo.set_pagination(session, direction_index=idx, page=1)
            try:
                task = self.research_service.enqueue_search(
                    db,
                    user_id=user_id,
                    direction_index=idx,
                    top_n=top_n,
                    force_refresh=True,
                )
                self._send(
                    f"已提交任务 {task.task_id} 的方向 {idx} 强制重检索（topN={top_n or self.settings.research_topn_default}）。",
                    wecom_user_id,
                    reply_sink,
                )
            except ValueError as exc:
                self._send(f"提交失败：{exc}", wecom_user_id, reply_sink)
            return True

        if normalized in {"调研 下一页", "调研 上一页"}:
            session = ResearchSessionRepo(db).get_or_create(user_id, page_size=self.settings.research_page_size)
            if not session.active_direction_index:
                self._send("请先发送“调研 选择 2”或“调研 检索 2”。", wecom_user_id, reply_sink)
                return True
            next_page = session.page + (1 if normalized.endswith("下一页") else -1)
            next_page = max(1, next_page)
            try:
                page = self.research_service.page_direction_papers(
                    db,
                    user_id=user_id,
                    direction_index=session.active_direction_index,
                    page=next_page,
                )
            except ValueError as exc:
                self._send(f"分页失败：{exc}", wecom_user_id, reply_sink)
                return True
            self._send(self._render_paper_page(page), wecom_user_id, reply_sink)
            return True

        m_export = re.match(r"^调研\s*导出(?:\s+格式[:：]?\s*(\w+))?$", normalized)
        if m_export:
            fmt = (m_export.group(1) or "md").strip().lower()
            try:
                path = self.research_service.export_task(db, user_id=user_id, fmt=fmt)
                if self.settings.research_export_send_file and hasattr(self.wecom_client, "send_file"):
                    ok, error = self.wecom_client.send_file(wecom_user_id, path)
                    if ok:
                        self._send(f"导出成功（{fmt}），已发送文件。", wecom_user_id, reply_sink)
                    else:
                        self.research_service.record_export_delivery(success=False)
                        self._send(f"导出成功（{fmt}）：{path}\n文件发送失败，已回退文本路径：{error}", wecom_user_id, reply_sink)
                else:
                    self._send(f"导出成功（{fmt}）：{path}", wecom_user_id, reply_sink)
            except ValueError as exc:
                self._send(f"导出失败：{exc}", wecom_user_id, reply_sink)
            return True

        m_paper = re.match(r"^调研\s*论文\s+(\d+)$", normalized)
        if m_paper:
            idx = int(m_paper.group(1))
            try:
                paper = self.research_service.get_paper_by_index(db, user_id=user_id, index=idx)
                self._send(self._render_paper_detail(paper), wecom_user_id, reply_sink)
            except ValueError as exc:
                self._send(f"查看失败：{exc}", wecom_user_id, reply_sink)
            return True

        m_upload = re.match(r"^调研\s*上传\s*PDF\s+(\d+)$", normalized, flags=re.IGNORECASE)
        if m_upload:
            idx = int(m_upload.group(1))
            task = self.research_service.get_active_task(db, user_id)
            if not task:
                self._send("你还没有调研任务。先发：调研 主题：xxx", wecom_user_id, reply_sink)
                return True
            try:
                paper = self.research_service.get_paper_by_index(db, user_id=user_id, index=idx)
            except ValueError as exc:
                self._send(f"查看失败：{exc}", wecom_user_id, reply_sink)
                return True
            endpoint = f"/api/v1/research/tasks/{task.task_id}/papers/{paper.get('paper_id')}/pdf/upload"
            self._send(
                "请调用上传接口补齐全文：\n"
                f"论文：{paper.get('title')}\n"
                f"接口：{endpoint}\n"
                "使用 multipart/form-data，字段名 file。",
                wecom_user_id,
                reply_sink,
            )
            return True

        m_doi = re.match(r"^调研\s*doi\s+(.+)$", normalized)
        if m_doi:
            doi = m_doi.group(1).strip()
            try:
                paper = self.research_service.get_paper_by_doi(db, user_id=user_id, doi=doi)
                self._send(self._render_paper_detail(paper), wecom_user_id, reply_sink)
            except ValueError as exc:
                self._send(f"查看失败：{exc}", wecom_user_id, reply_sink)
            return True

        self._send("未识别的调研命令。发送“调研 帮助”查看支持的指令。", wecom_user_id, reply_sink)
        return True

    @staticmethod
    def _help_text() -> str:
        return (
            "调研命令：\n"
            "1) 调研 主题：{topic} 年份：2021-2026 领域：xxx 数量：20 来源：semantic_scholar|arxiv\n"
            "2) 调研 状态\n"
            "3) 调研 任务 列表\n"
            "4) 调研 任务 切换 {task_id}\n"
            "5) 调研 选择 {k}\n"
            "6) 调研 检索 {k} 数量：30\n"
            "7) 调研 重新检索 {k} 数量：30\n"
            "8) 调研 下一页 / 调研 上一页\n"
            "9) 调研 论文 {index} / 调研 doi {doi}\n"
            "10) 调研 导出 / 调研 导出 格式：bib|md|json\n"
            "11) 调研 全文 构建 / 调研 全文 状态\n"
            "12) 调研 图谱 构建 [方向 k] / 调研 图谱 查看 [方向 k]\n"
            "13) 调研 上传PDF {paper_index}"
        )

    @staticmethod
    def _render_paper_page(page: dict) -> str:
        lines = [
            f"任务 {page['task_id']} | 方向 {page['direction_index']} | 第 {page['page']} 页",
            f"总计 {page['total']} 篇",
        ]
        for item in page["items"]:
            head = f"[{item['index']}] {item['title']}"
            meta = " | ".join([x for x in [str(item.get("year") or ""), item.get("venue") or "", item.get("source") or ""] if x])
            lines.append(head if not meta else f"{head}\n{meta}")
            if item.get("doi"):
                lines.append(f"DOI: {item['doi']}")
            if item.get("url"):
                lines.append(f"URL: {item['url']}")
            if item.get("method_summary"):
                lines.append(item["method_summary"])
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _render_paper_detail(paper: dict) -> str:
        lines = [paper.get("title") or "Untitled"]
        authors = paper.get("authors") or []
        if authors:
            lines.append(f"Authors: {', '.join(authors[:8])}")
        if paper.get("year") or paper.get("venue"):
            lines.append(f"Year/Venue: {paper.get('year') or '-'} / {paper.get('venue') or '-'}")
        if paper.get("doi"):
            lines.append(f"DOI: {paper['doi']}")
        if paper.get("url"):
            lines.append(f"URL: {paper['url']}")
        if paper.get("abstract"):
            abstract = str(paper["abstract"]).strip()
            lines.append(f"Abstract: {abstract[:700]}{'...' if len(abstract) > 700 else ''}")
        if paper.get("method_summary"):
            lines.append(str(paper["method_summary"]))
        if paper.get("paper_id"):
            lines.append(f"PaperID: {paper['paper_id']}")
        return "\n".join(lines)

    @staticmethod
    def _parse_topic_payload(payload: str) -> tuple[str, dict] | str:
        text = (payload or "").strip()
        if not text:
            return "请补充调研主题，例如：调研 主题：ultrasound report generation"
        marker = re.compile(r"(年份|领域|数量|来源)\s*[:：]\s*")
        matches = list(marker.finditer(text))
        topic = text
        extras: dict[str, str] = {}
        if matches:
            topic = text[: matches[0].start()].strip()
            for idx, item in enumerate(matches):
                key = item.group(1)
                start = item.end()
                end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
                value = text[start:end].strip()
                if value:
                    extras[key] = value
        if not topic:
            return "主题不能为空。请使用：调研 主题：{topic} 年份：2021-2026 数量：20 来源：arxiv"
        constraints: dict[str, object] = {}
        year_value = extras.get("年份")
        if year_value:
            parsed = re.match(r"^(\d{4})(?:\s*-\s*(\d{4}))?$", year_value)
            if not parsed:
                return "年份格式错误，请使用 2021-2026 或 2024。"
            year_from = int(parsed.group(1))
            year_to = int(parsed.group(2) or parsed.group(1))
            if year_from > year_to:
                return "年份范围错误，开始年份不能晚于结束年份。"
            constraints["year_from"] = year_from
            constraints["year_to"] = year_to
        top_n_value = extras.get("数量")
        if top_n_value:
            if not top_n_value.isdigit():
                return "数量格式错误，请填写整数，例如 数量：20。"
            constraints["top_n"] = max(1, min(100, int(top_n_value)))
        sources_value = extras.get("来源")
        if sources_value:
            raw_sources = re.split(r"[|,，/\s]+", sources_value)
            sources = [item.strip().lower() for item in raw_sources if item and item.strip()]
            allowed = {"semantic_scholar", "arxiv"}
            filtered = [src for src in sources if src in allowed]
            if not filtered:
                return "来源仅支持 semantic_scholar 或 arxiv（可用 | 分隔）。"
            constraints["sources"] = filtered
        domain_value = extras.get("领域")
        if domain_value:
            constraints["domain"] = domain_value[:120]
        return topic, constraints

    @staticmethod
    def _render_constraints(constraints: dict) -> str:
        if not constraints:
            return "约束：默认（来源=semantic_scholar,arxiv；数量=系统默认）"
        parts: list[str] = []
        if constraints.get("year_from"):
            y_from = constraints.get("year_from")
            y_to = constraints.get("year_to")
            parts.append(f"年份={y_from}-{y_to}")
        if constraints.get("top_n"):
            parts.append(f"数量={constraints.get('top_n')}")
        if constraints.get("sources"):
            parts.append(f"来源={'|'.join(constraints.get('sources') or [])}")
        if constraints.get("domain"):
            parts.append(f"领域={constraints.get('domain')}")
        return "约束：" + "，".join(parts)

    def _send(self, text: str, wecom_user_id: str, reply_sink: Callable[[str], None] | None) -> None:
        if reply_sink:
            reply_sink(text)
            return
        ok, error = self.wecom_client.send_text(wecom_user_id, text)
        if not ok:
            logger.warning("research_reply_send_failed user=%s error=%s", wecom_user_id, error)

    def _graph_view_url(self, task_id: str, direction_index: int | None) -> str:
        host = self.settings.app_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        base = f"http://{host}:{self.settings.app_port}/api/v1/research/tasks/{task_id}/graph/view"
        if direction_index is None:
            return base
        return f"{base}?direction_index={direction_index}"
