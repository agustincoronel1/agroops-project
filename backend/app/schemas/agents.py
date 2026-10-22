"""Contracts at the boundaries of the agents: what they receive and what they return.

Agents are graph nodes, not tools: their inputs come from the graph state and their outputs
go back into it (and into the job result). Each boundary is validated with these models.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.actions import ActionStatus

ESTABLISHMENT_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
RetrievalMode = Literal["vector", "keyword", "hybrid"]


class AgentInput(BaseModel):
    """Input every agent node validates before doing any work."""

    user_input: str = Field(min_length=1, max_length=5000)
    establishment_id: str = Field(pattern=ESTABLISHMENT_PATTERN)


class SourceRef(BaseModel):
    """Verifiable reference to the chunk that supports an answer."""

    source: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    page: int | None = None
    distance: float | None = None  # None for keyword-only hits (no vector distance)
    retrieval: RetrievalMode = "vector"


class OperationsAnswer(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    answer: str = Field(min_length=1, max_length=4000)


class JobResult(BaseModel):
    """Final result of a job, as returned by GET /tasks/{id}."""

    model_config = ConfigDict(str_strip_whitespace=True)

    answer: str | None = Field(default=None, min_length=1, max_length=8000)
    agents_consulted: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    action: dict[str, Any] | None = None
    action_status: ActionStatus | None = None
    warning: str | None = None
