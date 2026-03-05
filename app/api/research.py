from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.mobile import get_current_user_id
from app.domain.schemas import (
    ResearchPaperDetailResponse,
    ResearchExportResponse,
    ResearchExploreStartRequest,
    ResearchExploreStartResponse,
    ResearchExploreTreeResponse,
    ResearchPaperSaveRequest,
    ResearchPaperSaveResponse,
    ResearchPaperSummarizeResponse,
    ResearchRoundNextRequest,
    ResearchRoundNextResponse,
    ResearchFulltextBuildResponse,
    ResearchFulltextStatusResponse,
    ResearchGraphBuildRequest,
    ResearchGraphBuildResponse,
    ResearchGraphResponse,
    ResearchGraphSnapshotListResponse,
    ResearchRoundProposeRequest,
    ResearchRoundProposeResponse,
    ResearchRoundSelectRequest,
    ResearchRoundSelectResponse,
    ResearchSearchResponse,
    ResearchSavedPaperListResponse,
    ResearchTaskCreateRequest,
    ResearchTaskListResponse,
    ResearchTaskPlanResponse,
    ResearchTaskResponse,
    ResearchTaskSearchEnqueueResponse,
    ResearchTaskSearchRequest,
)
from app.infra.db import get_db
from app.services.research_service import ResearchService


router = APIRouter(prefix="/api/v1/research")


def get_research_service(request: Request) -> ResearchService:
    return request.app.state.research_service


@router.post("/tasks", response_model=ResearchTaskResponse)
def create_research_task(
    payload: ResearchTaskCreateRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchTaskResponse:
    constraints = {
        "year_from": payload.year_from,
        "year_to": payload.year_to,
        "top_n": payload.top_n,
        "sources": payload.sources,
    }
    row = research_service.create_task(db, user_id=user_id, topic=payload.topic, constraints=constraints)
    data = research_service.get_task(db, user_id=user_id, task_id=row.task_id)
    return ResearchTaskResponse(**data)


@router.get("/tasks", response_model=ResearchTaskListResponse)
def list_research_tasks(
    limit: int = Query(default=10, ge=1, le=50),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchTaskListResponse:
    items = research_service.list_tasks(db, user_id=user_id, limit=limit)
    return ResearchTaskListResponse(items=[ResearchTaskResponse(**x) for x in items], total=len(items))


@router.get("/tasks/{task_id}", response_model=ResearchTaskResponse)
def get_research_task(
    task_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchTaskResponse:
    try:
        data = research_service.get_task(db, user_id=user_id, task_id=task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ResearchTaskResponse(**data)


@router.post("/tasks/{task_id}/plan", response_model=ResearchTaskPlanResponse)
def plan_task(
    task_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchTaskPlanResponse:
    try:
        task, queued = research_service.enqueue_plan(db, user_id=user_id, task_id=task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchTaskPlanResponse(task_id=task.task_id, status=task.status.value, queued=queued)


@router.post("/tasks/{task_id}/search", response_model=ResearchTaskSearchEnqueueResponse)
def search_direction(
    task_id: str,
    payload: ResearchTaskSearchRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchTaskSearchEnqueueResponse:
    try:
        research_service.switch_task(db, user_id=user_id, task_id=task_id)
        task = research_service.enqueue_search(
            db,
            user_id=user_id,
            direction_index=payload.direction_index,
            top_n=payload.top_n,
            force_refresh=payload.force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchTaskSearchEnqueueResponse(
        task_id=task.task_id,
        status=task.status.value,
        direction_index=payload.direction_index,
        force_refresh=payload.force_refresh,
    )


@router.post("/tasks/{task_id}/explore/start", response_model=ResearchExploreStartResponse)
def explore_start(
    task_id: str,
    payload: ResearchExploreStartRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchExploreStartResponse:
    try:
        task, round_row = research_service.start_exploration(
            db,
            user_id=user_id,
            task_id=task_id,
            direction_index=payload.direction_index,
            top_n=payload.top_n,
            year_from=payload.year_from,
            year_to=payload.year_to,
            sources=payload.sources,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchExploreStartResponse(
        task_id=task.task_id,
        direction_index=payload.direction_index,
        round_id=round_row.id,
        status=task.status.value,
        queued=True,
    )


@router.post("/tasks/{task_id}/explore/rounds/{round_id}/propose", response_model=ResearchRoundProposeResponse)
def explore_round_propose(
    task_id: str,
    round_id: int,
    payload: ResearchRoundProposeRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchRoundProposeResponse:
    try:
        data = research_service.propose_round_candidates(
            db,
            user_id=user_id,
            task_id=task_id,
            round_id=round_id,
            action=payload.action,
            feedback_text=payload.feedback_text,
            candidate_count=payload.candidate_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchRoundProposeResponse(**data)


@router.post("/tasks/{task_id}/explore/rounds/{round_id}/select", response_model=ResearchRoundSelectResponse)
def explore_round_select(
    task_id: str,
    round_id: int,
    payload: ResearchRoundSelectRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchRoundSelectResponse:
    try:
        data = research_service.select_round_candidate(
            db,
            user_id=user_id,
            task_id=task_id,
            round_id=round_id,
            candidate_id=payload.candidate_id,
            top_n=payload.top_n,
            force_refresh=payload.force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchRoundSelectResponse(**data)


@router.post("/tasks/{task_id}/explore/rounds/{round_id}/next", response_model=ResearchRoundNextResponse)
def explore_round_next(
    task_id: str,
    round_id: int,
    payload: ResearchRoundNextRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchRoundNextResponse:
    try:
        data = research_service.create_next_round_from_intent(
            db,
            user_id=user_id,
            task_id=task_id,
            round_id=round_id,
            intent_text=payload.intent_text,
            top_n=payload.top_n,
            force_refresh=payload.force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchRoundNextResponse(**data)


@router.get("/tasks/{task_id}/explore/tree", response_model=ResearchExploreTreeResponse)
def explore_tree(
    task_id: str,
    include_papers: bool = Query(default=False),
    paper_limit: int | None = Query(default=None, ge=1, le=50),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchExploreTreeResponse:
    try:
        data = research_service.get_exploration_tree(
            db,
            user_id=user_id,
            task_id=task_id,
            include_papers=include_papers,
            paper_limit=paper_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchExploreTreeResponse(task_id=task_id, nodes=data["nodes"], edges=data["edges"], stats=data["stats"])


@router.get("/tasks/{task_id}/papers", response_model=ResearchSearchResponse)
def get_direction_papers(
    task_id: str,
    direction_index: int = Query(..., ge=1),
    page: int = Query(default=1, ge=1),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchSearchResponse:
    try:
        research_service.switch_task(db, user_id=user_id, task_id=task_id)
        data = research_service.page_direction_papers(
            db,
            user_id=user_id,
            direction_index=direction_index,
            page=page,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchSearchResponse(**data)


@router.get("/tasks/{task_id}/papers/saved", response_model=ResearchSavedPaperListResponse)
def get_saved_papers(
    task_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchSavedPaperListResponse:
    try:
        data = research_service.list_saved_papers(
            db,
            user_id=user_id,
            task_id=task_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchSavedPaperListResponse(**data)


@router.get("/tasks/{task_id}/papers/{paper_id:path}", response_model=ResearchPaperDetailResponse)
def get_paper_detail(
    task_id: str,
    paper_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchPaperDetailResponse:
    try:
        data = research_service.get_paper_detail(
            db,
            user_id=user_id,
            task_id=task_id,
            paper_token=paper_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchPaperDetailResponse(**data)


@router.post("/tasks/{task_id}/papers/{paper_id:path}/save", response_model=ResearchPaperSaveResponse)
def save_paper(
    task_id: str,
    paper_id: str,
    payload: ResearchPaperSaveRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchPaperSaveResponse:
    try:
        data = research_service.save_paper(
            db,
            user_id=user_id,
            task_id=task_id,
            paper_token=paper_id,
            subdir=payload.subdir,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchPaperSaveResponse(**data)


@router.post("/tasks/{task_id}/papers/{paper_id:path}/summarize", response_model=ResearchPaperSummarizeResponse)
def summarize_paper(
    task_id: str,
    paper_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchPaperSummarizeResponse:
    try:
        data = research_service.enqueue_paper_summary(
            db,
            user_id=user_id,
            task_id=task_id,
            paper_token=paper_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchPaperSummarizeResponse(**data)


@router.get("/tasks/{task_id}/export", response_model=ResearchExportResponse)
def export_task(
    task_id: str,
    format: str = Query(default="md"),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchExportResponse:
    try:
        research_service.switch_task(db, user_id=user_id, task_id=task_id)
        path = research_service.export_task(db, user_id=user_id, fmt=format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchExportResponse(task_id=task_id, format=format, path=path)


@router.post("/tasks/{task_id}/fulltext/build", response_model=ResearchFulltextBuildResponse)
def build_fulltext(
    task_id: str,
    force: bool = Query(default=False),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchFulltextBuildResponse:
    try:
        task, queued = research_service.enqueue_fulltext_build(
            db,
            user_id=user_id,
            task_id=task_id,
            force=force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchFulltextBuildResponse(task_id=task.task_id, status=task.status.value, queued=queued)


@router.post("/tasks/{task_id}/fulltext/retry", response_model=ResearchFulltextBuildResponse)
def retry_fulltext(
    task_id: str,
    paper_ids: list[str] | None = None,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchFulltextBuildResponse:
    try:
        task, queued = research_service.enqueue_fulltext_build(
            db,
            user_id=user_id,
            task_id=task_id,
            force=True,
            paper_ids=paper_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchFulltextBuildResponse(task_id=task.task_id, status=task.status.value, queued=queued)


@router.get("/tasks/{task_id}/fulltext/status", response_model=ResearchFulltextStatusResponse)
def fulltext_status(
    task_id: str,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchFulltextStatusResponse:
    try:
        data = research_service.get_fulltext_status(db, user_id=user_id, task_id=task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchFulltextStatusResponse(**data)


@router.post("/tasks/{task_id}/papers/{paper_id:path}/pdf/upload")
async def upload_paper_pdf(
    task_id: str,
    paper_id: str,
    file: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> dict:
    data = await file.read()
    try:
        payload = research_service.upload_pdf_for_paper(
            db,
            user_id=user_id,
            task_id=task_id,
            paper_token=paper_id,
            filename=file.filename or f"{paper_id}.pdf",
            content=data,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task_id": task_id, **payload}


@router.post("/tasks/{task_id}/graph/build", response_model=ResearchGraphBuildResponse)
def build_graph(
    task_id: str,
    payload: ResearchGraphBuildRequest,
    force: bool = Query(default=False),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchGraphBuildResponse:
    force_refresh = bool(force or payload.force_refresh)
    try:
        task, queued = research_service.enqueue_graph_build(
            db,
            user_id=user_id,
            task_id=task_id,
            direction_index=payload.direction_index,
            round_id=payload.round_id,
            view=payload.view,
            citation_sources=payload.citation_sources,
            seed_top_n=payload.seed_top_n,
            expand_limit_per_paper=payload.expand_limit_per_paper,
            force_refresh=payload.force_refresh,
            force=force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchGraphBuildResponse(
        task_id=task.task_id,
        status=task.status.value,
        queued=queued,
        direction_index=payload.direction_index,
        round_id=payload.round_id,
        view=payload.view,
    )


@router.post("/tasks/{task_id}/explore/rounds/{round_id}/citation/build", response_model=ResearchGraphBuildResponse)
def build_round_citation_graph(
    task_id: str,
    round_id: int,
    payload: ResearchGraphBuildRequest,
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchGraphBuildResponse:
    try:
        task, queued = research_service.enqueue_round_citation_build(
            db,
            user_id=user_id,
            task_id=task_id,
            round_id=round_id,
            seed_top_n=payload.seed_top_n,
            expand_limit_per_paper=payload.expand_limit_per_paper,
            citation_sources=payload.citation_sources,
            force_refresh=payload.force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchGraphBuildResponse(
        task_id=task.task_id,
        status=task.status.value,
        queued=queued,
        direction_index=payload.direction_index,
        round_id=round_id,
        view="citation",
    )


@router.get("/tasks/{task_id}/graph", response_model=ResearchGraphResponse)
def get_graph(
    task_id: str,
    view: str = Query(default="citation"),
    direction_index: int | None = Query(default=None),
    round_id: int | None = Query(default=None),
    include_papers: bool = Query(default=False),
    paper_limit: int | None = Query(default=None, ge=1, le=50),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchGraphResponse:
    try:
        data = research_service.get_graph_snapshot(
            db,
            user_id=user_id,
            task_id=task_id,
            direction_index=direction_index,
            round_id=round_id,
            view=view,
            include_papers=include_papers,
            paper_limit=paper_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchGraphResponse(**data)


@router.get("/tasks/{task_id}/graph/snapshots", response_model=ResearchGraphSnapshotListResponse)
def list_graph_snapshots(
    task_id: str,
    view: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchGraphSnapshotListResponse:
    try:
        data = research_service.list_graph_snapshots(
            db,
            user_id=user_id,
            task_id=task_id,
            view=view,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResearchGraphSnapshotListResponse(**data)


@router.get("/tasks/{task_id}/graph/view", response_class=HTMLResponse)
def graph_view(
    task_id: str,
    view: str = Query(default="citation"),
    direction_index: int | None = Query(default=None),
    round_id: int | None = Query(default=None),
    _user_id: int = Depends(get_current_user_id),
) -> HTMLResponse:
    direction_q = f"&direction_index={direction_index}" if direction_index is not None else ""
    round_q = f"&round_id={round_id}" if round_id is not None else ""
    view_q = f"&view={view}"
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>MemoMate Research Graph</title>
  <script src="https://cdn.jsdelivr.net/npm/cytoscape@3.31.2/dist/cytoscape.min.js"></script>
  <style>
    :root {{
      --bg: #f3f1ea;
      --panel: #fbfaf6;
      --ink: #262421;
      --muted: #6b655e;
      --edge: #8e877e;
      --topic: #176087;
      --direction: #1f8a70;
      --paper: #d97706;
    }}
    body {{ margin: 0; background: radial-gradient(circle at top, #fff9ee 0%, var(--bg) 60%); color: var(--ink); font-family: "Source Han Sans SC","Noto Sans SC",sans-serif; }}
    .wrap {{ display: grid; grid-template-columns: 1fr 340px; min-height: 100vh; }}
    #cy {{ min-height: 100vh; }}
    .panel {{ background: var(--panel); border-left: 1px solid #ddd4c8; padding: 14px; overflow: auto; }}
    .title {{ font-size: 18px; font-weight: 700; margin-bottom: 10px; }}
    .meta {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    .item {{ margin-bottom: 12px; font-size: 13px; line-height: 1.5; word-break: break-word; }}
    .label {{ color: var(--muted); font-weight: 600; }}
    .search {{ width: 100%; border: 1px solid #d5cfc4; border-radius: 8px; padding: 8px 10px; font-size: 14px; box-sizing: border-box; margin-bottom: 10px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div id="cy"></div>
    <div class="panel">
      <div class="title">Research Graph</div>
      <input class="search" id="searchInput" placeholder="搜索标题/ID"/>
      <div class="meta" id="summary">加载中...</div>
      <div id="detail" class="item">点击节点查看详情</div>
    </div>
  </div>
  <script>
    const token = localStorage.getItem("memomate_access_token") || "";
    const api = `/api/v1/research/tasks/{task_id}/graph?` + `t=${{Date.now()}}{view_q}{direction_q}{round_q}`;
    fetch(api, {{ headers: token ? {{ Authorization: `Bearer ${{token}}` }} : {{}} }})
      .then(r => r.json())
      .then(data => {{
        document.getElementById("summary").innerText = `节点 ${{data.nodes.length}} | 边 ${{data.edges.length}} | 状态 ${{data.status}}`;
        const elements = [];
        data.nodes.forEach(n => elements.push({{ data: {{ id: n.id, label: n.label, type: n.type, year: n.year || "", source: n.source || "", fulltext: n.fulltext_status || "", direction: n.direction_index || "" }} }}));
        data.edges.forEach(e => elements.push({{ data: {{ source: e.source, target: e.target, edgeType: e.type, weight: e.weight || 1 }} }}));
        const cy = cytoscape({{
          container: document.getElementById('cy'),
          elements,
          layout: {{ name: 'cose', animate: false, fit: true, padding: 30 }},
          style: [
            {{ selector: 'node', style: {{ 'label': 'data(label)', 'font-size': 11, 'text-wrap':'wrap', 'text-max-width': 140, 'text-valign': 'center', 'text-halign':'center', 'background-color': '#888', 'color': '#1f1f1f', 'width': 20, 'height': 20 }} }},
            {{ selector: 'node[type=\"topic\"]', style: {{ 'background-color': 'var(--topic)', 'width': 38, 'height': 38, 'color':'#fff' }} }},
            {{ selector: 'node[type=\"direction\"]', style: {{ 'background-color': 'var(--direction)', 'width': 30, 'height': 30, 'color':'#fff' }} }},
            {{ selector: 'node[type=\"paper\"]', style: {{ 'background-color': 'var(--paper)' }} }},
            {{ selector: 'edge', style: {{ 'curve-style':'bezier', 'line-color':'var(--edge)', 'target-arrow-shape':'triangle', 'target-arrow-color':'var(--edge)', 'width': 1.5, 'opacity': 0.85 }} }},
            {{ selector: 'edge[edgeType=\"cited_by\"]', style: {{ 'line-style':'dashed' }} }},
            {{ selector: '.faded', style: {{ 'opacity': 0.12 }} }},
          ],
        }});
        cy.on('tap', 'node', (evt) => {{
          const d = evt.target.data();
          document.getElementById('detail').innerHTML = `
            <div class="item"><span class="label">ID:</span> ${{d.id}}</div>
            <div class="item"><span class="label">标题:</span> ${{d.label}}</div>
            <div class="item"><span class="label">类型:</span> ${{d.type}}</div>
            <div class="item"><span class="label">年份:</span> ${{d.year || "-"}}</div>
            <div class="item"><span class="label">来源:</span> ${{d.source || "-"}}</div>
            <div class="item"><span class="label">全文状态:</span> ${{d.fulltext || "-"}}</div>`;
          cy.elements().removeClass('faded');
          const neighborhood = evt.target.closedNeighborhood();
          cy.elements().not(neighborhood).addClass('faded');
        }});
        document.getElementById('searchInput').addEventListener('input', (e) => {{
          const q = (e.target.value || '').toLowerCase().trim();
          cy.elements().removeClass('faded');
          if (!q) return;
          cy.nodes().forEach(n => {{
            const hit = `${{n.data('label')}} ${{n.id()}}`.toLowerCase().includes(q);
            if (!hit) n.addClass('faded');
          }});
        }});
      }})
      .catch(err => {{
        document.getElementById("summary").innerText = "图谱加载失败";
        document.getElementById("detail").innerText = String(err);
      }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)
