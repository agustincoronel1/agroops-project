from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class TaskCreateRequest(BaseModel):
    user_input: str = Field(min_length=1, max_length=5000)


class TaskCreateResponse(BaseModel):
    job_id: str
    status: JobStatus


class Job(BaseModel):
    """Job record persisted in Redis."""

    job_id: str
    status: JobStatus
    user_input: str
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
