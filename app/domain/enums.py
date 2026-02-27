from __future__ import annotations

from enum import Enum


class OperationType(str, Enum):
    ADD = "add"
    QUERY = "query"
    DELETE = "delete"
    UPDATE = "update"


class ScheduleType(str, Enum):
    ONE_TIME = "one_time"
    RRULE = "rrule"


class ReminderSource(str, Enum):
    WECHAT = "wechat"
    MOBILE_API = "mobile_api"
    ADMIN_CHAT = "admin_chat"


class PendingActionStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class ReminderStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELED = "canceled"


class DeliveryStatus(str, Enum):
    SENT = "sent"
    FAILED = "failed"


class VoiceRecordStatus(str, Enum):
    RECEIVED = "received"
    TRANSCRIBED = "transcribed"
    FAILED = "failed"


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"
