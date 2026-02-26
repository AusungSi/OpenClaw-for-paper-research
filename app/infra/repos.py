from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy import Select, and_, desc, func, select
from sqlalchemy.orm import Session

from app.domain.enums import PendingActionStatus, ReminderStatus, VoiceRecordStatus
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


class UserRepo:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, wecom_user_id: str, timezone_name: str, locale: str = "zh-CN") -> User:
        user = self.db.execute(select(User).where(User.wecom_user_id == wecom_user_id)).scalar_one_or_none()
        if user:
            return user
        now = datetime.now(timezone.utc)
        user = User(
            wecom_user_id=wecom_user_id,
            timezone=timezone_name,
            locale=locale,
            created_at=now,
            updated_at=now,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def get_by_id(self, user_id: int) -> User | None:
        return self.db.get(User, user_id)

    def get_by_wecom_id(self, wecom_user_id: str) -> User | None:
        return self.db.execute(select(User).where(User.wecom_user_id == wecom_user_id)).scalar_one_or_none()


class InboundMessageRepo:
    def __init__(self, db: Session):
        self.db = db

    def exists(self, msg_id: str) -> bool:
        return self.db.execute(select(InboundMessage.id).where(InboundMessage.wecom_msg_id == msg_id)).first() is not None

    def create_if_new(
        self,
        msg_id: str,
        user_id: int,
        msg_type: str,
        raw_xml: str,
        normalized_text: str,
    ) -> bool:
        row = InboundMessage(
            wecom_msg_id=msg_id,
            user_id=user_id,
            msg_type=msg_type,
            raw_xml=raw_xml,
            normalized_text=normalized_text,
            created_at=datetime.now(timezone.utc),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
            return True
        except IntegrityError:
            return False

    def recent_texts(self, user_id: int, limit: int = 5, exclude_msg_id: str | None = None) -> list[str]:
        filters = [
            InboundMessage.user_id == user_id,
            InboundMessage.msg_type == "text",
            InboundMessage.normalized_text != "",
        ]
        if exclude_msg_id:
            filters.append(InboundMessage.wecom_msg_id != exclude_msg_id)
        stmt = (
            select(InboundMessage.normalized_text)
            .where(and_(*filters))
            .order_by(desc(InboundMessage.created_at))
            .limit(limit)
        )
        rows = [row[0].strip() for row in self.db.execute(stmt).all() if row[0] and row[0].strip()]
        rows.reverse()
        return rows


class PendingActionRepo:
    def __init__(self, db: Session):
        self.db = db

    def create(self, row: PendingAction) -> PendingAction:
        self.db.add(row)
        self.db.flush()
        return row

    def latest_pending_for_user(self, user_id: int) -> PendingAction | None:
        now = datetime.now(timezone.utc)
        stmt = (
            select(PendingAction)
            .where(
                and_(
                    PendingAction.user_id == user_id,
                    PendingAction.status == PendingActionStatus.PENDING,
                    PendingAction.expires_at > now,
                )
            )
            .order_by(desc(PendingAction.created_at))
        )
        return self.db.execute(stmt).scalars().first()

    def mark_status(self, row: PendingAction, status: PendingActionStatus) -> PendingAction:
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        self.db.add(row)
        self.db.flush()
        return row


class ReminderRepo:
    def __init__(self, db: Session):
        self.db = db

    def create(self, reminder: Reminder) -> Reminder:
        self.db.add(reminder)
        self.db.flush()
        return reminder

    def get(self, reminder_id: int, user_id: int) -> Reminder | None:
        stmt = select(Reminder).where(and_(Reminder.id == reminder_id, Reminder.user_id == user_id))
        return self.db.execute(stmt).scalar_one_or_none()

    def list(
        self,
        user_id: int,
        status: str | None,
        page: int,
        size: int,
        from_utc: datetime | None,
        to_utc: datetime | None,
    ) -> tuple[list[Reminder], int]:
        filters = [Reminder.user_id == user_id]
        if status:
            filters.append(Reminder.status == ReminderStatus(status))
        if from_utc:
            filters.append(Reminder.next_run_utc >= from_utc)
        if to_utc:
            filters.append(Reminder.next_run_utc <= to_utc)

        stmt: Select[tuple[Reminder]] = (
            select(Reminder)
            .where(and_(*filters))
            .order_by(Reminder.next_run_utc.asc().nulls_last(), Reminder.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        count_stmt = select(func.count(Reminder.id)).where(and_(*filters))

        items = list(self.db.execute(stmt).scalars().all())
        total = int(self.db.execute(count_stmt).scalar_one())
        return items, total

    def find_first_by_keyword(self, user_id: int, keyword: str) -> Reminder | None:
        stmt = (
            select(Reminder)
            .where(
                and_(
                    Reminder.user_id == user_id,
                    Reminder.status == ReminderStatus.PENDING,
                    Reminder.content.ilike(f"%{keyword}%"),
                )
            )
            .order_by(Reminder.next_run_utc.asc().nulls_last())
        )
        return self.db.execute(stmt).scalars().first()

    def due_reminders(self, now: datetime, limit: int = 100) -> list[Reminder]:
        stmt = (
            select(Reminder)
            .where(
                and_(
                    Reminder.status == ReminderStatus.PENDING,
                    Reminder.next_run_utc.is_not(None),
                    Reminder.next_run_utc <= now,
                )
            )
            .order_by(Reminder.next_run_utc.asc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())


class DeliveryRepo:
    def __init__(self, db: Session):
        self.db = db

    def create(self, row: DeliveryLog) -> DeliveryLog:
        self.db.add(row)
        self.db.flush()
        return row


class MobileRepo:
    def __init__(self, db: Session):
        self.db = db

    def create_pair_code(self, user_id: int, pair_code: str, expires_at: datetime) -> MobileDevice:
        row = MobileDevice(
            user_id=user_id,
            pair_code=pair_code,
            pair_code_expires_at=expires_at,
            token_version=1,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def claim_pair_code(self, pair_code: str, device_id: str) -> MobileDevice | None:
        now = datetime.now(timezone.utc)
        stmt = select(MobileDevice).where(
            and_(
                MobileDevice.pair_code == pair_code,
                MobileDevice.pair_code_expires_at.is_not(None),
                MobileDevice.pair_code_expires_at > now,
                MobileDevice.is_active.is_(True),
            )
        )
        row = self.db.execute(stmt).scalar_one_or_none()
        if not row:
            return None

        row.device_id = device_id
        row.pair_code = None
        row.pair_code_expires_at = None
        row.updated_at = now
        row.token_version += 1
        self.db.add(row)
        self.db.flush()
        return row

    def get_device(self, user_id: int, device_id: str) -> MobileDevice | None:
        stmt = select(MobileDevice).where(
            and_(
                MobileDevice.user_id == user_id,
                MobileDevice.device_id == device_id,
                MobileDevice.is_active.is_(True),
            )
        )
        return self.db.execute(stmt).scalar_one_or_none()


class RefreshTokenRepo:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, user_id: int, device_id: str, refresh_token: str, expires_at: datetime) -> RefreshToken:
        row = RefreshToken(
            user_id=user_id,
            device_id=device_id,
            token_hash=self.hash_token(refresh_token),
            expires_at=expires_at,
            revoked_at=None,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def exists_active(self, user_id: int, device_id: str, refresh_token: str) -> bool:
        token_hash = self.hash_token(refresh_token)
        now = datetime.now(timezone.utc)
        stmt = select(RefreshToken.id).where(
            and_(
                RefreshToken.user_id == user_id,
                RefreshToken.device_id == device_id,
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
        )
        return self.db.execute(stmt).first() is not None

    def revoke_all_for_device(self, user_id: int, device_id: str) -> None:
        now = datetime.now(timezone.utc)
        stmt = select(RefreshToken).where(
            and_(
                RefreshToken.user_id == user_id,
                RefreshToken.device_id == device_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
        rows = self.db.execute(stmt).scalars().all()
        for row in rows:
            row.revoked_at = now
            self.db.add(row)


class VoiceRecordRepo:
    def __init__(self, db: Session):
        self.db = db

    def create_or_update(
        self,
        *,
        user_id: int,
        wecom_msg_id: str,
        media_id: str | None,
        audio_format: str | None,
        source: str,
        transcript_text: str | None,
        status: VoiceRecordStatus,
        error: str | None,
        latency_ms: int | None,
    ) -> VoiceRecord:
        now = datetime.now(timezone.utc)
        row = self.db.execute(select(VoiceRecord).where(VoiceRecord.wecom_msg_id == wecom_msg_id)).scalar_one_or_none()
        if row is None:
            row = VoiceRecord(
                user_id=user_id,
                wecom_msg_id=wecom_msg_id,
                media_id=media_id,
                audio_format=audio_format,
                source=source,
                transcript_text=transcript_text,
                status=status,
                error=error,
                latency_ms=latency_ms,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            self.db.flush()
            return row

        row.media_id = media_id
        row.audio_format = audio_format
        row.source = source
        row.transcript_text = transcript_text
        row.status = status
        row.error = error
        row.latency_ms = latency_ms
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row
