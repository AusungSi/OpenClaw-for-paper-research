from __future__ import annotations

from datetime import timedelta

from dateutil import parser as date_parser
from dateutil.rrule import rrulestr
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezone import format_user_time, local_to_utc, now_utc, utc_to_local
from app.domain.enums import OperationType, ReminderSource, ReminderStatus, ScheduleType
from app.domain.models import Reminder
from app.domain.schemas import IntentDraft, ReminderCreateRequest, ReminderResponse, ReminderUpdateRequest
from app.infra.repos import ReminderRepo
from app.services.reply_generation_service import ReplyGenerationService
from app.services.reply_renderer import ReplyRenderer


class ReminderService:
    def __init__(
        self,
        reply_renderer: ReplyRenderer | None = None,
        reply_generation_service: ReplyGenerationService | None = None,
    ) -> None:
        self.settings = get_settings()
        self.reply_renderer = reply_renderer or ReplyRenderer()
        self.reply_generation_service = reply_generation_service

    def create_from_draft(self, db: Session, user_id: int, draft: IntentDraft) -> Reminder:
        if draft.operation != OperationType.ADD:
            raise ValueError("draft operation is not add")
        if not draft.schedule:
            raise ValueError("schedule is required")
        reminder_repo = ReminderRepo(db)
        now = now_utc()

        if draft.schedule == ScheduleType.ONE_TIME:
            if not draft.run_at_local:
                raise ValueError("run_at_local is required")
            run_local = date_parser.parse(draft.run_at_local)
            run_utc = local_to_utc(run_local, draft.timezone)
            next_run_utc = run_utc
            rrule_text = None
        else:
            if not draft.rrule:
                raise ValueError("rrule is required")
            if not draft.run_at_local:
                run_local = utc_to_local(now + timedelta(minutes=1), draft.timezone)
            else:
                run_local = date_parser.parse(draft.run_at_local)
            run_utc = local_to_utc(run_local, draft.timezone)
            self._validate_rrule(draft.rrule, run_utc)
            next_run_utc = run_utc
            rrule_text = draft.rrule

        reminder = Reminder(
            user_id=user_id,
            content=draft.content,
            schedule_type=draft.schedule,
            source=draft.source,
            run_at_utc=run_utc,
            rrule=rrule_text,
            timezone=draft.timezone,
            next_run_utc=next_run_utc,
            status=ReminderStatus.PENDING,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        return reminder_repo.create(reminder)

    def apply_confirmed_draft(self, db: Session, user_id: int, draft: IntentDraft) -> str:
        reminder_repo = ReminderRepo(db)
        now = now_utc()

        if draft.operation == OperationType.ADD:
            reminder = self.create_from_draft(db, user_id, draft)
            fallback = self.reply_renderer.add_success(reminder.content, reminder.next_run_utc, reminder.timezone)
            if not self.reply_generation_service:
                return fallback
            when_text = format_user_time(reminder.next_run_utc, reminder.timezone) if reminder.next_run_utc else "时间待定"
            return self.reply_generation_service.generate_add_success(
                content=reminder.content,
                when_text=when_text,
                timezone=reminder.timezone,
                fallback=fallback,
            )

        if draft.operation == OperationType.DELETE:
            target = reminder_repo.find_first_by_keyword(user_id, draft.content)
            if not target:
                fallback = self.reply_renderer.not_found_for_delete()
                if not self.reply_generation_service:
                    return fallback
                return self.reply_generation_service.generate_not_found_delete(
                    keyword=draft.content,
                    fallback=fallback,
                )
            target.status = ReminderStatus.CANCELED
            target.updated_at = now
            db.add(target)
            db.flush()
            fallback = self.reply_renderer.delete_success(target.content)
            if not self.reply_generation_service:
                return fallback
            return self.reply_generation_service.generate_delete_success(
                content=target.content,
                fallback=fallback,
            )

        if draft.operation == OperationType.UPDATE:
            target = reminder_repo.find_first_by_keyword(user_id, draft.content)
            if not target:
                fallback = self.reply_renderer.not_found_for_update()
                if not self.reply_generation_service:
                    return fallback
                return self.reply_generation_service.generate_not_found_update(
                    keyword=draft.content,
                    fallback=fallback,
                )
            if draft.run_at_local:
                run_local = date_parser.parse(draft.run_at_local)
                run_utc = local_to_utc(run_local, draft.timezone)
                target.run_at_utc = run_utc
                target.next_run_utc = run_utc
            if draft.rrule:
                self._validate_rrule(draft.rrule, target.run_at_utc or now)
                target.rrule = draft.rrule
                target.schedule_type = ScheduleType.RRULE
            target.updated_at = now
            db.add(target)
            db.flush()
            fallback = self.reply_renderer.update_success(target.content)
            if not self.reply_generation_service:
                return fallback
            when_text = format_user_time(target.next_run_utc, target.timezone) if target.next_run_utc else None
            return self.reply_generation_service.generate_update_success(
                content=target.content,
                when_text=when_text,
                fallback=fallback,
            )

        return "查询不需要确认。"

    def list_for_user(
        self,
        db: Session,
        user_id: int,
        status: str | None,
        page: int,
        size: int,
        from_utc,
        to_utc,
    ) -> tuple[list[ReminderResponse], int]:
        reminder_repo = ReminderRepo(db)
        items, total = reminder_repo.list(user_id, status, page, size, from_utc, to_utc)
        result = [
            ReminderResponse(
                id=item.id,
                content=item.content,
                schedule_type=item.schedule_type,
                source=item.source,
                run_at_utc=item.run_at_utc,
                rrule=item.rrule,
                timezone=item.timezone,
                next_run_utc=item.next_run_utc,
                status=item.status.value,
            )
            for item in items
        ]
        return result, total

    def create_from_mobile(self, db: Session, user_id: int, payload: ReminderCreateRequest) -> Reminder:
        draft = IntentDraft(
            operation=OperationType.ADD,
            content=payload.content,
            timezone=payload.timezone,
            source=ReminderSource.MOBILE_API,
            schedule=payload.schedule_type,
            run_at_local=payload.run_at_local,
            rrule=payload.rrule,
            confidence=1.0,
            needs_confirmation=False,
        )
        return self.create_from_draft(db, user_id, draft)

    def update_from_mobile(self, db: Session, user_id: int, reminder_id: int, payload: ReminderUpdateRequest) -> Reminder:
        reminder_repo = ReminderRepo(db)
        row = reminder_repo.get(reminder_id, user_id)
        if not row:
            raise ValueError("reminder not found")
        now = now_utc()
        if payload.content:
            row.content = payload.content
        if payload.timezone:
            row.timezone = payload.timezone
        if payload.run_at_local:
            run_local = date_parser.parse(payload.run_at_local)
            run_utc = local_to_utc(run_local, row.timezone)
            row.run_at_utc = run_utc
            row.next_run_utc = run_utc
        if payload.rrule is not None:
            if payload.rrule:
                self._validate_rrule(payload.rrule, row.run_at_utc or now)
                row.schedule_type = ScheduleType.RRULE
                row.rrule = payload.rrule
            else:
                row.rrule = None
                row.schedule_type = ScheduleType.ONE_TIME
        row.updated_at = now
        db.add(row)
        db.flush()
        return row

    def delete_for_user(self, db: Session, user_id: int, reminder_id: int) -> bool:
        reminder_repo = ReminderRepo(db)
        row = reminder_repo.get(reminder_id, user_id)
        if not row:
            return False
        row.status = ReminderStatus.CANCELED
        row.updated_at = now_utc()
        db.add(row)
        db.flush()
        return True

    def query_summary(self, db: Session, user_id: int) -> str:
        parsed_items = self.query_summary_items(db, user_id)
        if not parsed_items:
            return self.reply_renderer.query_empty()
        return self.reply_renderer.query_summary(parsed_items)

    def query_summary_items(self, db: Session, user_id: int) -> list[tuple[int, str, str]]:
        items, _ = ReminderRepo(db).list(user_id, ReminderStatus.PENDING.value, page=1, size=5, from_utc=None, to_utc=None)
        parsed_items: list[tuple[int, str, str]] = []
        for item in items:
            when = format_user_time(item.next_run_utc, item.timezone) if item.next_run_utc else "时间待定"
            parsed_items.append((item.id, item.content, when))
        return parsed_items

    @staticmethod
    def _validate_rrule(rrule_text: str, dtstart) -> None:
        rule = rrulestr(rrule_text, dtstart=dtstart)
        _ = rule.after(dtstart, inc=True)
