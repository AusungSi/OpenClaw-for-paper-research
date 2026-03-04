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


class ResearchTaskStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    SEARCHING = "searching"
    DONE = "done"
    FAILED = "failed"


class ResearchJobType(str, Enum):
    PLAN = "plan"
    SEARCH = "search"
    FULLTEXT = "fulltext"
    GRAPH_BUILD = "graph_build"


class ResearchJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ResearchPaperFulltextStatus(str, Enum):
    NOT_STARTED = "not_started"
    FETCHING = "fetching"
    FETCHED = "fetched"
    PARSED = "parsed"
    FAILED = "failed"
    NEED_UPLOAD = "need_upload"


class ResearchGraphBuildStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
