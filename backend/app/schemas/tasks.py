from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_ESTABLISHMENT_ID = "demo"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class TaskCreateRequest(BaseModel):
    user_input: str = Field(min_length=1, max_length=5000)
    establishment_id: str = Field(
        default=DEFAULT_ESTABLISHMENT_ID, pattern=r"^[A-Za-z0-9_-]{1,64}$"
    )


class TaskCreateResponse(BaseModel):
    job_id: str
    status: JobStatus


class Job(BaseModel):
    """Job record persisted in Redis."""

    job_id: str
    status: JobStatus
    user_input: str
    establishment_id: str = DEFAULT_ESTABLISHMENT_ID
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
