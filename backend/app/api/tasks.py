from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis

from app.api.security import require_approval_key
from app.infrastructure.redis import get_redis
from app.schemas.tasks import (
    ApprovalRequest,
    Job,
    TaskCreateRequest,
    TaskCreateResponse,
)
from app.services.job_service import (
    ApprovalConflictError,
    JobNotFoundError,
    JobService,
)

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


@router.post(
    "/{job_id}/approve",
    response_model=TaskCreateResponse,
    dependencies=[Depends(require_approval_key)],
    responses={
        401: {"description": "Invalid or missing X-API-Key"},
        404: {"description": "Job not found"},
        409: {"description": "Job is not waiting for approval / already decided"},
        503: {"description": "APPROVAL_API_KEY not configured, or Redis unavailable"},
    },
)
async def approve_task(
    job_id: str, payload: ApprovalRequest, jobs: JobServiceDep
) -> TaskCreateResponse:
    """Approve or reject the pending critical action of a WAITING_APPROVAL job."""
    try:
        job = await jobs.submit_approval(job_id, payload)
    except JobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Job {job_id} not found") from exc
    except ApprovalConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return TaskCreateResponse(job_id=job.job_id, status=job.status)
