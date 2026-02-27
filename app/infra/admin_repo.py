from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from app.domain.enums import DeliveryStatus, PendingActionStatus, ReminderSource, ReminderStatus, VoiceRecordStatus
from app.domain.models import (
    DeliveryLog,
    InboundMessage,
    MobileDevice,
    PendingAction,
    RefreshToken,
    Reminder,
    User,
    VoiceRecord,
)


class AdminRepo:
    def __init__(self, db: Session):
        self.db = db

    def list_users(
        self,
        *,
        q: str | None,
        timezone_name: str | None,
        page: int,
        size: int,
        sort: str,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = []
        if q:
            filters.append(User.wecom_user_id.ilike(f"%{q}%"))
        if timezone_name:
            filters.append(User.timezone == timezone_name)

        order_col = User.updated_at if sort == "updated_at" else User.created_at
        stmt = select(User)
        count_stmt = select(func.count(User.id))
        if filters:
            condition = and_(*filters)
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        stmt = stmt.order_by(desc(order_col)).offset((page - 1) * size).limit(size)

        rows = list(self.db.execute(stmt).scalars().all())
        total = int(self.db.execute(count_stmt).scalar_one())
        cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)

        data: list[dict[str, Any]] = []
        for row in rows:
            pending_reminders = int(
                self.db.execute(
                    select(func.count(Reminder.id)).where(
                        and_(
                            Reminder.user_id == row.id,
                            Reminder.status == ReminderStatus.PENDING,
                        )
                    )
                ).scalar_one()
            )

            failed_deliveries_24h = int(
                self.db.execute(
                    select(func.count(DeliveryLog.id))
                    .join(Reminder, Reminder.id == DeliveryLog.reminder_id)
                    .where(
                        and_(
                            Reminder.user_id == row.id,
                            DeliveryLog.status == DeliveryStatus.FAILED,
                            DeliveryLog.sent_at_utc >= cutoff_24h,
                        )
                    )
                ).scalar_one()
            )

            last_inbound_at = self.db.execute(
                select(func.max(InboundMessage.created_at)).where(InboundMessage.user_id == row.id)
            ).scalar_one()

            last_voice_status = self.db.execute(
                select(VoiceRecord.status)
                .where(VoiceRecord.user_id == row.id)
                .order_by(desc(VoiceRecord.updated_at))
                .limit(1)
            ).scalar_one_or_none()

            data.append(
                {
                    "id": row.id,
                    "wecom_user_id": row.wecom_user_id,
                    "timezone": row.timezone,
                    "locale": row.locale,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "pending_reminders": pending_reminders,
                    "failed_deliveries_24h": failed_deliveries_24h,
                    "last_inbound_at": last_inbound_at,
                    "last_voice_status": last_voice_status.value if last_voice_status else None,
                }
            )
        return data, total

    def get_user(self, user_id: int) -> User | None:
        return self.db.get(User, user_id)

    def get_user_overview(self, user_id: int) -> dict[str, Any] | None:
        user = self.get_user(user_id)
        if not user:
            return None

        now = datetime.now(timezone.utc)
        cutoff_7d = now - timedelta(days=7)

        reminder_counts = self._enum_count_map(
            statuses=[x.value for x in ReminderStatus],
            rows=self.db.execute(
                select(Reminder.status, func.count(Reminder.id))
                .where(Reminder.user_id == user_id)
                .group_by(Reminder.status)
            ).all(),
        )
        pending_action_counts = self._enum_count_map(
            statuses=[x.value for x in PendingActionStatus],
            rows=self.db.execute(
                select(PendingAction.status, func.count(PendingAction.id))
                .where(PendingAction.user_id == user_id)
                .group_by(PendingAction.status)
            ).all(),
        )
        delivery_counts_7d = self._enum_count_map(
            statuses=[x.value for x in DeliveryStatus],
            rows=self.db.execute(
                select(DeliveryLog.status, func.count(DeliveryLog.id))
                .join(Reminder, Reminder.id == DeliveryLog.reminder_id)
                .where(and_(Reminder.user_id == user_id, DeliveryLog.sent_at_utc >= cutoff_7d))
                .group_by(DeliveryLog.status)
            ).all(),
        )

        inbound_counts_7d = {
            "text": 0,
            "voice": 0,
            "other": 0,
        }
        for msg_type, count in self.db.execute(
            select(InboundMessage.msg_type, func.count(InboundMessage.id))
            .where(and_(InboundMessage.user_id == user_id, InboundMessage.created_at >= cutoff_7d))
            .group_by(InboundMessage.msg_type)
        ).all():
            if msg_type in ("text", "voice"):
                inbound_counts_7d[msg_type] = int(count)
            else:
                inbound_counts_7d["other"] += int(count)

        voice_counts_7d = self._enum_count_map(
            statuses=[x.value for x in VoiceRecordStatus],
            rows=self.db.execute(
                select(VoiceRecord.status, func.count(VoiceRecord.id))
                .where(and_(VoiceRecord.user_id == user_id, VoiceRecord.created_at >= cutoff_7d))
                .group_by(VoiceRecord.status)
            ).all(),
        )

        devices = list(
            self.db.execute(
                select(MobileDevice).where(MobileDevice.user_id == user_id).order_by(desc(MobileDevice.updated_at))
            ).scalars()
        )

        token_total = int(
            self.db.execute(select(func.count(RefreshToken.id)).where(RefreshToken.user_id == user_id)).scalar_one()
        )
        token_active = int(
            self.db.execute(
                select(func.count(RefreshToken.id)).where(
                    and_(
                        RefreshToken.user_id == user_id,
                        RefreshToken.revoked_at.is_(None),
                        RefreshToken.expires_at > now,
                    )
                )
            ).scalar_one()
        )
        token_revoked = int(
            self.db.execute(
                select(func.count(RefreshToken.id)).where(
                    and_(
                        RefreshToken.user_id == user_id,
                        RefreshToken.revoked_at.is_not(None),
                    )
                )
            ).scalar_one()
        )
        token_expired = max(0, token_total - token_active - token_revoked)

        return {
            "user": user,
            "reminder_counts": reminder_counts,
            "pending_action_counts": pending_action_counts,
            "delivery_counts_7d": delivery_counts_7d,
            "inbound_counts_7d": inbound_counts_7d,
            "voice_counts_7d": voice_counts_7d,
            "devices": devices,
            "token_stats": {
                "total": token_total,
                "active": token_active,
                "revoked": token_revoked,
                "expired": token_expired,
            },
        }

    def list_user_reminders(
        self,
        *,
        user_id: int,
        status: str | None,
        source: str | None,
        q: str | None,
        from_utc: datetime | None,
        to_utc: datetime | None,
        page: int,
        size: int,
    ) -> tuple[list[Reminder], int]:
        filters = [Reminder.user_id == user_id]
        if status:
            filters.append(Reminder.status == ReminderStatus(status))
        if source:
            filters.append(Reminder.source == ReminderSource(source))
        if q:
            filters.append(Reminder.content.ilike(f"%{q}%"))
        if from_utc:
            filters.append(Reminder.next_run_utc >= from_utc)
        if to_utc:
            filters.append(Reminder.next_run_utc <= to_utc)

        stmt = select(Reminder).where(and_(*filters)).order_by(Reminder.next_run_utc.asc().nulls_last(), Reminder.id.desc())
        count_stmt = select(func.count(Reminder.id)).where(and_(*filters))

        items = list(self.db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all())
        total = int(self.db.execute(count_stmt).scalar_one())
        return items, total

    def list_user_pending_actions(
        self,
        *,
        user_id: int,
        status: str | None,
        page: int,
        size: int,
    ) -> tuple[list[PendingAction], int]:
        filters = [PendingAction.user_id == user_id]
        if status:
            filters.append(PendingAction.status == PendingActionStatus(status))

        stmt = select(PendingAction).where(and_(*filters)).order_by(desc(PendingAction.created_at))
        count_stmt = select(func.count(PendingAction.id)).where(and_(*filters))
        items = list(self.db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all())
        total = int(self.db.execute(count_stmt).scalar_one())
        return items, total

    def list_user_inbound_messages(
        self,
        *,
        user_id: int,
        msg_type: str | None,
        page: int,
        size: int,
    ) -> tuple[list[InboundMessage], int]:
        filters = [InboundMessage.user_id == user_id]
        if msg_type:
            filters.append(InboundMessage.msg_type == msg_type)

        stmt = select(InboundMessage).where(and_(*filters)).order_by(desc(InboundMessage.created_at))
        count_stmt = select(func.count(InboundMessage.id)).where(and_(*filters))
        items = list(self.db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all())
        total = int(self.db.execute(count_stmt).scalar_one())
        return items, total

    def list_user_voice_records(
        self,
        *,
        user_id: int,
        status: str | None,
        source: str | None,
        page: int,
        size: int,
    ) -> tuple[list[VoiceRecord], int]:
        filters = [VoiceRecord.user_id == user_id]
        if status:
            filters.append(VoiceRecord.status == VoiceRecordStatus(status))
        if source:
            filters.append(VoiceRecord.source == source)

        stmt = select(VoiceRecord).where(and_(*filters)).order_by(desc(VoiceRecord.updated_at))
        count_stmt = select(func.count(VoiceRecord.id)).where(and_(*filters))
        items = list(self.db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all())
        total = int(self.db.execute(count_stmt).scalar_one())
        return items, total

    def list_user_deliveries(
        self,
        *,
        user_id: int,
        status: str | None,
        from_utc: datetime | None,
        to_utc: datetime | None,
        page: int,
        size: int,
    ) -> tuple[list[DeliveryLog], int]:
        filters = [Reminder.user_id == user_id]
        if status:
            filters.append(DeliveryLog.status == DeliveryStatus(status))
        if from_utc:
            filters.append(DeliveryLog.sent_at_utc >= from_utc)
        if to_utc:
            filters.append(DeliveryLog.sent_at_utc <= to_utc)

        stmt = (
            select(DeliveryLog)
            .join(Reminder, Reminder.id == DeliveryLog.reminder_id)
            .where(and_(*filters))
            .order_by(desc(DeliveryLog.sent_at_utc))
        )
        count_stmt = (
            select(func.count(DeliveryLog.id))
            .join(Reminder, Reminder.id == DeliveryLog.reminder_id)
            .where(and_(*filters))
        )
        items = list(self.db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all())
        total = int(self.db.execute(count_stmt).scalar_one())
        return items, total

    def list_user_devices(self, user_id: int) -> list[MobileDevice]:
        return list(
            self.db.execute(
                select(MobileDevice).where(MobileDevice.user_id == user_id).order_by(desc(MobileDevice.updated_at))
            ).scalars()
        )

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        return self.db.get(Reminder, reminder_id)

    def global_reminder_counts(self) -> dict[str, int]:
        counts = {x.value: 0 for x in ReminderStatus}
        rows = self.db.execute(select(Reminder.status, func.count(Reminder.id)).group_by(Reminder.status)).all()
        for status, count in rows:
            key = status.value if hasattr(status, "value") else str(status)
            counts[key] = int(count)
        return counts

    def global_delivery_counts_24h(self) -> dict[str, int]:
        counts = {x.value: 0 for x in DeliveryStatus}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        rows = self.db.execute(
            select(DeliveryLog.status, func.count(DeliveryLog.id))
            .where(DeliveryLog.sent_at_utc >= cutoff)
            .group_by(DeliveryLog.status)
        ).all()
        for status, count in rows:
            key = status.value if hasattr(status, "value") else str(status)
            counts[key] = int(count)
        return counts

    @staticmethod
    def _enum_count_map(*, statuses: list[str], rows: list[tuple[Any, Any]]) -> dict[str, int]:
        out = {key: 0 for key in statuses}
        for status, count in rows:
            key = status.value if hasattr(status, "value") else str(status)
            out[key] = int(count)
        return out
