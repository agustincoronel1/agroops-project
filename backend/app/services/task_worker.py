import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from app.core.errors import public_error
from app.observability.tracing import job_output, job_trace
from app.schemas.tasks import Job, JobStatus
from app.services.job_service import JobService

logger = logging.getLogger(__name__)


class JobOutcome(BaseModel):
    """Processor result: either a final result or an action awaiting approval."""

    result: dict[str, Any] | None = None
    pending_action: dict[str, Any] | None = None
    # Trace-only facts for a pending action (agents consulted, sources count). Never sent to the API.
    trace_summary: dict[str, Any] | None = None


Processor = Callable[[Job], Awaitable[dict[str, Any] | JobOutcome]]


class TaskWorker:
    """Background asyncio worker consuming job ids from the Redis queue."""

    def __init__(
        self,
        job_service: JobService,
        processor: Processor,
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
        current = await self._jobs.get_job(job_id)
        if current is not None and current.status is not JobStatus.PENDING:
            # Duplicate/stale queue entry: never run (or re-run) a non-pending job.
            logger.warning("Job %s is %s, not PENDING: skipping", job_id, current.status)
            return
        job = await self._jobs.mark_running(job_id)
        if job is None:
            logger.warning("Job %s not found, skipping", job_id)
            return
        async with job_trace(job) as trace_run:
            try:
                outcome = await self._processor(job)
            except Exception as exc:
                # Full details stay in the server log; the job only gets the public message.
                logger.exception("Job %s failed", job_id)
                await self._jobs.mark_failed(job_id, public_error(exc))
                trace_run.set_status("failed", error_type=type(exc).__name__)
                trace_run.set_outputs(
                    job_output(job_id, JobStatus.FAILED, error_type=type(exc).__name__)
                )
            else:
                if isinstance(outcome, JobOutcome):
                    if outcome.pending_action is not None:
                        await self._jobs.mark_waiting_approval(job_id, outcome.pending_action)
                        trace_run.set_status("waiting_approval")
                        trace_run.set_outputs(
                            job_output(
                                job_id,
                                JobStatus.WAITING_APPROVAL,
                                action_status="PENDING_APPROVAL",
                                **(outcome.trace_summary or {}),
                            )
                        )
                        return
                    outcome = outcome.result or {}
                await self._jobs.mark_done(job_id, outcome)
                trace_run.set_status("done")
                trace_run.set_outputs(_done_output(job_id, outcome))


def _done_output(job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Trace output derived from the same result the API returns (counts/flags only)."""
    return job_output(
        job_id,
        JobStatus.DONE,
        agents_consulted=result.get("agents_consulted") or [],
        sources_count=len(result.get("sources") or []),
        action_status=result.get("action_status"),
        has_answer=bool(result.get("answer")),
    )
