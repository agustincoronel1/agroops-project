from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgroOpsState(TypedDict):
    """State shared by the Supervisor graph and its agents.

    `messages` is kept because checkpoints already stored in Redis carry it; removing a
    channel would break resuming those jobs. No node writes to it today.
    """

    task_id: str
    user_input: str
    establishment_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    current_step: str | None
    next_agent: str | None
    steps: int
    agent_outputs: dict[str, str]
    context: dict[str, Any]
    pending_action: dict[str, Any] | None
    action_result: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None


def new_state(
    task_id: str, user_input: str, establishment_id: str = "demo"
) -> AgroOpsState:
    return AgroOpsState(
        task_id=task_id,
        user_input=user_input,
        establishment_id=establishment_id,
        messages=[],
        current_step=None,
        next_agent=None,
        steps=0,
        agent_outputs={},
        context={},
        pending_action=None,
        action_result=None,
        result=None,
        error=None,
    )
