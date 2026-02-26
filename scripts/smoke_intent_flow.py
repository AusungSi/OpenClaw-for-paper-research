from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.domain.models import Base
from app.services.confirm_service import ConfirmService
from app.services.intent_service import IntentService
from app.services.message_ingest import MessageIngestService
from app.services.reminder_service import ReminderService
from app.services.reply_renderer import ReplyRenderer


class FailingOllama:
    def generate_json(self, _prompt: str):
        raise RuntimeError("offline for smoke test")


class CaptureWeCom:
    def __init__(self):
        self.messages: list[str] = []

    def send_text(self, user_id: str, content: str):
        self.messages.append(f"{user_id}: {content}")
        return True, None


def run() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    capture = CaptureWeCom()
    renderer = ReplyRenderer()
    ingest = MessageIngestService(
        intent_service=IntentService(ollama_client=FailingOllama()),
        confirm_service=ConfirmService(),
        reminder_service=ReminderService(reply_renderer=renderer),
        wecom_client=capture,
        reply_renderer=renderer,
    )

    with session_local() as db:
        ingest.process_text_message(db, "demo_user", "m1", "<xml/>", "明天早上9点提醒我开会")
        ingest.process_text_message(db, "demo_user", "m2", "<xml/>", "确认")
        ingest.process_text_message(db, "demo_user", "m3", "<xml/>", "查询")
        ingest.process_text_message(db, "demo_user", "m4", "<xml/>", "删除 开会")
        ingest.process_text_message(db, "demo_user", "m5", "<xml/>", "确认")
        db.commit()

    print("=== Smoke Flow Messages ===")
    for idx, message in enumerate(capture.messages, start=1):
        safe_message = message.replace("•", "-")
        print(f"{idx}. {safe_message}")
    print("===========================")


if __name__ == "__main__":
    run()
