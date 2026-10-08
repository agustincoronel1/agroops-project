from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis

from app.infrastructure.redis import get_redis
from app.schemas.tasks import Job, TaskCreateRequest, TaskCreateResponse
from app.services.job_service import JobService

router = APIRouter(prefix="/tasks", tags=["Tasks"])


def get_job_service(redis: Annotated[Redis, Depends(get_redis)]) -> JobService:
    return JobService(redis)


JobServiceDep = Annotated[JobService, Depends(get_job_service)]


@router.post(
    "", response_model=TaskCreateResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_task(
    payload: TaskCreateRequest, jobs: JobServiceDep
) -> TaskCreateResponse:
    job = await jobs.create_job(payload.user_input, payload.establishment_id)
    return TaskCreateResponse(job_id=job.job_id, status=job.status)


@router.get("/{job_id}", response_model=Job)
async def get_task(job_id: str, jobs: JobServiceDep) -> Job:
    job = await jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Job {job_id} not found")
    return job
