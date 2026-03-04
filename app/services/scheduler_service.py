from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.core.logging import get_logger
from app.infra.db import session_scope
from app.services.research_service import ResearchService
from app.workers.dispatcher import Dispatcher


logger = get_logger("scheduler")


class SchedulerService:
    def __init__(self, dispatcher: Dispatcher, research_service: ResearchService | None = None) -> None:
        self.settings = get_settings()
        self.dispatcher = dispatcher
        self.research_service = research_service
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
        if (
            self.research_service
            and self.settings.research_enabled
            and self.settings.research_queue_mode.strip().lower() == "internal"
        ):
            self.scheduler.add_job(
                self.run_research_cycle,
                "interval",
                seconds=max(5, self.settings.research_job_interval_seconds),
                id="process_research_jobs",
                max_instances=1,
                coalesce=True,
            )
        self.scheduler.start()
        self.started = True
        await self.run_dispatch_cycle()
        if (
            self.research_service
            and self.settings.research_enabled
            and self.settings.research_queue_mode.strip().lower() == "internal"
        ):
            await self.run_research_cycle()
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

    async def run_research_cycle(self) -> int:
        if not self.research_service:
            return 0
        with session_scope() as db:
            return self.research_service.process_one_job(db)
