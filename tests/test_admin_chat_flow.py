from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.admin import router as admin_router
from app.api.mobile import get_current_user_id, router as mobile_router
from app.core.timezone import now_utc
from app.domain.enums import OperationType, ReminderSource, ReminderStatus, ScheduleType
from app.domain.models import Base, Reminder, User
from app.domain.schemas import IntentDraft
from app.infra.db import get_db
from app.services.confirm_service import ConfirmService
from app.services.message_ingest import MessageIngestService
from app.services.reminder_service import ReminderService
from app.workers.dispatcher import Dispatcher


class FakeIntentService:
    def __init__(self) -> None:
        self.last_error: str | None = None

    def parse_intent(self, text: str, timezone: str, context_messages: list[str] | None = None) -> IntentDraft:
        normalized = (text or "").strip().lower()
        if normalized.startswith("query"):
            return IntentDraft(
                operation=OperationType.QUERY,
                content="",
                timezone=timezone,
                schedule=None,
                confidence=0.99,
                needs_confirmation=False,
            )
        if normalized.startswith("delete"):
            content = normalized.replace("delete", "", 1).strip() or "meeting"
            return IntentDraft(
                operation=OperationType.DELETE,
                content=content,
                timezone=timezone,
                schedule=None,
                confidence=0.95,
            )
        if normalized.startswith("update"):
            content = normalized.replace("update", "", 1).strip() or "meeting"
            return IntentDraft(
                operation=OperationType.UPDATE,
                content=content,
                timezone=timezone,
                schedule=ScheduleType.ONE_TIME,
                run_at_local="2026-02-28 10:00:00",
                confidence=0.9,
            )
        return IntentDraft(
            operation=OperationType.ADD,
            content="meeting",
            timezone=timezone,
            schedule=ScheduleType.ONE_TIME,
            run_at_local="2026-02-28 09:00:00",
            confidence=0.98,
        )


class FakeWeCom:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, user_id: str, content: str) -> tuple[bool, str | None]:
        self.sent.append((user_id, content))
        return True, None


class DummyReplyGeneration:
    last_error: str | None = None


def build_admin_chat_client() -> tuple[TestClient, sessionmaker[Session], FakeWeCom, dict[str, int]]:
    engine = create_engine(
        "sqlite+pysqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(mobile_router)

    current_user = {"id": 1}

    def fake_db() -> Generator[Session, None, None]:
        db = session_local()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def fake_current_user_id() -> int:
        return current_user["id"]

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user_id] = fake_current_user_id

    wecom = FakeWeCom()
    reminder_service = ReminderService()
    intent_service = FakeIntentService()
    ingest_service = MessageIngestService(
        intent_service=intent_service,
        confirm_service=ConfirmService(),
        reminder_service=reminder_service,
        wecom_client=wecom,
    )
    app.state.message_ingest_service = ingest_service
    app.state.intent_service = intent_service
    app.state.reply_generation_service = DummyReplyGeneration()
    app.state.reminder_service = reminder_service
    app.state.wecom_client = wecom
    return TestClient(app), session_local, wecom, current_user


def _create_user(session_local: sessionmaker[Session], wecom_user_id: str = "chat_user") -> User:
    db = session_local()
    now = now_utc()
    user = User(
        wecom_user_id=wecom_user_id,
        timezone="Asia/Shanghai",
        locale="zh-CN",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user


def _send_admin_chat(client: TestClient, user_id: int, text: str, session_id: str | None = None) -> dict:
    payload = {"user_id": user_id, "text": text, "session_id": session_id}
    resp = client.post("/api/v1/admin/chat/send", json=payload)
    assert resp.status_code == 200
    return resp.json()


def test_admin_chat_send_basic():
    client, session_local, wecom, current_user = build_admin_chat_client()
    user = _create_user(session_local, "admin_chat_basic")
    current_user["id"] = user.id

    body = _send_admin_chat(client, user.id, "remind me tomorrow morning")

    assert body["pipeline_status"] == "ok"
    assert body["source"] == "admin_chat"
    assert body["replies"]
    assert wecom.sent == []


def test_admin_chat_confirm_flow_sync():
    client, session_local, _wecom, current_user = build_admin_chat_client()
    user = _create_user(session_local, "admin_chat_confirm")
    current_user["id"] = user.id

    first = _send_admin_chat(client, user.id, "remind me tomorrow morning")
    _ = _send_admin_chat(client, user.id, "yes", first["session_id"])

    db = session_local()
    rows = list(db.execute(select(Reminder).where(Reminder.user_id == user.id)).scalars().all())
    db.close()
    assert len(rows) == 1
    assert rows[0].source == ReminderSource.ADMIN_CHAT


def test_admin_chat_reminder_visible_in_mobile_api():
    client, session_local, _wecom, current_user = build_admin_chat_client()
    user = _create_user(session_local, "admin_chat_mobile_visible")
    current_user["id"] = user.id

    first = _send_admin_chat(client, user.id, "remind me tomorrow morning")
    _ = _send_admin_chat(client, user.id, "yes", first["session_id"])

    resp = client.get("/api/v1/reminders")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "admin_chat"


def test_admin_chat_reminder_dispatched_by_scheduler():
    client, session_local, wecom, _current_user = build_admin_chat_client()
    user = _create_user(session_local, "admin_chat_dispatch")

    db = session_local()
    now = now_utc()
    reminder = Reminder(
        user_id=user.id,
        content="dispatch me",
        schedule_type=ScheduleType.ONE_TIME,
        source=ReminderSource.ADMIN_CHAT,
        run_at_utc=now - timedelta(minutes=1),
        rrule=None,
        timezone="Asia/Shanghai",
        next_run_utc=now - timedelta(minutes=1),
        status=ReminderStatus.PENDING,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    db.add(reminder)
    db.commit()

    dispatcher = Dispatcher(wecom_client=wecom)
    sent_count = dispatcher.dispatch_due(db)
    db.commit()
    db.refresh(reminder)
    db.close()

    assert sent_count == 1
    assert len(wecom.sent) == 1
    assert reminder.status == ReminderStatus.COMPLETED


def test_admin_reminder_source_filter():
    client, session_local, _wecom, _current_user = build_admin_chat_client()
    user = _create_user(session_local, "admin_chat_filter")

    db = session_local()
    now = now_utc()
    db.add_all(
        [
            Reminder(
                user_id=user.id,
                content="chat source reminder",
                schedule_type=ScheduleType.ONE_TIME,
                source=ReminderSource.ADMIN_CHAT,
                run_at_utc=now + timedelta(hours=2),
                rrule=None,
                timezone="Asia/Shanghai",
                next_run_utc=now + timedelta(hours=2),
                status=ReminderStatus.PENDING,
                last_error=None,
                created_at=now,
                updated_at=now,
            ),
            Reminder(
                user_id=user.id,
                content="wechat source reminder",
                schedule_type=ScheduleType.ONE_TIME,
                source=ReminderSource.WECHAT,
                run_at_utc=now + timedelta(hours=3),
                rrule=None,
                timezone="Asia/Shanghai",
                next_run_utc=now + timedelta(hours=3),
                status=ReminderStatus.PENDING,
                last_error=None,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    db.commit()
    db.close()

    resp = client.get(f"/api/v1/admin/users/{user.id}/reminders", params={"source": "admin_chat"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["source"] == "admin_chat"


def test_admin_chat_guard():
    client, session_local, _wecom, _current_user = build_admin_chat_client()
    user = _create_user(session_local, "admin_chat_guard")
    payload = {"user_id": user.id, "text": "remind me tomorrow", "session_id": None}

    local_resp = client.post("/api/v1/admin/chat/send", json=payload)
    assert local_resp.status_code == 200

    non_local_client = TestClient(client.app, client=("8.8.8.8", 5555))
    remote_resp = non_local_client.post("/api/v1/admin/chat/send", json=payload)
    assert remote_resp.status_code == 403
