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
    ResearchActionType,
    ResearchGraphBuildStatus,
    ResearchGraphViewType,
    ResearchJobStatus,
    ResearchJobType,
    ResearchPaperFulltextStatus,
    ResearchRoundStatus,
    ResearchTaskStatus,
    VoiceRecordStatus,
)
from app.domain.models import (
    DeliveryLog,
    InboundMessage,
    MobileDevice,
    PendingAction,
    ResearchCitationFetchCache,
    ResearchDirection,
    ResearchJob,
    ResearchCitationEdge,
    ResearchGraphSnapshot,
    ResearchPaper,
    ResearchSeedPaper,
    ResearchPaperFulltext,
    ResearchRound,
    ResearchRoundCandidate,
    ResearchRoundPaper,
    ResearchSearchCache,
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

    def list_wecom_ids(self, limit: int = 100) -> list[str]:
        stmt = (
            select(User.wecom_user_id)
            .order_by(desc(User.updated_at), desc(User.id))
            .limit(max(1, min(500, int(limit))))
        )
        rows = self.db.execute(stmt).all()
        return [str(row[0]) for row in rows if row and row[0]]


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

    def list_recent_all(self, limit: int = 200) -> list[ResearchTask]:
        stmt = select(ResearchTask).order_by(ResearchTask.created_at.desc()).limit(limit)
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


class ResearchSeedPaperRepo:
    def __init__(self, db: Session):
        self.db = db

    def replace_for_task(self, task_id: int, papers: list[dict]) -> list[ResearchSeedPaper]:
        self.db.query(ResearchSeedPaper).filter(ResearchSeedPaper.task_id == task_id).delete()
        now = datetime.now(timezone.utc)
        rows: list[ResearchSeedPaper] = []
        seen_doi: set[str] = set()
        seen_title: set[str] = set()
        for item in papers:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            doi = str(item.get("doi") or "").strip().lower()
            title_norm = str(item.get("title_norm") or "").strip()[:512]
            if doi and doi in seen_doi:
                continue
            if title_norm and title_norm in seen_title:
                continue
            row = ResearchSeedPaper(
                task_id=task_id,
                paper_id=(str(item.get("paper_id") or "").strip() or None),
                title=title[:10000],
                title_norm=title_norm,
                authors_json=orjson.dumps(item.get("authors") or []).decode("utf-8"),
                year=int(item.get("year")) if str(item.get("year") or "").isdigit() else None,
                venue=(str(item.get("venue") or "").strip()[:255] or None),
                doi=(doi or None),
                url=(str(item.get("url") or "").strip() or None),
                abstract=(str(item.get("abstract") or "").strip() or None),
                source=str(item.get("source") or "unknown")[:64],
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

    def list_for_task(self, task_id: int, *, limit: int | None = None) -> list[ResearchSeedPaper]:
        stmt = select(ResearchSeedPaper).where(ResearchSeedPaper.task_id == task_id).order_by(ResearchSeedPaper.id.asc())
        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def summary_for_task(self, task_id: int) -> dict[str, int]:
        total = self.db.execute(
            select(func.count(ResearchSeedPaper.id)).where(ResearchSeedPaper.task_id == task_id)
        ).scalar_one()
        with_abstract = self.db.execute(
            select(func.count(ResearchSeedPaper.id)).where(
                and_(
                    ResearchSeedPaper.task_id == task_id,
                    ResearchSeedPaper.abstract.is_not(None),
                    ResearchSeedPaper.abstract != "",
                )
            )
        ).scalar_one()
        return {"total": int(total or 0), "with_abstract": int(with_abstract or 0)}


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

    def list_by_ids(self, paper_ids: list[int]) -> list[ResearchPaper]:
        if not paper_ids:
            return []
        stmt = select(ResearchPaper).where(ResearchPaper.id.in_(paper_ids)).order_by(ResearchPaper.id.asc())
        return list(self.db.execute(stmt).scalars().all())

    def upsert_direction_papers(self, direction: ResearchDirection, papers: list[dict]) -> list[ResearchPaper]:
        now = datetime.now(timezone.utc)
        rows: list[ResearchPaper] = []
        for item in papers:
            doi = str(item.get("doi") or "").strip().lower()
            title_norm = str(item.get("title_norm") or "").strip()[:512]
            existing = None
            if doi:
                existing = self.db.execute(
                    select(ResearchPaper).where(
                        and_(
                            ResearchPaper.task_id == direction.task_id,
                            ResearchPaper.doi == doi,
                        )
                    )
                ).scalar_one_or_none()
            if not existing and title_norm:
                existing = self.db.execute(
                    select(ResearchPaper).where(
                        and_(
                            ResearchPaper.task_id == direction.task_id,
                            ResearchPaper.title_norm == title_norm,
                        )
                    )
                ).scalar_one_or_none()
            if existing:
                if existing.direction_id != direction.id:
                    existing.direction_id = direction.id
                if item.get("abstract") and not existing.abstract:
                    existing.abstract = item.get("abstract")
                if item.get("url") and not existing.url:
                    existing.url = item.get("url")
                if item.get("venue") and not existing.venue:
                    existing.venue = item.get("venue")
                if item.get("year") and not existing.year:
                    existing.year = item.get("year")
                if item.get("method_summary"):
                    existing.method_summary = str(item.get("method_summary") or "")
                existing.updated_at = now
                self.db.add(existing)
                rows.append(existing)
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
        self.db.flush()
        return rows

    def get_by_token(self, task_id: int, token: str) -> ResearchPaper | None:
        token_str = (token or "").strip()
        if not token_str:
            return None
        if token_str.isdigit():
            row = self.db.get(ResearchPaper, int(token_str))
            if row and row.task_id == task_id:
                return row
        stmt = select(ResearchPaper).where(
            and_(
                ResearchPaper.task_id == task_id,
                ResearchPaper.paper_id == token_str,
            )
        )
        row = self.db.execute(stmt).scalar_one_or_none()
        if row:
            return row
        stmt = select(ResearchPaper).where(
            and_(
                ResearchPaper.task_id == task_id,
                ResearchPaper.doi == token_str.lower(),
            )
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def mark_saved(self, row: ResearchPaper, *, md_path: str, bib_path: str) -> ResearchPaper:
        now = datetime.now(timezone.utc)
        row.saved = True
        row.saved_path = md_path
        row.saved_bib_path = bib_path
        row.saved_at = now
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    def list_saved_for_task(self, task_id: int, *, limit: int = 200) -> list[ResearchPaper]:
        stmt = (
            select(ResearchPaper)
            .where(and_(ResearchPaper.task_id == task_id, ResearchPaper.saved.is_(True)))
            .order_by(desc(ResearchPaper.saved_at), desc(ResearchPaper.id))
            .limit(max(1, min(1000, int(limit))))
        )
        return list(self.db.execute(stmt).scalars().all())

    def update_key_points(
        self,
        row: ResearchPaper,
        *,
        status: str,
        key_points: str | None = None,
        source: str | None = None,
        error: str | None = None,
    ) -> ResearchPaper:
        now = datetime.now(timezone.utc)
        row.key_points_status = status[:16]
        if key_points is not None:
            row.key_points = key_points
        if source is not None:
            row.key_points_source = source[:32]
        row.key_points_error = error
        row.key_points_updated_at = now
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row


class ResearchPaperFulltextRepo:
    def __init__(self, db: Session):
        self.db = db

    def get(self, task_id: int, paper_id: str) -> ResearchPaperFulltext | None:
        stmt = select(ResearchPaperFulltext).where(
            and_(
                ResearchPaperFulltext.task_id == task_id,
                ResearchPaperFulltext.paper_id == paper_id,
            )
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def upsert(
        self,
        *,
        task_id: int,
        paper_id: str,
        source_url: str | None = None,
        status: str | None = None,
        pdf_path: str | None = None,
        text_path: str | None = None,
        text_chars: int | None = None,
        parser: str | None = None,
        quality_score: float | None = None,
        sections_json: str | None = None,
        fail_reason: str | None = None,
        fetched_at: datetime | None = None,
        parsed_at: datetime | None = None,
    ) -> ResearchPaperFulltext:
        now = datetime.now(timezone.utc)
        row = self.get(task_id, paper_id)
        if row is None:
            row = ResearchPaperFulltext(
                task_id=task_id,
                paper_id=paper_id,
                source_url=source_url,
                pdf_path=pdf_path,
                text_path=text_path,
                text_chars=max(0, int(text_chars or 0)),
                parser=parser,
                quality_score=quality_score,
                sections_json=sections_json or "{}",
                status=ResearchPaperFulltextStatus(status or ResearchPaperFulltextStatus.NOT_STARTED.value),
                fail_reason=fail_reason,
                fetched_at=fetched_at,
                parsed_at=parsed_at,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            self.db.flush()
            return row
        if source_url is not None:
            row.source_url = source_url
        if status is not None:
            row.status = ResearchPaperFulltextStatus(status)
        if pdf_path is not None:
            row.pdf_path = pdf_path
        if text_path is not None:
            row.text_path = text_path
        if text_chars is not None:
            row.text_chars = max(0, int(text_chars))
        if parser is not None:
            row.parser = parser[:32]
        if quality_score is not None:
            row.quality_score = float(quality_score)
        if sections_json is not None:
            row.sections_json = sections_json
        row.fail_reason = fail_reason
        if fetched_at is not None:
            row.fetched_at = fetched_at
        if parsed_at is not None:
            row.parsed_at = parsed_at
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    def list_for_task(self, task_id: int) -> list[ResearchPaperFulltext]:
        stmt = (
            select(ResearchPaperFulltext)
            .where(ResearchPaperFulltext.task_id == task_id)
            .order_by(ResearchPaperFulltext.id.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def summary_for_task(self, task_id: int) -> dict[str, int]:
        rows = self.list_for_task(task_id)
        summary: dict[str, int] = {
            "total": 0,
            "parsed": 0,
            "need_upload": 0,
            "failed": 0,
            "fetching": 0,
            "fetched": 0,
            "not_started": 0,
        }
        for row in rows:
            key = str(row.status.value if hasattr(row.status, "value") else row.status)
            summary["total"] += 1
            summary[key] = summary.get(key, 0) + 1
        return summary


class ResearchCitationEdgeRepo:
    def __init__(self, db: Session):
        self.db = db

    def replace_for_task(self, task_id: int, edges: list[dict]) -> list[ResearchCitationEdge]:
        self.db.query(ResearchCitationEdge).filter(ResearchCitationEdge.task_id == task_id).delete()
        now = datetime.now(timezone.utc)
        rows: list[ResearchCitationEdge] = []
        for item in edges:
            src = str(item.get("source") or "").strip()
            dst = str(item.get("target") or "").strip()
            edge_type = str(item.get("type") or "").strip() or "cites"
            if not src or not dst:
                continue
            row = ResearchCitationEdge(
                task_id=task_id,
                src_paper_id=src[:128],
                dst_paper_id=dst[:128],
                edge_type=edge_type[:32],
                source=str(item.get("source_name") or "semantic_scholar")[:64],
                weight=float(item.get("weight") or 1.0),
                created_at=now,
            )
            self.db.add(row)
            rows.append(row)
        self.db.flush()
        return rows

    def list_for_task(self, task_id: int) -> list[ResearchCitationEdge]:
        stmt = select(ResearchCitationEdge).where(ResearchCitationEdge.task_id == task_id).order_by(ResearchCitationEdge.id.asc())
        return list(self.db.execute(stmt).scalars().all())


class ResearchGraphSnapshotRepo:
    def __init__(self, db: Session):
        self.db = db

    def upsert_snapshot(
        self,
        *,
        task_id: int,
        direction_index: int | None,
        round_id: int | None,
        view_type: str,
        depth: int,
        nodes: list[dict],
        edges: list[dict],
        stats: dict,
        status: str,
    ) -> ResearchGraphSnapshot:
        now = datetime.now(timezone.utc)
        stmt = (
            select(ResearchGraphSnapshot)
            .where(
                and_(
                    ResearchGraphSnapshot.task_id == task_id,
                    ResearchGraphSnapshot.direction_index == direction_index,
                    ResearchGraphSnapshot.round_id == round_id,
                    ResearchGraphSnapshot.view_type == ResearchGraphViewType(view_type),
                )
            )
            .order_by(ResearchGraphSnapshot.updated_at.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).scalar_one_or_none()
        if row is None:
            row = ResearchGraphSnapshot(
                task_id=task_id,
                direction_index=direction_index,
                round_id=round_id,
                view_type=ResearchGraphViewType(view_type),
                depth=max(1, int(depth)),
                nodes_json=orjson.dumps(nodes).decode("utf-8"),
                edges_json=orjson.dumps(edges).decode("utf-8"),
                stats_json=orjson.dumps(stats).decode("utf-8"),
                status=ResearchGraphBuildStatus(status),
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            self.db.flush()
            return row
        row.depth = max(1, int(depth))
        row.round_id = round_id
        row.view_type = ResearchGraphViewType(view_type)
        row.nodes_json = orjson.dumps(nodes).decode("utf-8")
        row.edges_json = orjson.dumps(edges).decode("utf-8")
        row.stats_json = orjson.dumps(stats).decode("utf-8")
        row.status = ResearchGraphBuildStatus(status)
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    def latest_for_task(
        self,
        task_id: int,
        *,
        direction_index: int | None = None,
        round_id: int | None = None,
        view_type: str | None = None,
    ) -> ResearchGraphSnapshot | None:
        filters = [ResearchGraphSnapshot.task_id == task_id]
        if direction_index is not None:
            filters.append(ResearchGraphSnapshot.direction_index == direction_index)
        if round_id is not None:
            filters.append(ResearchGraphSnapshot.round_id == round_id)
        if view_type:
            filters.append(ResearchGraphSnapshot.view_type == ResearchGraphViewType(view_type))
        stmt = (
            select(ResearchGraphSnapshot)
            .where(and_(*filters))
            .order_by(ResearchGraphSnapshot.updated_at.desc(), ResearchGraphSnapshot.id.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_recent(
        self,
        task_id: int,
        *,
        limit: int = 10,
        view_type: str | None = None,
    ) -> list[ResearchGraphSnapshot]:
        filters = [ResearchGraphSnapshot.task_id == task_id]
        if view_type:
            filters.append(ResearchGraphSnapshot.view_type == ResearchGraphViewType(view_type))
        stmt = (
            select(ResearchGraphSnapshot)
            .where(and_(*filters))
            .order_by(ResearchGraphSnapshot.updated_at.desc(), ResearchGraphSnapshot.id.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())


class ResearchRoundRepo:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        task_id: int,
        direction_index: int,
        parent_round_id: int | None,
        depth: int,
        action: str,
        feedback_text: str | None,
        query_terms: list[str],
        status: str = ResearchRoundStatus.QUEUED.value,
    ) -> ResearchRound:
        now = datetime.now(timezone.utc)
        row = ResearchRound(
            task_id=task_id,
            direction_index=direction_index,
            parent_round_id=parent_round_id,
            depth=max(1, int(depth)),
            action=ResearchActionType(action),
            feedback_text=feedback_text,
            query_terms_json=orjson.dumps(query_terms).decode("utf-8"),
            status=ResearchRoundStatus(status),
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def get(self, round_id: int) -> ResearchRound | None:
        return self.db.get(ResearchRound, round_id)

    def list_for_task(
        self,
        task_id: int,
        *,
        direction_index: int | None = None,
    ) -> list[ResearchRound]:
        filters = [ResearchRound.task_id == task_id]
        if direction_index is not None:
            filters.append(ResearchRound.direction_index == direction_index)
        stmt = (
            select(ResearchRound)
            .where(and_(*filters))
            .order_by(ResearchRound.created_at.asc(), ResearchRound.id.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def count_for_task_direction(self, task_id: int, direction_index: int) -> int:
        stmt = select(func.count(ResearchRound.id)).where(
            and_(
                ResearchRound.task_id == task_id,
                ResearchRound.direction_index == direction_index,
            )
        )
        return int(self.db.execute(stmt).scalar() or 0)

    def update_status(self, row: ResearchRound, status: str) -> ResearchRound:
        row.status = ResearchRoundStatus(status)
        row.updated_at = datetime.now(timezone.utc)
        self.db.add(row)
        self.db.flush()
        return row


class ResearchRoundCandidateRepo:
    def __init__(self, db: Session):
        self.db = db

    def replace_for_round(self, round_id: int, candidates: list[dict]) -> list[ResearchRoundCandidate]:
        self.db.query(ResearchRoundCandidate).filter(ResearchRoundCandidate.round_id == round_id).delete()
        now = datetime.now(timezone.utc)
        rows: list[ResearchRoundCandidate] = []
        for idx, item in enumerate(candidates, start=1):
            row = ResearchRoundCandidate(
                round_id=round_id,
                candidate_index=idx,
                name=str(item.get("name") or f"候选方向 {idx}")[:255],
                queries_json=orjson.dumps(item.get("queries") or []).decode("utf-8"),
                reason=str(item.get("reason") or "").strip()[:2000] or None,
                selected=False,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            rows.append(row)
        self.db.flush()
        return rows

    def list_for_round(self, round_id: int) -> list[ResearchRoundCandidate]:
        stmt = (
            select(ResearchRoundCandidate)
            .where(ResearchRoundCandidate.round_id == round_id)
            .order_by(ResearchRoundCandidate.candidate_index.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_index(self, round_id: int, candidate_index: int) -> ResearchRoundCandidate | None:
        stmt = select(ResearchRoundCandidate).where(
            and_(
                ResearchRoundCandidate.round_id == round_id,
                ResearchRoundCandidate.candidate_index == candidate_index,
            )
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_id(self, candidate_id: int) -> ResearchRoundCandidate | None:
        return self.db.get(ResearchRoundCandidate, candidate_id)

    def mark_selected(self, row: ResearchRoundCandidate) -> ResearchRoundCandidate:
        row.selected = True
        row.updated_at = datetime.now(timezone.utc)
        self.db.add(row)
        self.db.flush()
        return row


class ResearchRoundPaperRepo:
    def __init__(self, db: Session):
        self.db = db

    def replace_for_round(
        self,
        *,
        round_id: int,
        rows: list[ResearchPaper],
        role: str = "seed",
    ) -> list[ResearchRoundPaper]:
        self.db.query(ResearchRoundPaper).filter(ResearchRoundPaper.round_id == round_id).delete()
        now = datetime.now(timezone.utc)
        out: list[ResearchRoundPaper] = []
        for idx, paper in enumerate(rows, start=1):
            ref = ResearchRoundPaper(
                round_id=round_id,
                paper_id=paper.id,
                rank=idx,
                role=role[:32],
                created_at=now,
            )
            self.db.add(ref)
            out.append(ref)
        self.db.flush()
        return out

    def list_for_round(self, round_id: int) -> list[ResearchRoundPaper]:
        stmt = select(ResearchRoundPaper).where(ResearchRoundPaper.round_id == round_id).order_by(ResearchRoundPaper.rank.asc())
        return list(self.db.execute(stmt).scalars().all())


class ResearchCitationFetchCacheRepo:
    def __init__(self, db: Session):
        self.db = db

    def get_valid(self, *, task_id: int, paper_key: str, source: str, now: datetime | None = None) -> dict | None:
        current = now or datetime.now(timezone.utc)
        stmt = select(ResearchCitationFetchCache).where(
            and_(
                ResearchCitationFetchCache.task_id == task_id,
                ResearchCitationFetchCache.paper_key == paper_key,
                ResearchCitationFetchCache.source == source,
                ResearchCitationFetchCache.expires_at > current,
            )
        )
        row = self.db.execute(stmt).scalar_one_or_none()
        if not row:
            return None
        try:
            data = orjson.loads(row.payload_json or "{}")
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def upsert(
        self,
        *,
        task_id: int,
        paper_key: str,
        source: str,
        payload: dict,
        ttl_seconds: int,
    ) -> ResearchCitationFetchCache:
        now = datetime.now(timezone.utc)
        stmt = select(ResearchCitationFetchCache).where(
            and_(
                ResearchCitationFetchCache.task_id == task_id,
                ResearchCitationFetchCache.paper_key == paper_key,
                ResearchCitationFetchCache.source == source,
            )
        )
        row = self.db.execute(stmt).scalar_one_or_none()
        expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
        payload_json = orjson.dumps(payload).decode("utf-8")
        if row is None:
            row = ResearchCitationFetchCache(
                task_id=task_id,
                paper_key=paper_key[:128],
                source=source[:64],
                payload_json=payload_json,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            self.db.flush()
            return row
        row.payload_json = payload_json
        row.expires_at = expires_at
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row


class ResearchSearchCacheRepo:
    def __init__(self, db: Session):
        self.db = db

    def get_valid(
        self,
        *,
        task_id: int,
        direction_index: int,
        source: str,
        query_text: str,
        year_from: int | None,
        year_to: int | None,
        top_n: int,
        now: datetime | None = None,
    ) -> list[dict] | None:
        current = now or datetime.now(timezone.utc)
        cache_key = self._cache_key(
            task_id=task_id,
            direction_index=direction_index,
            source=source,
            query_text=query_text,
            year_from=year_from,
            year_to=year_to,
            top_n=top_n,
        )
        stmt = select(ResearchSearchCache).where(
            and_(
                ResearchSearchCache.cache_key == cache_key,
                ResearchSearchCache.expires_at > current,
            )
        )
        row = self.db.execute(stmt).scalar_one_or_none()
        if not row:
            return None
        try:
            data = orjson.loads(row.papers_json or "[]")
        except Exception:
            return None
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)]

    def upsert(
        self,
        *,
        task_id: int,
        direction_index: int,
        source: str,
        query_text: str,
        year_from: int | None,
        year_to: int | None,
        top_n: int,
        papers: list[dict],
        ttl_seconds: int,
    ) -> ResearchSearchCache:
        now = datetime.now(timezone.utc)
        cache_key = self._cache_key(
            task_id=task_id,
            direction_index=direction_index,
            source=source,
            query_text=query_text,
            year_from=year_from,
            year_to=year_to,
            top_n=top_n,
        )
        stmt = select(ResearchSearchCache).where(ResearchSearchCache.cache_key == cache_key)
        row = self.db.execute(stmt).scalar_one_or_none()
        expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
        payload_json = orjson.dumps(papers).decode("utf-8")
        if row is None:
            row = ResearchSearchCache(
                task_id=task_id,
                direction_index=direction_index,
                source=source[:64],
                query_text=query_text[:4000],
                year_from=year_from,
                year_to=year_to,
                top_n=top_n,
                cache_key=cache_key,
                papers_json=payload_json,
                papers_count=len(papers),
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            self.db.add(row)
            self.db.flush()
            return row
        row.task_id = task_id
        row.direction_index = direction_index
        row.source = source[:64]
        row.query_text = query_text[:4000]
        row.year_from = year_from
        row.year_to = year_to
        row.top_n = top_n
        row.papers_json = payload_json
        row.papers_count = len(papers)
        row.expires_at = expires_at
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    @staticmethod
    def _cache_key(
        *,
        task_id: int,
        direction_index: int,
        source: str,
        query_text: str,
        year_from: int | None,
        year_to: int | None,
        top_n: int,
    ) -> str:
        raw = "|".join(
            [
                str(task_id),
                str(direction_index),
                source.strip().lower(),
                query_text.strip().lower(),
                str(year_from or ""),
                str(year_to or ""),
                str(top_n),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ResearchJobRepo:
    def __init__(self, db: Session):
        self.db = db

    def create(self, row: ResearchJob) -> ResearchJob:
        self.db.add(row)
        self.db.flush()
        return row

    def enqueue(self, task_id: int, job_type: ResearchJobType, payload: dict, *, queue_name: str = "research") -> ResearchJob:
        now = datetime.now(timezone.utc)
        row = ResearchJob(
            task_id=task_id,
            job_type=job_type,
            status=ResearchJobStatus.QUEUED,
            payload_json=orjson.dumps(payload).decode("utf-8"),
            error=None,
            attempts=0,
            queue_name=queue_name[:32],
            worker_id=None,
            lease_until=None,
            heartbeat_at=None,
            scheduled_at=now,
            started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def next_queued(self, *, queue_name: str = "research") -> ResearchJob | None:
        stmt = (
            select(ResearchJob)
            .where(
                and_(
                    ResearchJob.status == ResearchJobStatus.QUEUED,
                    ResearchJob.scheduled_at <= datetime.now(timezone.utc),
                    ResearchJob.queue_name == queue_name,
                )
            )
            .order_by(ResearchJob.created_at.asc())
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def claim_next(self, *, worker_id: str, lease_seconds: int, queue_name: str = "research") -> ResearchJob | None:
        now = datetime.now(timezone.utc)
        stmt = (
            select(ResearchJob)
            .where(
                and_(
                    ResearchJob.queue_name == queue_name,
                    ResearchJob.scheduled_at <= now,
                    (
                        (ResearchJob.status == ResearchJobStatus.QUEUED)
                        | (
                            and_(
                                ResearchJob.status == ResearchJobStatus.RUNNING,
                                ResearchJob.lease_until.is_not(None),
                                ResearchJob.lease_until <= now,
                            )
                        )
                    ),
                )
            )
            .order_by(ResearchJob.created_at.asc(), ResearchJob.id.asc())
            .limit(1)
        )
        row = self.db.execute(stmt).scalar_one_or_none()
        if not row:
            return None
        row.status = ResearchJobStatus.RUNNING
        row.worker_id = worker_id[:64]
        row.lease_until = now + timedelta(seconds=max(5, int(lease_seconds)))
        row.heartbeat_at = now
        row.started_at = now
        row.updated_at = now
        row.attempts += 1
        self.db.add(row)
        self.db.flush()
        return row

    def heartbeat(self, row: ResearchJob, *, worker_id: str, lease_seconds: int) -> ResearchJob:
        now = datetime.now(timezone.utc)
        if row.worker_id != worker_id:
            return row
        row.heartbeat_at = now
        row.lease_until = now + timedelta(seconds=max(5, int(lease_seconds)))
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    def latest_for_task(self, task_id: int) -> ResearchJob | None:
        stmt = (
            select(ResearchJob)
            .where(ResearchJob.task_id == task_id)
            .order_by(ResearchJob.created_at.desc(), ResearchJob.id.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def has_pending(self, task_id: int, job_type: ResearchJobType) -> bool:
        stmt = select(ResearchJob.id).where(
            and_(
                ResearchJob.task_id == task_id,
                ResearchJob.job_type == job_type,
                ResearchJob.status.in_([ResearchJobStatus.QUEUED, ResearchJobStatus.RUNNING]),
            )
        )
        return self.db.execute(stmt).first() is not None

    def next_retry_for_task(self, task_id: int) -> ResearchJob | None:
        stmt = (
            select(ResearchJob)
            .where(
                and_(
                    ResearchJob.task_id == task_id,
                    ResearchJob.status == ResearchJobStatus.QUEUED,
                )
            )
            .order_by(ResearchJob.scheduled_at.asc(), ResearchJob.id.asc())
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
        row.worker_id = None
        row.lease_until = None
        row.heartbeat_at = None
        row.finished_at = now
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    def mark_failed(self, row: ResearchJob, error: str) -> ResearchJob:
        now = datetime.now(timezone.utc)
        row.status = ResearchJobStatus.FAILED
        row.error = error[:2000]
        row.worker_id = None
        row.lease_until = None
        row.heartbeat_at = None
        row.finished_at = now
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row

    def mark_retry(self, row: ResearchJob, *, error: str, delay_seconds: int) -> ResearchJob:
        now = datetime.now(timezone.utc)
        row.status = ResearchJobStatus.QUEUED
        row.error = error[:2000]
        row.worker_id = None
        row.lease_until = None
        row.heartbeat_at = None
        row.scheduled_at = now + timedelta(seconds=max(1, delay_seconds))
        row.started_at = None
        row.finished_at = None
        row.updated_at = now
        self.db.add(row)
        self.db.flush()
        return row
