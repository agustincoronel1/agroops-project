from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgroOpsState(TypedDict):
    """Shared state for the future multi-agent LangGraph flow."""

    task_id: str
    user_input: str
    messages: Annotated[list[AnyMessage], add_messages]
    current_step: str | None
    context: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None


def new_state(task_id: str, user_input: str) -> AgroOpsState:
    return AgroOpsState(
        task_id=task_id,
        user_input=user_input,
        messages=[],
        current_step=None,
        context={},
        result=None,
        error=None,
    )
