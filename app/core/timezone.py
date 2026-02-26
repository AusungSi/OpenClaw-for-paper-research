from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


BEIJING_TIMEZONE = "Asia/Shanghai"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_timezone(dt: datetime, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def local_to_utc(dt: datetime, timezone_name: str) -> datetime:
    return ensure_timezone(dt, timezone_name).astimezone(timezone.utc)


def utc_to_local(dt: datetime, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def format_user_time(dt: datetime, timezone_name: str, *, with_seconds: bool = False) -> str:
    local_dt = ensure_timezone(dt, timezone_name)
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    if timezone_name == BEIJING_TIMEZONE:
        return f"{local_dt.strftime(fmt)}（北京时间）"
    return f"{local_dt.strftime(fmt)}（{timezone_name}）"
