from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import orjson
from sqlalchemy.exc import IntegrityError
from sqlalchemy import Select, and_, desc, func, select
from sqlalchemy.orm import Session

from app.domain.enums import (
    PendingActionStatus,
    ReminderStatus,
    ResearchJobStatus,
    ResearchJobType,
    ResearchTaskStatus,
    VoiceRecordStatus,
)
from app.domain.models import (
    DeliveryLog,
    InboundMessage,
    MobileDevice,
    PendingAction,
    ResearchDirection,
    ResearchJob,
    ResearchPaper,
    ResearchSession,
    ResearchTask,
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


class ResearchTaskRepo:
    def __init__(self, db: Session):
        self.db = db

    def create(self, row: ResearchTask) -> ResearchTask:
        self.db.add(row)
        self.db.flush()
        return row

    def get_by_task_id(self, task_id: str, user_id: int | None = None) -> ResearchTask | None:
        filters = [ResearchTask.task_id == task_id]
        if user_id is not None:
            filters.append(ResearchTask.user_id == user_id)
        stmt = select(ResearchTask).where(and_(*filters))
        return self.db.execute(stmt).scalar_one_or_none()

    def list_recent(self, user_id: int, limit: int = 10) -> list[ResearchTask]:
        stmt = (
            select(ResearchTask)
            .where(ResearchTask.user_id == user_id)
            .order_by(ResearchTask.created_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def update_status(self, row: ResearchTask, status: ResearchTaskStatus) -> ResearchTask:
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        self.db.add(row)
        self.db.flush()
        return row


class ResearchSessionRepo:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, user_id: int, page_size: int = 10) -> ResearchSession:
        stmt = select(ResearchSession).where(ResearchSession.user_id == user_id)
        row = self.db.execute(stmt).scalar_one_or_none()
        if row:
            return row
        now = datetime.now(timezone.utc)
        row = ResearchSession(
            user_id=user_id,
            active_task_id=None,
            active_direction_index=None,
            page=1,
            page_size=page_size,
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def set_active_task(self, row: ResearchSession, task_id: str) -> ResearchSession:
        row.active_task_id = task_id
        row.active_direction_index = None
        row.page = 1
        row.updated_at = datetime.now(timezone.utc)
        self.db.add(row)
        self.db.flush()
        return row

    def set_pagination(self, row: ResearchSession, *, direction_index: int | None, page: int) -> ResearchSession:
        row.active_direction_index = direction_index
        row.page = max(1, page)
        row.updated_at = datetime.now(timezone.utc)
        self.db.add(row)
        self.db.flush()
        return row


class ResearchDirectionRepo:
    def __init__(self, db: Session):
        self.db = db

    def replace_for_task(self, task: ResearchTask, directions: list[dict]) -> list[ResearchDirection]:
        self.db.query(ResearchPaper).filter(ResearchPaper.task_id == task.id).delete()
        self.db.query(ResearchDirection).filter(ResearchDirection.task_id == task.id).delete()
        now = datetime.now(timezone.utc)
        rows: list[ResearchDirection] = []
        for idx, item in enumerate(directions, start=1):
            row = ResearchDirection(
                task_id=task.id,
                direction_index=idx,
                name=str(item.get("name") or f"Direction {idx}"),
                queries_json=orjson.dumps(item.get("queries") or []).decode("utf-8"),
                exclude_terms_json=orjson.dumps(item.get("exclude_terms") or []).decode("utf-8"),
                papers_count=0,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            rows.append(row)
        self.db.flush()
        return rows

    def list_for_task(self, task_id: int) -> list[ResearchDirection]:
        stmt = select(ResearchDirection).where(ResearchDirection.task_id == task_id).order_by(ResearchDirection.direction_index.asc())
        return list(self.db.execute(stmt).scalars().all())

    def get_by_index(self, task_id: int, direction_index: int) -> ResearchDirection | None:
        stmt = select(ResearchDirection).where(
            and_(ResearchDirection.task_id == task_id, ResearchDirection.direction_index == direction_index)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def update_papers_count(self, row: ResearchDirection, papers_count: int) -> None:
        row.papers_count = papers_count
        row.updated_at = datetime.now(timezone.utc)
        self.db.add(row)
        self.db.flush()


class ResearchPaperRepo:
    def __init__(self, db: Session):
        self.db = db

    def replace_direction_papers(self, direction: ResearchDirection, papers: list[dict]) -> list[ResearchPaper]:
        self.db.query(ResearchPaper).filter(ResearchPaper.direction_id == direction.id).delete()
        existing_rows = (
            self.db.execute(
                select(ResearchPaper).where(
                    and_(
                        ResearchPaper.task_id == direction.task_id,
                        ResearchPaper.direction_id != direction.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_doi = {
            (row.doi or "").strip().lower()
            for row in existing_rows
            if (row.doi or "").strip()
        }
        existing_title = {
            (row.title_norm or "").strip()
            for row in existing_rows
            if (row.title_norm or "").strip()
        }

        now = datetime.now(timezone.utc)
        rows: list[ResearchPaper] = []
        seen_doi: set[str] = set()
        seen_title: set[str] = set()
        for item in papers:
            doi = str(item.get("doi") or "").strip().lower()
            title_norm = str(item.get("title_norm") or "").strip()[:512]
            if doi and (doi in existing_doi or doi in seen_doi):
                continue
            if title_norm and (title_norm in existing_title or title_norm in seen_title):
                continue
            row = ResearchPaper(
                task_id=direction.task_id,
                direction_id=direction.id,
                paper_id=item.get("paper_id"),
                title=str(item.get("title") or "").strip()[:10000],
                title_norm=title_norm,
                authors_json=orjson.dumps(item.get("authors") or []).decode("utf-8"),
                year=item.get("year"),
                venue=item.get("venue"),
                doi=doi or None,
                url=item.get("url"),
                abstract=item.get("abstract"),
                method_summary=str(item.get("method_summary") or ""),
                source=str(item.get("source") or "unknown"),
                relevance_score=item.get("relevance_score"),
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            rows.append(row)
            if doi:
                seen_doi.add(doi)
            if title_norm:
                seen_title.add(title_norm)
        self.db.flush()
        return rows

    def list_for_direction(self, direction_id: int) -> list[ResearchPaper]:
        stmt = select(ResearchPaper).where(ResearchPaper.direction_id == direction_id).order_by(ResearchPaper.id.asc())
        return list(self.db.execute(stmt).scalars().all())

    def list_for_task(self, task_id: int) -> list[ResearchPaper]:
        stmt = select(ResearchPaper).where(ResearchPaper.task_id == task_id).order_by(ResearchPaper.id.asc())
        return list(self.db.execute(stmt).scalars().all())


class ResearchJobRepo:
    def __init__(self, db: Session):
        self.db = db

    def create(self, row: ResearchJob) -> ResearchJob:
        self.db.add(row)
        self.db.flush()
        return row

    def enqueue(self, task_id: int, job_type: ResearchJobType, payload: dict) -> ResearchJob:
        now = datetime.now(timezone.utc)
        row = ResearchJob(
            task_id=task_id,
            job_type=job_type,
            status=ResearchJobStatus.QUEUED,
            payload_json=orjson.dumps(payload).decode("utf-8"),
            error=None,
            attempts=0,
            scheduled_at=now,
            started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def next_queued(self) -> ResearchJob | None:
        stmt = (
            select(ResearchJob)
            .where(
                and_(
                    ResearchJob.status == ResearchJobStatus.QUEUED,
                    ResearchJob.scheduled_at <= datetime.now(timezone.utc),
                )
            )
            .order_by(ResearchJob.created_at.asc())
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def mark_running(self, row: ResearchJob) -> ResearchJob:
        now = datetime.now(timezone.utc)
        row.status = ResearchJobStatus.RUNNING
        row.started_at = now
        row.updated_at = now
        row.attempts += 1
        self.db.add(row)
        self.db.flush()
        return row

    def mark_done(self, row: ResearchJob) -> ResearchJob:
        now = datetime.now(timezone.utc)
        row.status = ResearchJobStatus.DONE
        row.error = None
        row.finished_at = now
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    def mark_failed(self, row: ResearchJob, error: str) -> ResearchJob:
        now = datetime.now(timezone.utc)
        row.status = ResearchJobStatus.FAILED
        row.error = error[:2000]
        row.finished_at = now
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    def mark_retry(self, row: ResearchJob, *, error: str, delay_seconds: int) -> ResearchJob:
        now = datetime.now(timezone.utc)
        row.status = ResearchJobStatus.QUEUED
        row.error = error[:2000]
        row.scheduled_at = now + timedelta(seconds=max(1, delay_seconds))
        row.started_at = None
        row.finished_at = None
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row
