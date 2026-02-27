from __future__ import annotations

from datetime import datetime

from dateutil import parser as date_parser

from app.core.timezone import format_user_time
from app.domain.enums import OperationType


class ReplyRenderer:
    """Centralized user-facing copy to keep tone and style consistent."""

    @staticmethod
    def empty_message() -> str:
        return "我这边收到了空消息，发一句你想提醒的内容就行。"

    @staticmethod
    def pair_code(code: str, minutes: int) -> str:
        return f"好的，给你一个移动端配对码：{code}，{minutes} 分钟内有效。"

    @staticmethod
    def pending_action_waiting() -> str:
        return "你这边还有一条待确认操作，回复“确认”继续，回复“取消”就不执行。"

    @staticmethod
    def action_canceled() -> str:
        return "没问题，这次操作我已经帮你取消了。"

    @staticmethod
    def clarification(question: str) -> str:
        return question.strip() if question.strip() else "我还差一点信息，你再补充一下具体时间吧。"

    @staticmethod
    def confirmation_prompt(
        operation: OperationType,
        content: str,
        timezone: str,
        schedule: str | None = None,
        run_at_local: str | None = None,
        rrule: str | None = None,
    ) -> str:
        display_time = None
        if run_at_local:
            display_time = run_at_local
            try:
                display_time = format_user_time(date_parser.parse(run_at_local), timezone)
            except (TypeError, ValueError):
                pass

        if operation == OperationType.DELETE:
            return f"我理解你是想删除“{content}”这条提醒。没问题的话，回复“确认”我就处理。"
        if operation == OperationType.UPDATE:
            if display_time:
                return f"我理解你是想把“{content}”调整到 {display_time}。没问题就回复“确认”。"
            return f"我理解你是想修改“{content}”这条提醒。没问题就回复“确认”。"
        if operation == OperationType.ADD:
            if display_time:
                return f"我会在 {display_time} 提醒你“{content}”。如果这样安排对的话，回复“确认”就行。"
            return f"我先记下“{content}”，你回复“确认”后我就创建提醒。"
        if operation == OperationType.QUERY:
            return "你是想查一下现在有哪些提醒，我这就帮你看。"

        return "我理解好了，没问题的话回复“确认”就行。"

    @staticmethod
    def add_success(content: str, next_run_at: datetime | None, timezone: str = "Asia/Shanghai") -> str:
        when = format_user_time(next_run_at, timezone) if next_run_at else "暂时还没算出具体提醒时间"
        return f"安排好了：{content}。我会在 {when} 提醒你。"

    @staticmethod
    def delete_success(content: str) -> str:
        return f"好的，这条提醒已删除：{content}"

    @staticmethod
    def update_success(content: str) -> str:
        return f"收到，这条提醒已经更新：{content}"

    @staticmethod
    def not_found_for_delete() -> str:
        return "我没找到可删除的那条提醒，你可以换个关键词再试试。"

    @staticmethod
    def not_found_for_update() -> str:
        return "我没找到要修改的提醒，你可以再描述得具体一点。"

    @staticmethod
    def query_empty() -> str:
        return "你现在还没有待提醒事项，随时告诉我你想记什么。"

    @staticmethod
    def query_summary(items: list[tuple[int, str, str]]) -> str:
        # tuple: (id, content, when_text)
        if len(items) == 1:
            item_id, content, when_text = items[0]
            return f"你现在有 1 条待提醒：[{item_id}] {content}，我会在 {when_text} 提醒你。"
        lines = [f"你现在有 {len(items)} 条待提醒，我帮你列一下："]
        for item_id, content, when_text in items:
            lines.append(f"[{item_id}] {content}，{when_text}")
        return "\n".join(lines)

    @staticmethod
    def busy_fallback() -> str:
        return "我这边刚才有点忙，没处理成功。你再发一次，我立刻处理。"

    @staticmethod
    def reminder_due(content: str, delay_minutes: int = 0) -> str:
        if delay_minutes > 0:
            return f"来提醒你啦：{content}（晚了约 {delay_minutes} 分钟）"
        return f"来提醒你啦：{content}"
