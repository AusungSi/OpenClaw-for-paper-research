from __future__ import annotations

from datetime import timedelta, timezone

from app.core.timezone import now_utc
from app.domain.enums import ReminderStatus, ScheduleType
from app.domain.models import Reminder, User
from app.workers.dispatcher import Dispatcher


class FakeWeCom:
    def __init__(self):
        self.sent = []

    def send_text(self, user_id: str, content: str):
        self.sent.append((user_id, content))
        return True, None


def test_dispatch_recurring_updates_next_run(db_session):
    now = now_utc()
    user = User(
        wecom_user_id="lisi",
        timezone="Asia/Shanghai",
        locale="zh-CN",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.flush()

    reminder = Reminder(
        user_id=user.id,
        content="报销",
        schedule_type=ScheduleType.RRULE,
        run_at_utc=now - timedelta(days=1),
        rrule="FREQ=DAILY",
        timezone="Asia/Shanghai",
        next_run_utc=now - timedelta(minutes=2),
        status=ReminderStatus.PENDING,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    db_session.add(reminder)
    db_session.flush()

    dispatcher = Dispatcher(wecom_client=FakeWeCom())
    count = dispatcher.dispatch_due(db_session)
    db_session.flush()

    assert count == 1
    assert reminder.next_run_utc is not None
    assert reminder.next_run_utc > now
    assert reminder.status == ReminderStatus.PENDING


def test_dispatch_handles_naive_datetime_inputs(db_session):
    now = now_utc()
    user = User(
        wecom_user_id="zhaoliu",
        timezone="Asia/Shanghai",
        locale="zh-CN",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.flush()

    naive_run = (now - timedelta(days=1)).replace(tzinfo=None)
    naive_next = (now - timedelta(minutes=3)).replace(tzinfo=None)
    reminder = Reminder(
        user_id=user.id,
        content="拿快递",
        schedule_type=ScheduleType.RRULE,
        run_at_utc=naive_run,
        rrule="FREQ=DAILY",
        timezone="Asia/Shanghai",
        next_run_utc=naive_next,
        status=ReminderStatus.PENDING,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    db_session.add(reminder)
    db_session.flush()

    dispatcher = Dispatcher(wecom_client=FakeWeCom())
    count = dispatcher.dispatch_due(db_session)
    db_session.flush()

    assert count == 1
    assert reminder.next_run_utc is not None
    assert reminder.next_run_utc.tzinfo is not None
    assert reminder.next_run_utc.tzinfo.utcoffset(reminder.next_run_utc) == timezone.utc.utcoffset(reminder.next_run_utc)
