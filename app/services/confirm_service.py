from __future__ import annotations

import uuid
from datetime import timedelta

import orjson

from app.core.config import get_settings
from app.core.timezone import now_utc
from app.domain.enums import OperationType, PendingActionStatus
from app.domain.models import PendingAction
from app.domain.schemas import IntentDraft
from app.infra.repos import PendingActionRepo


class ConfirmService:
    CONFIRM_WORDS = {"确认", "确定", "yes", "ok", "好的", "好", "y"}
    REJECT_WORDS = {"取消", "不", "no", "n", "算了"}

    def __init__(self) -> None:
        self.settings = get_settings()

    def create_pending_action(
        self,
        repo: PendingActionRepo,
        user_id: int,
        action_type: OperationType,
        draft: IntentDraft,
        source_message_id: str,
    ) -> PendingAction:
        created_at = now_utc()
        pending = PendingAction(
            action_id=uuid.uuid4().hex[:16],
            user_id=user_id,
            action_type=action_type,
            draft_json=orjson.dumps(draft.model_dump()).decode("utf-8"),
            source_message_id=source_message_id,
            status=PendingActionStatus.PENDING,
            expires_at=created_at + timedelta(minutes=self.settings.pending_action_minutes),
            created_at=created_at,
            updated_at=created_at,
        )
        return repo.create(pending)

    @staticmethod
    def parse_decision(text: str) -> PendingActionStatus | None:
        normalized = text.strip().lower()
        if normalized in ConfirmService.CONFIRM_WORDS:
            return PendingActionStatus.CONFIRMED
        if normalized in ConfirmService.REJECT_WORDS:
            return PendingActionStatus.REJECTED
        return None
