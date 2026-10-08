from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.incident_action import approval_node, prepare_incident_node
from app.agents.knowledge import KnowledgeRetriever, NullRetriever, knowledge_node
from app.agents.operations import operations_node
from app.agents.supervisor import FINISH, supervisor_node
from app.graph.state import AgroOpsState
from app.services.incident_service import IncidentService
from app.services.llm import LLM


def _route(state: AgroOpsState) -> str:
    return END if state["next_agent"] in (None, FINISH) else state["next_agent"]  # type: ignore[return-value]


def build_graph(
    llm: LLM,
    retriever: KnowledgeRetriever | None = None,
    max_steps: int = 4,
    checkpointer: Any = None,
    incidents: IncidentService | None = None,
) -> CompiledStateGraph:
    """START -> supervisor -> (operations | knowledge | incident+approval)* -> END.

    The incident action (human approval via interrupt) is only wired when both a
    checkpointer and an IncidentService are provided.
    """
    actions_enabled = checkpointer is not None and incidents is not None
    allowed = ("operations", "knowledge") + (("register_incident",) if actions_enabled else ())
    builder = StateGraph(AgroOpsState)
    builder.add_node("supervisor", partial(supervisor_node, llm=llm, max_steps=max_steps, allowed=allowed))
    builder.add_node("operations", partial(operations_node, llm=llm))
    builder.add_node(
        "knowledge", partial(knowledge_node, llm=llm, retriever=retriever or NullRetriever())
    )
    routes: dict[str, str] = {"operations": "operations", "knowledge": "knowledge", END: END}
    if actions_enabled:
        builder.add_node("prepare_incident", partial(prepare_incident_node, llm=llm))
        builder.add_node("approval", partial(approval_node, incidents=incidents))
        builder.add_edge("prepare_incident", "approval")
        builder.add_edge("approval", "supervisor")
        routes["register_incident"] = "prepare_incident"
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", _route, routes)
    builder.add_edge("operations", "supervisor")
    builder.add_edge("knowledge", "supervisor")
    return builder.compile(checkpointer=checkpointer)


def graph_config(max_steps: int) -> dict[str, Any]:
    # Hard safety net on top of the supervisor's own step limit.
    return {"recursion_limit": 2 * max_steps + 6}
