from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dateutil.rrule import rrulestr
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezone import now_utc
from app.domain.enums import DeliveryStatus, ReminderStatus, ScheduleType
from app.domain.models import DeliveryLog
from app.infra.repos import DeliveryRepo, ReminderRepo, UserRepo
from app.infra.wecom_client import WeComClient
from app.core.logging import get_logger
from app.services.reply_renderer import ReplyRenderer


logger = get_logger("dispatcher")


class Dispatcher:
    def __init__(self, wecom_client: WeComClient) -> None:
        self.settings = get_settings()
        self.wecom_client = wecom_client
        self.reply_renderer = ReplyRenderer()

    def dispatch_due(self, db: Session) -> int:
        now = self._to_utc(now_utc())
        reminder_repo = ReminderRepo(db)
        due = reminder_repo.due_reminders(now)
        if not due:
            return 0

        delivery_repo = DeliveryRepo(db)
        user_repo = UserRepo(db)
        sent_count = 0

        for reminder in due:
            if not reminder.next_run_utc:
                continue
            user = user_repo.get_by_id(reminder.user_id)
            if not user:
                logger.warning(
                    "dispatch_skipped category=processing reason=user_not_found reminder_id=%s",
                    reminder.id,
                )
                continue

            planned = self._to_utc(reminder.next_run_utc)
            if not planned:
                logger.warning(
                    "dispatch_skipped category=processing reason=invalid_planned_time reminder_id=%s",
                    reminder.id,
                )
                continue
            delay_seconds = max(0, int((now - planned).total_seconds()))
            delay_minutes = delay_seconds // 60 if delay_seconds > 30 else 0
            msg = self.reply_renderer.reminder_due(reminder.content, delay_minutes=delay_minutes)
            if delay_minutes > 0:
                logger.info(
                    "dispatch_compensation category=scheduler reminder_id=%s delay_minutes=%s",
                    reminder.id,
                    delay_minutes,
                )

            ok, error = self.wecom_client.send_text(user.wecom_user_id, msg)
            if ok:
                sent_count += 1
                reminder.last_error = None
                if reminder.schedule_type == ScheduleType.ONE_TIME:
                    reminder.status = ReminderStatus.COMPLETED
                    reminder.next_run_utc = None
                else:
                    reminder.next_run_utc = self._next_run(reminder, now)
                    if not reminder.next_run_utc:
                        reminder.status = ReminderStatus.COMPLETED
            else:
                reminder.last_error = error
                reminder.next_run_utc = now + timedelta(minutes=self.settings.reminder_retry_minutes)
                logger.warning(
                    "dispatch_send_failed category=external reminder_id=%s user_id=%s error=%s",
                    reminder.id,
                    user.id,
                    error,
                )

            reminder.updated_at = now
            db.add(reminder)
            delivery_repo.create(
                DeliveryLog(
                    reminder_id=reminder.id,
                    planned_at_utc=planned,
                    sent_at_utc=now,
                    delay_seconds=delay_seconds,
                    status=DeliveryStatus.SENT if ok else DeliveryStatus.FAILED,
                    error=error,
                )
            )

        return sent_count

    @staticmethod
    def _next_run(reminder, reference_time):
        if not reminder.rrule:
            return None
        dtstart = Dispatcher._to_utc(reminder.run_at_utc) or Dispatcher._to_utc(reminder.next_run_utc) or now_utc()
        ref = Dispatcher._to_utc(reference_time) or now_utc()
        rule = rrulestr(reminder.rrule, dtstart=dtstart)
        next_run = rule.after(ref, inc=False)
        if next_run and next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        return next_run

    @staticmethod
    def _to_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
