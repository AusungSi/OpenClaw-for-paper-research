# MemoMate 演示步骤（提醒 + 本地调研）

## 1) 启动后端与 worker

```powershell
.\scripts\start_backend.ps1
.\scripts\start_research_worker.ps1
```

或：

```powershell
.\scripts\start_all_with_worker.ps1
```

预期日志：

- `Uvicorn running on http://0.0.0.0:8000`
- `research_worker_started`

## 2) 健康检查

```powershell
curl http://127.0.0.1:8000/api/v1/health
```

确认字段：

- `db_ok`
- `scheduler_ok`
- `openclaw_http_ok/openclaw_http_fail`
- `research_jobs_total`
- `research_cache_hit/research_cache_miss`

## 3) 本地前端调研流程（主演示）

打开：

- `http://127.0.0.1:8000/research/ui`

按顺序操作：

1. 创建主题任务（如 `ultrasound report generation hallucination`）
2. 选择方向并 `explore/start`
3. 输入反馈并 `propose`（expand/deepen/pivot/converge）
4. 选择候选方向进入下一轮
5. 查看轮次树图（Topic -> Direction -> Round -> Paper）
6. 触发 citation build（按需）并查看引文扩展图
7. 导出 `md/bib/json`

## 4) API 快速验证（可选）

```powershell
curl -X POST http://127.0.0.1:8000/api/v1/research/tasks -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d "{\"topic\":\"ultrasound report generation\"}"
```

```powershell
curl -X POST http://127.0.0.1:8000/api/v1/research/tasks/<task_id>/explore/start -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d "{\"direction_index\":1}"
```

```powershell
curl http://127.0.0.1:8000/api/v1/research/tasks/<task_id>/explore/tree -H "Authorization: Bearer <token>"
```

## 5) 企业微信验证（轻量模式）

企业微信仅验证以下能力：

1. 提醒创建/确认/查询/删除
2. `调研 状态`
3. 返回本地前端入口链接（复杂调研不在企业微信内执行）

## 6) 回归测试

```powershell
python -m pytest -q
```

期望全部通过。
