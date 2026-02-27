from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import ipaddress
from time import perf_counter
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.domain.enums import ReminderSource
from app.domain.schemas import (
    AdminActionResponse,
    AdminChatReplyItem,
    AdminChatSendRequest,
    AdminChatSendResponse,
    AdminDispatchResponse,
    AdminInboundMessageListResponse,
    AdminOverviewResponse,
    AdminReminderListResponse,
    AdminUserAuditOverviewResponse,
    AdminUserDeliveryListResponse,
    AdminUserDeviceListResponse,
    AdminUserListResponse,
    AdminUserPendingActionListResponse,
    AdminUserVoiceRecordListResponse,
    ReminderSnoozeRequest,
)
from app.infra.db import get_db
from app.infra.repos import UserRepo
from app.services.admin_service import AdminService
from app.services.asr_service import AsrService
from app.services.intent_service import IntentService
from app.services.message_ingest import MessageIngestService
from app.services.reply_generation_service import ReplyGenerationService
from app.services.scheduler_service import SchedulerService


router = APIRouter()


def require_local_admin_access(request: Request) -> None:
    host = request.client.host if request.client else ""
    if _is_localhost(host):
        return
    raise HTTPException(status_code=403, detail="admin endpoints are localhost-only")


def _is_localhost(host: str) -> bool:
    if not host:
        return False
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def get_scheduler(request: Request) -> SchedulerService:
    return request.app.state.scheduler_service


def get_ingest_service(request: Request) -> MessageIngestService:
    return request.app.state.message_ingest_service


def get_intent_service(request: Request) -> IntentService:
    return request.app.state.intent_service


def get_reply_service(request: Request) -> ReplyGenerationService:
    return request.app.state.reply_generation_service


def get_asr_service(request: Request) -> AsrService:
    return request.app.state.asr_service


def get_admin_service(db: Session = Depends(get_db)) -> AdminService:
    return AdminService(db)


@router.get("/admin", response_class=HTMLResponse)
def admin_home(_admin: None = Depends(require_local_admin_access)) -> HTMLResponse:
    return HTMLResponse(_home_html())


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(_admin: None = Depends(require_local_admin_access)) -> HTMLResponse:
    return HTMLResponse(_users_html())


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail_page(user_id: int, _admin: None = Depends(require_local_admin_access)) -> HTMLResponse:
    return HTMLResponse(_user_detail_html(user_id))


@router.get("/admin/chat", response_class=HTMLResponse)
def admin_chat_page(_admin: None = Depends(require_local_admin_access)) -> HTMLResponse:
    return HTMLResponse(_chat_html())


@router.get("/api/v1/admin", include_in_schema=False)
def admin_api_root(_admin: None = Depends(require_local_admin_access)) -> RedirectResponse:
    return RedirectResponse(url="/api/v1/admin/overview")


@router.get("/api/v1/admin/overview", response_model=AdminOverviewResponse)
def admin_overview(
    request: Request,
    admin_service: AdminService = Depends(get_admin_service),
    ingest_service: MessageIngestService = Depends(get_ingest_service),
    intent_service: IntentService = Depends(get_intent_service),
    reply_service: ReplyGenerationService = Depends(get_reply_service),
    asr_service: AsrService = Depends(get_asr_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminOverviewResponse:
    scheduler = get_scheduler(request)
    wecom_send_ok, wecom_last_error = request.app.state.wecom_client.last_send_status()
    intent_ok, intent_provider_name, _intent_error = intent_service.health_status()
    reply_ok, reply_provider_name, _reply_error = reply_service.health_status()
    asr_ok, asr_provider_name, _asr_error = asr_service.health_status()
    return admin_service.overview(
        scheduler_started=scheduler.started,
        ollama_ok=request.app.state.ollama_client.healthcheck(),
        wecom_send_ok=wecom_send_ok,
        wecom_last_error=wecom_last_error,
        webhook_dedup_ok=ingest_service.webhook_dedup_ok,
        intent_provider_name=intent_provider_name if intent_ok else f"{intent_provider_name}(degraded)",
        reply_provider_name=reply_provider_name if reply_ok else f"{reply_provider_name}(degraded)",
        asr_provider_name=asr_provider_name if asr_ok else f"{asr_provider_name}(degraded)",
        dedup_duplicates=ingest_service.dedup_duplicates,
        dedup_failures=ingest_service.dedup_failures,
    )


@router.post("/api/v1/admin/scheduler/dispatch-once", response_model=AdminDispatchResponse)
async def admin_dispatch_once(
    scheduler: SchedulerService = Depends(get_scheduler),
    _admin: None = Depends(require_local_admin_access),
) -> AdminDispatchResponse:
    started = perf_counter()
    try:
        processed = await scheduler.run_dispatch_cycle()
        return AdminService.dispatch_response(
            processed_count=processed,
            duration_ms=int((perf_counter() - started) * 1000),
            error=None,
        )
    except Exception as exc:
        return AdminService.dispatch_response(
            processed_count=0,
            duration_ms=int((perf_counter() - started) * 1000),
            error=str(exc),
        )


@router.get("/api/v1/admin/users", response_model=AdminUserListResponse)
def admin_list_users(
    q: str | None = Query(default=None),
    timezone_name: str | None = Query(default=None, alias="timezone"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    sort: str = Query(default="updated_at"),
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminUserListResponse:
    if sort not in {"created_at", "updated_at"}:
        raise HTTPException(status_code=400, detail="sort must be created_at or updated_at")
    return admin_service.list_users(q=q, timezone_name=timezone_name, page=page, size=size, sort=sort)


@router.get("/api/v1/admin/users/{user_id}/overview", response_model=AdminUserAuditOverviewResponse)
def admin_user_overview(
    user_id: int,
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminUserAuditOverviewResponse:
    data = admin_service.get_user_overview(user_id)
    if not data:
        raise HTTPException(status_code=404, detail="user not found")
    return data


@router.get("/api/v1/admin/users/{user_id}/reminders", response_model=AdminReminderListResponse)
def admin_user_reminders(
    user_id: int,
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    q: str | None = Query(default=None),
    from_utc: datetime | None = Query(default=None),
    to_utc: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminReminderListResponse:
    _ensure_user_exists(admin_service, user_id)
    try:
        return admin_service.list_user_reminders(
            user_id=user_id,
            status=status,
            source=source,
            q=q,
            from_utc=from_utc,
            to_utc=to_utc,
            page=page,
            size=size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/v1/admin/users/{user_id}/pending-actions", response_model=AdminUserPendingActionListResponse)
def admin_user_pending_actions(
    user_id: int,
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminUserPendingActionListResponse:
    _ensure_user_exists(admin_service, user_id)
    try:
        return admin_service.list_user_pending_actions(user_id=user_id, status=status, page=page, size=size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/v1/admin/users/{user_id}/inbound-messages", response_model=AdminInboundMessageListResponse)
def admin_user_inbound_messages(
    user_id: int,
    msg_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminInboundMessageListResponse:
    _ensure_user_exists(admin_service, user_id)
    return admin_service.list_user_inbound_messages(user_id=user_id, msg_type=msg_type, page=page, size=size)


@router.get("/api/v1/admin/users/{user_id}/voice-records", response_model=AdminUserVoiceRecordListResponse)
def admin_user_voice_records(
    user_id: int,
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminUserVoiceRecordListResponse:
    _ensure_user_exists(admin_service, user_id)
    try:
        return admin_service.list_user_voice_records(
            user_id=user_id,
            status=status,
            source=source,
            page=page,
            size=size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/v1/admin/users/{user_id}/deliveries", response_model=AdminUserDeliveryListResponse)
def admin_user_deliveries(
    user_id: int,
    status: str | None = Query(default=None),
    from_utc: datetime | None = Query(default=None),
    to_utc: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminUserDeliveryListResponse:
    _ensure_user_exists(admin_service, user_id)
    try:
        return admin_service.list_user_deliveries(
            user_id=user_id,
            status=status,
            from_utc=from_utc,
            to_utc=to_utc,
            page=page,
            size=size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/v1/admin/users/{user_id}/devices", response_model=AdminUserDeviceListResponse)
def admin_user_devices(
    user_id: int,
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminUserDeviceListResponse:
    _ensure_user_exists(admin_service, user_id)
    return admin_service.list_user_devices(user_id)


@router.post("/api/v1/admin/reminders/{reminder_id}/cancel", response_model=AdminActionResponse)
def admin_cancel_reminder(
    reminder_id: int,
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminActionResponse:
    try:
        return admin_service.cancel_reminder(reminder_id)
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/api/v1/admin/reminders/{reminder_id}/retry", response_model=AdminActionResponse)
def admin_retry_reminder(
    reminder_id: int,
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminActionResponse:
    try:
        return admin_service.retry_reminder(reminder_id)
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/api/v1/admin/reminders/{reminder_id}/snooze", response_model=AdminActionResponse)
def admin_snooze_reminder(
    reminder_id: int,
    payload: ReminderSnoozeRequest,
    admin_service: AdminService = Depends(get_admin_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminActionResponse:
    try:
        return admin_service.snooze_reminder(reminder_id, payload.minutes)
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/api/v1/admin/chat/send", response_model=AdminChatSendResponse)
def admin_chat_send(
    payload: AdminChatSendRequest,
    db: Session = Depends(get_db),
    ingest_service: MessageIngestService = Depends(get_ingest_service),
    intent_service: IntentService = Depends(get_intent_service),
    reply_service: ReplyGenerationService = Depends(get_reply_service),
    _admin: None = Depends(require_local_admin_access),
) -> AdminChatSendResponse:
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    user = UserRepo(db).get_by_id(payload.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")

    session_id = (payload.session_id or "").strip() or uuid.uuid4().hex[:12]
    msg_id = f"admin-chat-{session_id}-{uuid.uuid4().hex[:8]}"
    raw_xml = f"<admin_chat><session_id>{escape(session_id)}</session_id><text>{escape(text)}</text></admin_chat>"

    replies: list[AdminChatReplyItem] = []

    def reply_sink(content: str) -> None:
        replies.append(AdminChatReplyItem(text=content, created_at=datetime.now(timezone.utc)))

    pipeline_status = "ok"
    try:
        ingest_service.process_text_message(
            db=db,
            wecom_user_id=user.wecom_user_id,
            msg_id=msg_id,
            raw_xml=raw_xml,
            text=text,
            reply_sink=reply_sink,
            message_source=ReminderSource.ADMIN_CHAT,
        )
    except Exception as exc:
        pipeline_status = "error"
        replies.append(AdminChatReplyItem(text=f"[pipeline_error] {exc}", created_at=datetime.now(timezone.utc)))

    return AdminChatSendResponse(
        session_id=session_id,
        msg_id=msg_id,
        user_id=user.id,
        wecom_user_id=user.wecom_user_id,
        input_text=text,
        source=ReminderSource.ADMIN_CHAT,
        replies=replies,
        pipeline_status=pipeline_status,
        errors={
            "intent_last_error": intent_service.last_error,
            "reply_last_error": reply_service.last_error,
        },
    )


def _ensure_user_exists(admin_service: AdminService, user_id: int) -> None:
    if not admin_service.user_exists(user_id):
        raise HTTPException(status_code=404, detail="user not found")


def _home_html() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MemoMate Admin</title>
  <style>
    :root { --bg:#0d1117; --card:#161b22; --line:#2b3440; --text:#e6edf3; --muted:#9aa7b5; --accent:#2f81f7; }
    body { margin:0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: radial-gradient(circle at 15% 15%, #1f2a37, #0d1117 55%); color:var(--text); }
    .wrap { max-width: 1080px; margin: 24px auto; padding: 0 16px; }
    .row { display:flex; gap:12px; flex-wrap: wrap; }
    .card { background:var(--card); border:1px solid var(--line); border-radius: 14px; padding:14px; flex:1; min-width: 280px; }
    button, a.btn { background:var(--accent); color:white; border:none; padding:8px 12px; border-radius:10px; text-decoration:none; cursor:pointer; display:inline-block; }
    pre { white-space: pre-wrap; word-break: break-word; font-size:12px; color:var(--muted); background:#0f141b; border:1px solid #28313d; border-radius:8px; padding:12px; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>MemoMate 管理员面板</h1>
    <p>仅限本机访问，用于系统健康检查、用户审计和本地对话调试。</p>
    <div class="row">
      <div class="card">
        <h3>全局状态</h3>
        <button id="refreshBtn">刷新概览</button>
        <button id="dispatchBtn">立即执行调度</button>
        <pre id="overview">loading...</pre>
      </div>
      <div class="card">
        <h3>用户审计</h3>
        <p>查看提醒、消息、语音、投递和设备状态。</p>
        <a class="btn" href="/admin/users">打开用户列表</a>
      </div>
      <div class="card">
        <h3>对话调试台</h3>
        <p>直接在后台发送文本，复用正式业务链路，不依赖微信 tunnel。</p>
        <a class="btn" href="/admin/chat">打开调试台</a>
      </div>
    </div>
  </div>
  <script>
    async function refreshOverview() {
      const el = document.getElementById('overview');
      const r = await fetch('/api/v1/admin/overview');
      el.textContent = JSON.stringify(await r.json(), null, 2);
    }
    document.getElementById('refreshBtn').addEventListener('click', refreshOverview);
    document.getElementById('dispatchBtn').addEventListener('click', async () => {
      const r = await fetch('/api/v1/admin/scheduler/dispatch-once', { method: 'POST' });
      const data = await r.json();
      alert('dispatch complete: ' + JSON.stringify(data));
      refreshOverview();
    });
    refreshOverview();
  </script>
</body>
</html>
""".strip()


def _users_html() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MemoMate Admin Users</title>
  <style>
    :root { --bg:#f4f7fb; --card:#ffffff; --line:#d9e1ea; --text:#102134; --muted:#586677; --accent:#0f6cff; }
    body { margin:0; font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif; background: linear-gradient(135deg, #eff4ff, #f6fbff); color:var(--text); }
    .wrap { max-width: 1160px; margin: 24px auto; padding: 0 16px; }
    .panel { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; }
    .filters { display:flex; gap:8px; flex-wrap: wrap; margin-bottom: 12px; }
    input, select, button { padding: 8px 10px; border:1px solid #c7d2de; border-radius:10px; }
    button { background:var(--accent); color:#fff; border:none; cursor:pointer; }
    table { width:100%; border-collapse: collapse; font-size:14px; }
    th, td { padding:10px; border-bottom:1px solid #e3e9f0; text-align:left; }
    th { color:#4b5b6b; font-weight:600; }
    a { color:var(--accent); text-decoration:none; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>用户审计列表</h1>
    <p>可按用户查看提醒、消息、语音、投递与设备状态。</p>
    <div class="panel">
      <div class="filters">
        <input id="q" placeholder="wecom_user_id 关键字" />
        <input id="tz" placeholder="timezone, e.g. Asia/Shanghai" />
        <select id="sort">
          <option value="updated_at">updated_at</option>
          <option value="created_at">created_at</option>
        </select>
        <button id="searchBtn">查询</button>
        <a href="/admin">返回总览</a>
      </div>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>WeCom</th><th>TZ</th><th>待提醒</th><th>24h失败投递</th><th>最近入站</th><th>最近语音状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
  <script>
    async function loadUsers() {
      const q = encodeURIComponent(document.getElementById('q').value || '');
      const tz = encodeURIComponent(document.getElementById('tz').value || '');
      const sort = encodeURIComponent(document.getElementById('sort').value || 'updated_at');
      const r = await fetch(`/api/v1/admin/users?q=${q}&timezone=${tz}&sort=${sort}&page=1&size=100`);
      const data = await r.json();
      const rows = document.getElementById('rows');
      rows.innerHTML = '';
      (data.items || []).forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${item.id}</td>
          <td>${item.wecom_user_id}</td>
          <td>${item.timezone}</td>
          <td>${item.pending_reminders}</td>
          <td>${item.failed_deliveries_24h}</td>
          <td>${item.last_inbound_at || ''}</td>
          <td>${item.last_voice_status || ''}</td>
          <td><a href="/admin/users/${item.id}">详情</a></td>
        `;
        rows.appendChild(tr);
      });
    }
    document.getElementById('searchBtn').addEventListener('click', loadUsers);
    loadUsers();
  </script>
</body>
</html>
""".strip()


def _user_detail_html(user_id: int) -> str:
    return (
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MemoMate User Audit</title>
  <style>
    :root { --bg:#0b1320; --card:#152238; --line:#2d3f58; --text:#e7edf6; --muted:#9db0c7; --accent:#22c55e; }
    body { margin:0; font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif; color:var(--text); background: linear-gradient(145deg, #0b1320, #13213a 70%); }
    .wrap { max-width: 1220px; margin: 20px auto; padding: 0 14px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 12px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px; }
    h1, h3 { margin-top:0; }
    pre { white-space: pre-wrap; word-break: break-word; font-size:12px; color:var(--muted); background:#0f1a2a; border:1px solid #2a3a52; border-radius:8px; padding:10px; max-height: 360px; overflow:auto; }
    a { color:#0f172a; background:var(--accent); border:none; border-radius:8px; padding:8px 10px; text-decoration:none; display:inline-block; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>用户审计详情 #"""
        + str(user_id)
        + """</h1>
    <p>包含提醒、待确认动作、入站消息、语音记录、投递记录和设备信息。</p>
    <a href="/admin/users">返回列表</a>
    <div class="grid">
      <div class="card"><h3>Overview</h3><pre id="overview"></pre></div>
      <div class="card"><h3>Reminders</h3><pre id="reminders"></pre></div>
      <div class="card"><h3>Pending Actions</h3><pre id="pending"></pre></div>
      <div class="card"><h3>Inbound Messages</h3><pre id="inbound"></pre></div>
      <div class="card"><h3>Voice Records</h3><pre id="voices"></pre></div>
      <div class="card"><h3>Deliveries</h3><pre id="deliveries"></pre></div>
      <div class="card"><h3>Devices</h3><pre id="devices"></pre></div>
    </div>
  </div>
  <script>
    const userId = """
        + str(user_id)
        + """;
    async function fetchAndDump(path, targetId) {
      const r = await fetch(path);
      const body = await r.json();
      document.getElementById(targetId).textContent = JSON.stringify(body, null, 2);
    }
    fetchAndDump(`/api/v1/admin/users/${userId}/overview`, 'overview');
    fetchAndDump(`/api/v1/admin/users/${userId}/reminders?page=1&size=50`, 'reminders');
    fetchAndDump(`/api/v1/admin/users/${userId}/pending-actions?page=1&size=50`, 'pending');
    fetchAndDump(`/api/v1/admin/users/${userId}/inbound-messages?page=1&size=50`, 'inbound');
    fetchAndDump(`/api/v1/admin/users/${userId}/voice-records?page=1&size=50`, 'voices');
    fetchAndDump(`/api/v1/admin/users/${userId}/deliveries?page=1&size=50`, 'deliveries');
    fetchAndDump(`/api/v1/admin/users/${userId}/devices`, 'devices');
  </script>
</body>
</html>
"""
    ).strip()


def _chat_html() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MemoMate Admin Chat</title>
  <style>
    :root { --bg:#0f172a; --card:#111b31; --line:#2e3d57; --text:#e8f0fb; --muted:#9fb0c8; --accent:#22c55e; }
    body { margin:0; font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif; color:var(--text); background: radial-gradient(circle at 15% 10%, #1f2f4b, #0f172a 60%); }
    .wrap { max-width: 1140px; margin: 20px auto; padding: 0 16px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }
    .row { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
    select, input, button, textarea { border-radius:8px; border:1px solid #30415d; background:#0f1a2d; color:#e8f0fb; padding:8px 10px; }
    button { background:var(--accent); color:#05220f; border:none; cursor:pointer; font-weight:600; }
    textarea { width:100%; min-height:90px; }
    #chat { margin-top:12px; max-height:420px; overflow:auto; border:1px solid #2e3d57; border-radius:10px; padding:10px; background:#0c1526; }
    .msg { margin-bottom:8px; padding:8px 10px; border-radius:8px; font-size:14px; }
    .in { background:#143052; }
    .out { background:#1a3a27; }
    .meta { color:var(--muted); font-size:12px; margin-top:4px; }
    .warn { color:#fecaca; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>后台对话调试台</h1>
    <p class="warn">调试输入与回复仅在本页面显示；如果创建了提醒，会写入正式链路并参与到点微信提醒。</p>
    <a href="/admin">返回总览</a>
    <div class="card" style="margin-top:12px;">
      <div class="row">
        <select id="userSelect"></select>
        <input id="sessionId" placeholder="session_id (可留空自动生成)" />
        <button id="loadUsersBtn">刷新用户</button>
        <button id="clearBtn">清空会话</button>
      </div>
      <textarea id="inputText" placeholder="输入要测试的文本，比如：明天早上9点提醒我开会"></textarea>
      <div class="row" style="margin-top:8px;">
        <button id="sendBtn">发送</button>
        <button class="quick" data-text="明天早上9点提醒我开会">新增样例</button>
        <button class="quick" data-text="确认">确认</button>
        <button class="quick" data-text="查询">查询</button>
        <button class="quick" data-text="删除 开会">删除样例</button>
      </div>
      <div id="chat"></div>
    </div>
  </div>
  <script>
    function append(type, text, meta) {
      const box = document.getElementById('chat');
      const div = document.createElement('div');
      div.className = `msg ${type}`;
      div.innerHTML = `<div>${text}</div><div class="meta">${meta || ''}</div>`;
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
    }

    async function loadUsers() {
      const sel = document.getElementById('userSelect');
      const r = await fetch('/api/v1/admin/users?page=1&size=200&sort=updated_at');
      const data = await r.json();
      sel.innerHTML = '';
      (data.items || []).forEach(u => {
        const opt = document.createElement('option');
        opt.value = u.id;
        opt.textContent = `${u.id} | ${u.wecom_user_id} | ${u.timezone}`;
        sel.appendChild(opt);
      });
    }

    async function send() {
      const userId = Number(document.getElementById('userSelect').value);
      const text = (document.getElementById('inputText').value || '').trim();
      let sessionId = (document.getElementById('sessionId').value || '').trim();
      if (!text) return;
      append('in', text, `user_id=${userId}`);
      const r = await fetch('/api/v1/admin/chat/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, text, session_id: sessionId || null }),
      });
      const data = await r.json();
      if (!r.ok) {
        append('out', `[error] ${data.detail || JSON.stringify(data)}`, '');
        return;
      }
      if (!sessionId) {
        document.getElementById('sessionId').value = data.session_id;
      }
      const replies = data.replies || [];
      if (!replies.length) {
        append('out', '[no reply]', `msg_id=${data.msg_id} source=${data.source}`);
      } else {
        replies.forEach(item => append('out', item.text, `msg_id=${data.msg_id} source=${data.source}`));
      }
    }

    document.getElementById('loadUsersBtn').addEventListener('click', loadUsers);
    document.getElementById('sendBtn').addEventListener('click', send);
    document.getElementById('clearBtn').addEventListener('click', () => {
      document.getElementById('chat').innerHTML = '';
      document.getElementById('sessionId').value = '';
    });
    document.querySelectorAll('.quick').forEach(btn => {
      btn.addEventListener('click', () => {
        document.getElementById('inputText').value = btn.dataset.text || '';
      });
    });
    loadUsers();
  </script>
</body>
</html>
""".strip()
