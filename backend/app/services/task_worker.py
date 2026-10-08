import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.schemas.tasks import Job
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

Processor = Callable[[Job], Awaitable[dict[str, Any]]]


async def dummy_processor(job: Job) -> dict[str, Any]:
    """Placeholder; will be replaced by the LangGraph execution."""
    await asyncio.sleep(0.5)
    return {"echo": job.user_input}


class TaskWorker:
    """Background asyncio worker consuming job ids from the Redis queue."""

    def __init__(
        self,
        job_service: JobService,
        processor: Processor = dummy_processor,
        poll_timeout: int = 1,
    ) -> None:
        self._jobs = job_service
        self._processor = processor
        self._poll_timeout = poll_timeout
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="task-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                job_id = await self._jobs.dequeue(self._poll_timeout)
                if job_id is not None:
                    await self.process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(1)

    async def process_job(self, job_id: str) -> None:
        job = await self._jobs.mark_running(job_id)
        if job is None:
            logger.warning("Job %s not found, skipping", job_id)
            return
        try:
            result = await self._processor(job)
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            await self._jobs.mark_failed(job_id, f"{type(exc).__name__}: {exc}")
        else:
            await self._jobs.mark_done(job_id, result)
