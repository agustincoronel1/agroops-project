import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from redis.exceptions import RedisError

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
        persist_attempts: int = 3,
        persist_backoff: float = 0.5,
    ) -> None:
        self._jobs = job_service
        self._processor = processor
        self._poll_timeout = poll_timeout
        self._persist_attempts = persist_attempts
        self._persist_backoff = persist_backoff
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
        try:
            current = await self._jobs.get_job(job_id)
            if current is not None and current.status is not JobStatus.PENDING:
                # Duplicate/stale queue entry: never run (or re-run) a non-pending job.
                logger.warning("Job %s is %s, not PENDING: skipping", job_id, current.status)
                return
            job = await self._jobs.mark_running(job_id)
        except RedisError:
            # Nothing has run yet, so the job is still PENDING: give it back to the queue.
            logger.exception("Job %s: Redis failed before it started; re-queuing", job_id)
            await self._requeue(job_id)
            return
        if job is None:
            logger.warning("Job %s not found, skipping", job_id)
            return
        async with job_trace(job) as trace_run:
            try:
                outcome = await self._processor(job)
            except Exception as exc:
                # Full details stay in the server log; the job only gets the public message.
                logger.exception("Job %s failed", job_id)
                failure = public_error(exc)
                error_type = type(exc).__name__
                saved = await self._persist(
                    job_id, "failure", lambda: self._jobs.mark_failed(job_id, failure)
                )
                trace_run.set_status("failed" if saved else "persist_failed", error_type=error_type)
                trace_run.set_outputs(job_output(job_id, JobStatus.FAILED, error_type=error_type))
                return
            if isinstance(outcome, JobOutcome) and outcome.pending_action is not None:
                pending = outcome.pending_action
                saved = await self._persist(
                    job_id, "pending approval", lambda: self._jobs.mark_waiting_approval(job_id, pending)
                )
                trace_run.set_status("waiting_approval" if saved else "persist_failed")
                trace_run.set_outputs(
                    job_output(
                        job_id,
                        JobStatus.WAITING_APPROVAL,
                        action_status="PENDING_APPROVAL",
                        **(outcome.trace_summary or {}),
                    )
                )
                return
            result = (outcome.result or {}) if isinstance(outcome, JobOutcome) else outcome
            saved = await self._persist(job_id, "result", lambda: self._jobs.mark_done(job_id, result))
            trace_run.set_status("done" if saved else "persist_failed")
            trace_run.set_outputs(_done_output(job_id, result))

    async def _persist(
        self, job_id: str, what: str, write: Callable[[], Awaitable[Any]]
    ) -> bool:
        """Save a job state, retrying on Redis errors. Returns False if Redis stays down.

        A job that cannot be saved is left as it was. It is never run again: on restart,
        the reconciler finishes it from its checkpoint, or marks it FAILED.
        """
        for attempt in range(1, self._persist_attempts + 1):
            try:
                await write()
                return True
            except RedisError as exc:
                logger.warning(
                    "Job %s: saving %s failed (attempt %d/%d): %s",
                    job_id, what, attempt, self._persist_attempts, type(exc).__name__,
                )
                if attempt < self._persist_attempts:
                    await asyncio.sleep(self._persist_backoff * attempt)
        logger.error(
            "Job %s: %s could not be saved; it stays as last saved and is reconciled on restart",
            job_id, what,
        )
        return False

    async def _requeue(self, job_id: str) -> None:
        try:
            await self._jobs.enqueue_pending_if_missing(job_id)
        except RedisError:
            logger.error("Job %s could not be re-queued; the reconciler will re-queue it on restart", job_id)


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
