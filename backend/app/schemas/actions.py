from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

ACTION_REGISTER_INCIDENT = "register_machinery_incident"


class ActionStatus(StrEnum):
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


def action_status_of(action_result: dict[str, Any] | None) -> ActionStatus | None:
    """Map the graph's action result to the public action_status (None = no action)."""
    if not action_result:
        return None
    mapping = {"executed": ActionStatus.APPLIED, "rejected": ActionStatus.REJECTED}
    return mapping.get(action_result.get("status", ""))


class MachineryIncidentRequest(BaseModel):
    """What the LLM extracts from the user request (validated before any approval)."""

    machine: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    severity: Literal["low", "medium", "high"]


class MachineryIncident(BaseModel):
    """DEMO record persisted in Redis once a human approves the action."""

    incident_id: str
    job_id: str
    establishment_id: str
    machine: str
    description: str
    severity: Literal["low", "medium", "high"]
    created_at: datetime
