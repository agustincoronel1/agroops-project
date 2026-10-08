from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.knowledge import KnowledgeRetriever, NullRetriever, knowledge_node
from app.agents.operations import operations_node
from app.agents.supervisor import FINISH, supervisor_node
from app.graph.state import AgroOpsState
from app.services.llm import LLM


def _route(state: AgroOpsState) -> str:
    return END if state["next_agent"] in (None, FINISH) else state["next_agent"]  # type: ignore[return-value]


def build_graph(
    llm: LLM,
    retriever: KnowledgeRetriever | None = None,
    max_steps: int = 4,
) -> CompiledStateGraph:
    """START -> supervisor -> (operations | knowledge)* -> supervisor -> END."""
    builder = StateGraph(AgroOpsState)
    builder.add_node("supervisor", partial(supervisor_node, llm=llm, max_steps=max_steps))
    builder.add_node("operations", partial(operations_node, llm=llm))
    builder.add_node(
        "knowledge", partial(knowledge_node, llm=llm, retriever=retriever or NullRetriever())
    )
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor", _route, {"operations": "operations", "knowledge": "knowledge", END: END}
    )
    builder.add_edge("operations", "supervisor")
    builder.add_edge("knowledge", "supervisor")
    return builder.compile()


def graph_config(max_steps: int) -> dict[str, Any]:
    # Hard safety net on top of the supervisor's own step limit.
    return {"recursion_limit": 2 * max_steps + 6}
