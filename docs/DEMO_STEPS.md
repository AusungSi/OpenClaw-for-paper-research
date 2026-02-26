# MemoMate 开发演示步骤

本文件用于本地开发阶段快速验证“新增 -> 确认 -> 查询 -> 删除”闭环。

## 1. 启动

```powershell
.\scripts\start_all.ps1
```

或一键“先测再启”：

```powershell
.\scripts\one_click_start_and_test.ps1
```

确认后端日志包含：

- `Uvicorn running on http://0.0.0.0:8000`
- `scheduler started`

## 2. 健康检查

```powershell
curl http://127.0.0.1:8000/api/v1/health
```

预期响应包含以下字段：

- `db_ok`
- `ollama_ok`
- `scheduler_ok`
- `wecom_send_ok`
- `wecom_last_error`
- `webhook_dedup_ok`
- `intent_provider_ok`
- `reply_provider_ok`
- `asr_provider_ok`

可选检查能力映射：

```powershell
curl http://127.0.0.1:8000/api/v1/capabilities
```

## 3. 微信文本流验证

在企业微信里向应用依次发送：

1. `明天早上9点提醒我开会`
2. `确认`
3. `查询`
4. `删除 开会`
5. `确认`

预期结果：

- 收到口语化确认消息
- 收到“安排好了”类成功消息
- 查询能返回最近待提醒列表
- 删除流程能完成并确认

## 4. 重复回调幂等验证

观察后端日志，若企业微信同一消息重试，应该出现：

- `duplicate_message_ignored category=dedup ...`

且不会重复创建业务动作。

## 5. 微信语音流验证

在企业微信里发送一段语音（60 秒以内）：

预期结果：

- 服务端能识别语音并进入确认流程
- 回复文案口语化，且包含关键事实（时间/内容）
- 回复“确认”后可创建提醒

## 6. 本地无微信情况下的快速演示

```powershell
C:\Users\lyt\anaconda3\envs\memomate\python.exe .\scripts\smoke_intent_flow.py
```

会输出完整的模拟回复序列，便于离线演示文案风格和流程正确性。
