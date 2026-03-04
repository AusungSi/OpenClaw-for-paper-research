# MemoMate Research Architecture

## 1. 目标

调研模块采用“本地前端主导 + 企业微信轻入口”架构：

- 企业微信：提醒与状态通知
- 本地前端：轮次探索、图谱、导出
- 后端：检索、去重、全文解析、图构建、队列执行

## 2. 关键数据模型

- `research_tasks`：任务主体
- `research_directions`：初始方向
- `research_rounds`：用户驱动的探索轮次
- `research_round_candidates`：每轮候选方向
- `research_round_papers`：轮次与论文映射
- `research_paper_fulltext`：全文处理状态与质量
- `research_citation_edges`：引文边
- `research_graph_snapshots`：树图/引文图快照
- `research_jobs`：异步队列（含 worker lease/heartbeat）

## 3. 异步执行模型

1. API/命令写入 `research_jobs`（`queued`）
2. Worker claim 任务并设置 `worker_id + lease_until`
3. 长任务过程中 heartbeat 延长 lease
4. 成功 `done`，失败进入 retry 或 `failed`
5. Worker 崩溃时，lease 过期任务可被其它 worker reclaim

## 4. 引文扩展策略

按需触发，默认 1-hop。

Provider 顺序：

1. `semantic_scholar`
2. `openalex`
3. `crossref`

规则：

- 单源失败不终止任务
- 首个成功源即返回结果（降低延迟）
- 同 `task + paper + source` 使用缓存（TTL）
- DOI 优先归一化，标题归一化兜底

## 5. 轮次探索流程

1. `explore/start`：选方向开启 Round-1
2. `propose`：根据动作与反馈生成候选方向
3. `select`：进入子轮次并触发下一轮检索
4. `explore/tree`：返回 Topic -> Direction -> Round -> Paper 树
5. `citation/build`：按需构建该轮引文图

## 6. 配置要点

- `RESEARCH_QUEUE_MODE=worker|internal`
- `RESEARCH_WORKER_CONCURRENCY`
- `RESEARCH_JOB_LEASE_SECONDS`
- `RESEARCH_CITATION_SOURCES_DEFAULT`
- `RESEARCH_CITATION_CACHE_TTL_SECONDS`
- `RESEARCH_WECOM_LITE_MODE=true`
- `RESEARCH_OCR_ENABLED=false`
