"""Audit extras (all MOCKED): reducers, limits and exception propagation."""

from typing import Any

import pytest
from app.graph.builder import build_graph, graph_config
from app.graph.state import new_state
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph.message import add_messages
from tests.test_graph import FakeLLM


def test_messages_reducer_appends_and_dedups_by_id() -> None:
    a = HumanMessage(content="hola", id="1")
    merged = add_messages([a], [AIMessage(content="resp", id="2")])
    assert [m.id for m in merged] == ["1", "2"]
    replaced = add_messages(merged, [HumanMessage(content="editado", id="1")])
    assert len(replaced) == 2 and replaced[0].content == "editado"


@pytest.mark.anyio
async def test_zero_max_steps_skips_routing_and_synthesizes() -> None:
    llm = FakeLLM([])  # parse() must never be called
    graph = build_graph(llm, max_steps=0)
    final = await graph.ainvoke(new_state("t", "q"), config=graph_config(0))
    assert final["result"]["agents_consulted"] == []
    assert len(llm.complete_calls) == 1


@pytest.mark.anyio
async def test_recursion_limit_is_a_hard_stop() -> None:
    graph = build_graph(FakeLLM(["operations", "knowledge", "FINISH"]))
    with pytest.raises(GraphRecursionError):
        await graph.ainvoke(new_state("t", "q"), config={"recursion_limit": 2})


@pytest.mark.anyio
async def test_agent_exception_propagates() -> None:
    class Boom(FakeLLM):
        async def complete(self, messages: Any) -> str:
            raise RuntimeError("operations exploded")

    graph = build_graph(Boom(["operations"]))
    with pytest.raises(RuntimeError, match="operations exploded"):
        await graph.ainvoke(new_state("t", "q"), config=graph_config(4))


@pytest.mark.anyio
async def test_nodes_do_not_populate_messages_yet() -> None:
    """Documents current behaviour: `messages` is declared but never written."""
    graph = build_graph(FakeLLM(["operations", "FINISH"]))
    final = await graph.ainvoke(new_state("t", "q"), config=graph_config(4))
    assert final["messages"] == []
