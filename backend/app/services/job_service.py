import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.schemas.tasks import Job, JobStatus

QUEUE_KEY = "agroops:jobs:queue"


def _job_key(job_id: str) -> str:
    return f"agroops:job:{job_id}"


class JobService:
    """Persists jobs in Redis and exposes a simple FIFO queue of job ids."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def create_job(self, user_input: str) -> Job:
        now = datetime.now(UTC)
        job = Job(
            job_id=uuid.uuid4().hex,
            status=JobStatus.PENDING,
            user_input=user_input,
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
        return await self._update(job_id, status=JobStatus.DONE, result=result)

    async def mark_failed(self, job_id: str, error: str) -> Job | None:
        return await self._update(job_id, status=JobStatus.FAILED, error=error)

    async def _update(self, job_id: str, **changes: Any) -> Job | None:
        job = await self.get_job(job_id)
        if job is None:
            return None
        job = job.model_copy(update={**changes, "updated_at": datetime.now(UTC)})
        await self._save(job)
        return job

    async def _save(self, job: Job) -> None:
        await self._redis.set(_job_key(job.job_id), job.model_dump_json())
