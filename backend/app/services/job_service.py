import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app.schemas.tasks import (
    DEFAULT_ESTABLISHMENT_ID,
    ApprovalDecision,
    ApprovalRequest,
    Job,
    JobStatus,
)

QUEUE_KEY = "agroops:jobs:queue"
JOB_KEY_PATTERN = "agroops:job:*"
APPROVAL_TX_RETRIES = 5
ORPHANED_JOB_ERROR = (
    "INTERRUPTED: el servicio se reinició mientras el job estaba en ejecución. "
    "No se volvió a ejecutar; enviá la consulta de nuevo si hace falta."
)


class JobNotFoundError(Exception):
    pass


class ApprovalConflictError(Exception):
    """The job is not waiting for approval, or it was already decided."""


def _job_key(job_id: str) -> str:
    return f"agroops:job:{job_id}"


class JobService:
    """Persists jobs in Redis and exposes a simple FIFO queue of job ids."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def create_job(
        self, user_input: str, establishment_id: str = DEFAULT_ESTABLISHMENT_ID
    ) -> Job:
        now = datetime.now(UTC)
        job = Job(
            job_id=uuid.uuid4().hex,
            status=JobStatus.PENDING,
            user_input=user_input,
            establishment_id=establishment_id,
            created_at=now,
            updated_at=now,
        )
        await self._save(job)
        await self._redis.rpush(QUEUE_KEY, job.job_id)
        return job

    async def get_job(self, job_id: str) -> Job | None:
        raw = await self._redis.get(_job_key(job_id))
        return Job.model_validate_json(raw) if raw else None

    async def dequeue(self, timeout: int = 1) -> str | None:
        """Block (without blocking the event loop) until a job id is available."""
        item = await self._redis.blpop([QUEUE_KEY], timeout=timeout)
        return item[1] if item else None

    async def mark_running(self, job_id: str) -> Job | None:
        return await self._update(job_id, status=JobStatus.RUNNING)

    async def mark_done(self, job_id: str, result: dict[str, Any]) -> Job | None:
        return await self._update(
            job_id, status=JobStatus.DONE, result=result, pending_action=None
        )

    async def mark_failed(self, job_id: str, error: str) -> Job | None:
        return await self._update(
            job_id, status=JobStatus.FAILED, error=error, pending_action=None
        )

    async def mark_waiting_approval(
        self, job_id: str, pending_action: dict[str, Any]
    ) -> Job | None:
        return await self._update(
            job_id,
            status=JobStatus.WAITING_APPROVAL,
            pending_action=pending_action,
            approval=None,
        )

    async def submit_approval(self, job_id: str, request: ApprovalRequest) -> Job:
        """Record a human decision and enqueue the job to resume, ATOMICALLY.

        The status check, the job update and the enqueue happen in one Redis
        WATCH/MULTI/EXEC transaction: there is no window where the decision is
        recorded but the job is not queued (or vice versa), and concurrent or
        repeated requests see the job already PENDING and get a conflict.
        """
        key = _job_key(job_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            for _ in range(APPROVAL_TX_RETRIES):
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if not raw:
                        raise JobNotFoundError(job_id)
                    job = Job.model_validate_json(raw)
                    if job.status is not JobStatus.WAITING_APPROVAL or not job.pending_action:
                        raise ApprovalConflictError(
                            f"Job is {job.status.value}; it is not waiting for approval."
                        )
                    updated = job.model_copy(
                        update={
                            "status": JobStatus.PENDING,
                            "approval": ApprovalDecision(
                                approve=request.approve,
                                reason=request.reason,
                                decided_at=datetime.now(UTC),
                            ),
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    pipe.rpush(QUEUE_KEY, job_id)
                    await pipe.execute()
                    return updated
                except WatchError:
                    continue  # job changed concurrently: re-read and re-check
        raise ApprovalConflictError("The job is being modified concurrently; retry.")

    async def iter_jobs(self) -> AsyncIterator[Job]:
        async for key in self._redis.scan_iter(match=JOB_KEY_PATTERN):
            raw = await self._redis.get(key)
            if raw:
                yield Job.model_validate_json(raw)

    async def enqueue_pending_if_missing(self, job_id: str) -> bool:
        """Re-enqueue a PENDING job that is not in the queue (lost by a crash)."""
        queued = await self._redis.lrange(QUEUE_KEY, 0, -1)
        if job_id in queued:
            return False
        await self._redis.rpush(QUEUE_KEY, job_id)
        return True

    async def requeue_for_resume(self, job_id: str) -> bool:
        """RUNNING (interrupted resume) -> PENDING + enqueue, in one transaction."""
        key = _job_key(job_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            raw = await pipe.get(key)
            if not raw:
                return False
            job = Job.model_validate_json(raw)
            if job.status is not JobStatus.RUNNING:
                return False
            updated = job.model_copy(
                update={"status": JobStatus.PENDING, "updated_at": datetime.now(UTC)}
            )
            pipe.multi()
            pipe.set(key, updated.model_dump_json())
            pipe.rpush(QUEUE_KEY, job_id)
            await pipe.execute()
            return True

    async def fail_orphaned_running_jobs(self) -> list[str]:
        """Mark jobs left RUNNING by a previous process as FAILED (never re-run).

        Call once at startup, before the worker starts: a single worker means any
        RUNNING job at that point was interrupted by a crash/restart.
        """
        recovered: list[str] = []
        async for key in self._redis.scan_iter(match=JOB_KEY_PATTERN):
            raw = await self._redis.get(key)
            if not raw:
                continue
            job = Job.model_validate_json(raw)
            if job.status is JobStatus.RUNNING:
                await self.mark_failed(job.job_id, ORPHANED_JOB_ERROR)
                recovered.append(job.job_id)
        return recovered

    async def _update(self, job_id: str, **changes: Any) -> Job | None:
        job = await self.get_job(job_id)
        if job is None:
            return None
        job = job.model_copy(update={**changes, "updated_at": datetime.now(UTC)})
        await self._save(job)
        return job

    async def _save(self, job: Job) -> None:
        await self._redis.set(_job_key(job.job_id), job.model_dump_json())
