from __future__ import annotations

import os
from time import sleep
from uuid import uuid4

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.infra.db import init_db, session_scope
from app.infra.wecom_client import WeComClient
from app.llm.openclaw_client import OpenClawClient
from app.services.research_service import ResearchService


setup_logging()
logger = get_logger("research_worker")


def run_forever() -> None:
    settings = get_settings()
    init_db()
    worker_id = os.getenv("RESEARCH_WORKER_ID", f"worker-{uuid4().hex[:8]}")
    research_service = ResearchService(
        openclaw_client=OpenClawClient(settings=settings),
        wecom_client=WeComClient(),
    )
    poll_seconds = max(1, int(settings.research_worker_poll_seconds))
    concurrency = max(1, int(settings.research_worker_concurrency))
    logger.info(
        "research_worker_started worker_id=%s queue=%s poll=%s concurrency=%s",
        worker_id,
        settings.research_queue_name,
        poll_seconds,
        concurrency,
    )
    while True:
        processed_total = 0
        try:
            with session_scope() as db:
                for _ in range(concurrency):
                    done = research_service.process_one_job(
                        db,
                        worker_id=worker_id,
                        queue_name=settings.research_queue_name,
                        lease_seconds=settings.research_job_lease_seconds,
                    )
                    processed_total += done
                    if done == 0:
                        break
        except Exception:
            logger.exception("research_worker_cycle_failed")
        if processed_total == 0:
            sleep(poll_seconds)


def main() -> None:
    try:
        run_forever()
    except KeyboardInterrupt:
        logger.info("research_worker_stopped")


if __name__ == "__main__":
    main()
