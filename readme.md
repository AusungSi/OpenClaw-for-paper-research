# MemoMate

MemoMate 是本地优先的个人助手，包含两条主能力链路：

1. 提醒助手（企业微信入口，轻量交互）
2. 文献调研助手（本地前端主导，支持迭代探索）

---

## 当前定位

- 企业微信：只承载提醒、状态通知、入口链接
- 调研主流程：在本地 Web 前端完成（主题 -> 方向 -> 轮次探索 -> 引文扩展 -> 导出）
- LLM 子任务：统一走 OpenClaw（意图抽取 + 调研规划/摘要）

---

## 已实现能力

### 提醒能力
- 企业微信回调与消息处理
- 意图抽取、确认、提醒落库与定时推送
- 语音转写（企业微信识别优先，本地 ASR 兜底）

### 调研能力（核心）
- 任务创建、方向规划、方向检索、分页查看、导出
- 轮次式探索（用户反馈驱动）：
  - `explore/start`
  - `round propose (expand/deepen/pivot/converge/stop)`
  - `round select -> child round`
- 全文处理：
  - 自动下载 PDF（可用时）
  - PyMuPDF + pdfminer 解析
  - 解析质量分数 + 章节轻结构化
  - 上传 PDF 补齐
- 图谱能力：
  - 轮次树图（Topic -> Direction -> Round -> Paper）
  - 按需 1-hop 引文图
  - 引文源兜底：`semantic_scholar -> openalex -> crossref`
- 独立 research worker：
  - DB 队列 claim / lease / heartbeat / reclaim

---

## 技术栈

- Backend: FastAPI + SQLAlchemy + Alembic
- DB: SQLite
- Scheduler: APScheduler（提醒主用；调研可切 worker/internal）
- LLM: OpenClaw Gateway HTTP + CLI fallback
- PDF parsing: PyMuPDF + pdfminer.six
- Graph: NetworkX + Cytoscape.js

---

## 关键接口

### 调研任务
- `POST /api/v1/research/tasks`
- `GET /api/v1/research/tasks`
- `GET /api/v1/research/tasks/{id}`
- `POST /api/v1/research/tasks/{id}/plan`
- `POST /api/v1/research/tasks/{id}/search`
- `GET /api/v1/research/tasks/{id}/papers`
- `GET /api/v1/research/tasks/{id}/export`

### 轮次探索
- `POST /api/v1/research/tasks/{id}/explore/start`
- `POST /api/v1/research/tasks/{id}/explore/rounds/{round_id}/propose`
- `POST /api/v1/research/tasks/{id}/explore/rounds/{round_id}/select`
- `GET /api/v1/research/tasks/{id}/explore/tree`

### 全文与图谱
- `POST /api/v1/research/tasks/{id}/fulltext/build`
- `POST /api/v1/research/tasks/{id}/fulltext/retry`
- `GET /api/v1/research/tasks/{id}/fulltext/status`
- `POST /api/v1/research/tasks/{id}/papers/{paper_id}/pdf/upload`
- `POST /api/v1/research/tasks/{id}/graph/build`
- `POST /api/v1/research/tasks/{id}/explore/rounds/{round_id}/citation/build`
- `GET /api/v1/research/tasks/{id}/graph?view=tree|citation`
- `GET /api/v1/research/tasks/{id}/graph/snapshots`
- `GET /api/v1/research/tasks/{id}/graph/view`

### 本地前端
- `GET /research/ui`

---

## 快速启动

### 1) 安装

```powershell
pip install -r requirements.txt
```

### 2) 配置

```powershell
copy .env.example .env
```

建议至少配置：

- OpenClaw：`OPENCLAW_ENABLED=true`、`OPENCLAW_BASE_URL`、`OPENCLAW_GATEWAY_TOKEN`
- 调研：`RESEARCH_ENABLED=true`
- 队列：`RESEARCH_QUEUE_MODE=worker`

### 3) 启动后端

```powershell
.\scripts\start_backend.ps1
```

### 4) 启动 research worker（推荐）

```powershell
.\scripts\start_research_worker.ps1
```

或一键拉起：

```powershell
.\scripts\start_all_with_worker.ps1
```

---

## 测试

```powershell
python -m pytest -q
```

---

## 目录

```text
app/
  api/                # wechat/mobile/health/research/research_ui
  core/               # config/logging/timezone
  domain/             # enums/models/schemas
  infra/              # db/repos/wecom
  llm/                # openclaw/ollama/providers
  services/           # reminder + research core services
  workers/            # dispatcher + research_worker
scripts/              # powershell scripts
tests/                # unit/integration tests
docs/                 # demo and notes
```

---

## 说明

- 当前阶段不做 OCR（`RESEARCH_OCR_ENABLED=false`）。
- 企业微信调研命令为轻量模式（`RESEARCH_WECOM_LITE_MODE=true`），复杂操作请在本地前端完成。
