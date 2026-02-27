from __future__ import annotations

from datetime import datetime, timezone

from app.domain.enums import OperationType
from app.services.reply_renderer import ReplyRenderer


def test_confirmation_prompt_golden():
    renderer = ReplyRenderer()
    text = renderer.confirmation_prompt(
        operation=OperationType.ADD,
        content="明天开会",
        timezone="Asia/Shanghai",
        schedule="one_time",
        run_at_local="2026-02-27T09:00:00+08:00",
        rrule=None,
    )
    expected = "我会在 2026-02-27 09:00（北京时间） 提醒你“明天开会”。如果这样安排对的话，回复“确认”就行。"
    assert text == expected


def test_add_success_golden():
    renderer = ReplyRenderer()
    ts = datetime(2026, 2, 27, 1, 0, tzinfo=timezone.utc)
    text = renderer.add_success("明天开会", ts)
    assert text == "安排好了：明天开会。我会在 2026-02-27 09:00（北京时间） 提醒你。"


def test_query_summary_golden():
    renderer = ReplyRenderer()
    text = renderer.query_summary([(1, "开会", "2026-02-27 09:00（北京时间）")])
    assert text == "你现在有 1 条待提醒：[1] 开会，我会在 2026-02-27 09:00（北京时间） 提醒你。"
