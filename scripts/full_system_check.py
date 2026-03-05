#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.infra.db import SessionLocal  # noqa: E402
from app.infra.repos import UserRepo  # noqa: E402
from app.services.mobile_auth_service import MobileAuthService  # noqa: E402


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    raise SystemExit(2)


def _ok(msg: str) -> None:
    print(f"[OK] {msg}")


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _get_base_url(settings, override: str | None) -> str:
    if override:
        return override.rstrip("/")
    host = settings.app_host or "127.0.0.1"
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{settings.app_port}"


def _requests_json(method: str, url: str, *, headers: dict | None = None, json_body: dict | None = None, timeout: int = 15):
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    text = resp.text
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {text}")
    return resp.json() if text else {}


def _wait_until(fn, *, timeout: int = 120, interval: float = 2.0, desc: str = ""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise RuntimeError(f"timeout waiting for {desc}")


def check_openclaw(settings, strict: bool) -> None:
    if not settings.openclaw_enabled:
        _fail("OPENCLAW_ENABLED=false. No OpenClaw HTTP calls will be made.")
    if not settings.openclaw_gateway_token:
        _fail("OPENCLAW_GATEWAY_TOKEN is empty. HTTP auth will fail.")
    base = settings.openclaw_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.openclaw_gateway_token}"}

    try:
        r = requests.get(f"{base}/health", timeout=5)
        if r.status_code >= 400:
            raise RuntimeError(r.text)
    except Exception as exc:
        _fail(f"OpenClaw /health failed: {exc}")
    _ok("OpenClaw /health reachable")

    try:
        r = requests.get(f"{base}/v1/models", headers=headers, timeout=10)
        if r.status_code >= 400:
            raise RuntimeError(r.text)
    except Exception as exc:
        _fail(f"OpenClaw /v1/models failed: {exc}")
    _ok("OpenClaw /v1/models reachable")

    payload = {
        "model": f"openclaw:{settings.openclaw_agent_id}",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "temperature": 0,
        "max_tokens": 32,
    }
    try:
        r = requests.post(f"{base}/v1/chat/completions", headers={"Content-Type": "application/json", **headers}, json=payload, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(r.text)
        data = r.json()
        if not data.get("choices"):
            raise RuntimeError("empty choices")
    except Exception as exc:
        _fail(f"OpenClaw /v1/chat/completions failed: {exc}")
    _ok("OpenClaw chat completions OK")

    if strict and settings.openclaw_cli_fallback_enabled:
        _fail("OPENCLAW_CLI_FALLBACK_ENABLED=true but strict mode requires no fallback.")


def get_token(settings) -> str:
    db = SessionLocal()
    try:
        user = UserRepo(db).get_or_create("healthcheck-user", timezone_name=settings.default_timezone)
        tokens = MobileAuthService().issue_tokens(db, user.id, device_id="healthcheck")
        db.commit()
        return tokens.access_token
    finally:
        db.close()


def check_backend(base_url: str, *, expect_openclaw: bool) -> dict:
    health = _requests_json("GET", f"{base_url}/api/v1/health")
    if not health.get("db_ok"):
        _fail("db_ok=false")
    if expect_openclaw:
        if health.get("intent_provider_name") != "openclaw":
            _fail(f"intent_provider_name={health.get('intent_provider_name')} (expected openclaw)")
        if not health.get("intent_provider_ok"):
            _fail(f"intent_provider_ok=false: {health.get('intent_last_error')}")
    _ok("Backend /health OK")
    return health


def research_flow(base_url: str, token: str, *, sources: list[str] | None = None) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    topic = f"healthcheck {datetime.utcnow().isoformat()}"
    task = _requests_json("POST", f"{base_url}/api/v1/research/tasks", headers={"Content-Type": "application/json", **headers}, json_body={"topic": topic})
    task_id = task["task_id"]
    _ok(f"created task {task_id}")

    def _directions_ready():
        data = _requests_json("GET", f"{base_url}/api/v1/research/tasks/{task_id}", headers=headers)
        if data.get("directions"):
            return data
        return None

    task = _wait_until(_directions_ready, timeout=180, interval=3, desc="directions planned")
    _ok(f"directions planned: {len(task.get('directions') or [])}")

    direction_index = 1
    start_payload = {"direction_index": direction_index, "sources": sources} if sources else {"direction_index": direction_index}
    start = _requests_json("POST", f"{base_url}/api/v1/research/tasks/{task_id}/explore/start", headers={"Content-Type": "application/json", **headers}, json_body=start_payload)
    round_id = start["round_id"]
    _ok(f"explore start round={round_id}")

    def _round_ready():
        data = _requests_json("GET", f"{base_url}/api/v1/research/tasks/{task_id}/explore/tree", headers=headers)
        node_types = {n.get("type") for n in data.get("nodes", [])}
        if "round" in node_types:
            return data
        return None

    _wait_until(_round_ready, timeout=180, interval=3, desc="round in tree")
    _ok("tree contains round node")

    propose = _requests_json(
        "POST",
        f"{base_url}/api/v1/research/tasks/{task_id}/explore/rounds/{round_id}/propose",
        headers={"Content-Type": "application/json", **headers},
        json_body={"action": "deepen", "feedback_text": "关注评估与可靠性", "candidate_count": 3},
    )
    candidates = propose.get("candidates") or []
    if not candidates:
        _fail("no candidates returned")
    _ok(f"candidates generated: {len(candidates)}")

    candidate_id = candidates[0]["candidate_id"]
    select = _requests_json(
        "POST",
        f"{base_url}/api/v1/research/tasks/{task_id}/explore/rounds/{round_id}/select",
        headers={"Content-Type": "application/json", **headers},
        json_body={"candidate_id": candidate_id},
    )
    child_round_id = select.get("child_round_id")
    _ok(f"child round created: {child_round_id}")

    def _child_round_ready():
        data = _requests_json("GET", f"{base_url}/api/v1/research/tasks/{task_id}/explore/tree", headers=headers)
        if any(n.get("id") == f"round:{child_round_id}" for n in data.get("nodes", [])):
            return data
        return None

    _wait_until(_child_round_ready, timeout=180, interval=3, desc="child round in tree")
    _ok("child round in tree")

    tree_with_papers = _requests_json("GET", f"{base_url}/api/v1/research/tasks/{task_id}/graph?view=tree&include_papers=true", headers=headers)
    paper_nodes = [n for n in tree_with_papers.get("nodes", []) if n.get("type") == "paper"]
    if not paper_nodes:
        _warn("no paper nodes yet (search may still be running). This does not fail the check.")
    else:
        _ok(f"paper nodes visible: {len(paper_nodes)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="MemoMate full system check (no fallback).")
    parser.add_argument("--base-url", default=None, help="Backend base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--sources", default="arxiv", help="Comma separated sources for explore start (default arxiv)")
    parser.add_argument("--strict", action="store_true", help="Fail if any fallback settings are enabled")
    args = parser.parse_args()

    settings = get_settings()
    base_url = _get_base_url(settings, args.base_url)
    sources = [x.strip() for x in (args.sources or "").split(",") if x.strip()]

    print("[INFO] base_url=", base_url)
    check_openclaw(settings, strict=args.strict)
    health = check_backend(base_url, expect_openclaw=True)

    if args.strict:
        if health.get("openclaw_cli_fallback_count", 0) > 0:
            _fail("openclaw_cli_fallback_count > 0 before tests")

    token = get_token(settings)
    research_flow(base_url, token, sources=sources)

    # re-check health metrics
    health2 = check_backend(base_url, expect_openclaw=True)
    if health2.get("openclaw_http_ok", 0) <= health.get("openclaw_http_ok", 0):
        _fail("openclaw_http_ok did not increase; LLM calls may not be using OpenClaw HTTP")
    if health2.get("openclaw_cli_fallback_count", 0) > health.get("openclaw_cli_fallback_count", 0):
        _fail("openclaw_cli_fallback_count increased; fallback occurred")

    _ok("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
