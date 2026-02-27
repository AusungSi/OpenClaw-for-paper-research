from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infra.db import session_scope
from app.workers.dispatcher import Dispatcher


logger = get_logger("scheduler")


class SchedulerService:
    def __init__(self, dispatcher: Dispatcher) -> None:
        self.settings = get_settings()
        self.dispatcher = dispatcher
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.scheduler.add_job(
            self.run_dispatch_cycle,
            "interval",
            seconds=self.settings.scheduler_interval_seconds,
            id="dispatch_due_reminders",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        self.started = True
        await self.run_dispatch_cycle()
        logger.info("scheduler started")

    async def shutdown(self) -> None:
        if not self.started:
            return
        self.scheduler.shutdown(wait=False)
        self.started = False
        logger.info("scheduler stopped")

    async def run_dispatch_cycle(self) -> int:
        with session_scope() as db:
            return self.dispatcher.dispatch_due(db)
