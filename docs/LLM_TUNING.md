# MemoMate LLM Tuning Guide

This guide describes how to tune prompt behavior without running the full WeCom flow.

## Goal

- Iterate on intent and reply prompts quickly
- Validate contract stability with focused tests
- Keep business APIs unchanged

## Prompt Files

- Intent prompt: `app/llm/prompts/intent_v1.txt`
  - Version in logs: `intent_v2_minimal`
  - Output contract: `operation`, `content`, `when_text`, `confidence`, `clarification_question`
- Reply prompt: `app/llm/prompts/reply_nlg_v1.txt`
  - Version in logs: `reply_v2_assistant`
  - Output contract: `{"reply": "..."}`

## Fast Playground

### 1) Intent debug

```powershell
python .\scripts\llm_playground.py intent --text "明天早上9点提醒我开会" --timezone "Asia/Shanghai"
```

Output includes:

- raw JSON from LLM
- validated `IntentLite`
- normalized `IntentDraft` used by business code

### 2) Reply debug

```powershell
python .\scripts\llm_playground.py reply --event-type confirmation_prompt --facts-json "{\"operation\":\"add\",\"content\":\"明天开会\",\"run_at_local\":\"2026-02-27T09:00:00+08:00\"}" --required "确认"
```

## One-Command LLM Test Flow

```powershell
.\scripts\test_llm_only.ps1 -IntentText "明天早上9点提醒我开会"
```

This runs:

1. intent playground once
2. focused contract tests
   - `tests/test_llm_intent_contract.py`
   - `tests/test_llm_reply_contract.py`

## Recommended Defaults

- Intent temperature: `0.0`
- Reply temperature: `0.2`
- Intent retries: `2`
- Reply retries: `2`

## Failure Modes to Watch

1. Intent misclassification:
   - Check `intent_stage=lite` logs first
   - Confirm whether error is from prompt output or normalization
2. Reply too generic:
   - Check required keyword validation failures
   - Fallback should trigger automatically
3. Invalid JSON:
   - Ensure prompt still enforces strict JSON
   - Do not add extra commentary in prompts

## Useful Log Fields

- `prompt_version`
- `intent_stage` (`lite` / `normalized`)
- `nlg_event`
