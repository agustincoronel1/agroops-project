from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.graph.builder import graph_config
from app.graph.state import new_state
from app.schemas.tasks import Job
from app.services.task_worker import Processor


def make_graph_processor(graph: CompiledStateGraph, max_steps: int) -> Processor:
    """Build the worker processor that runs a job through the LangGraph graph."""

    async def process(job: Job) -> dict[str, Any]:
        final = await graph.ainvoke(
            new_state(job.job_id, job.user_input, job.establishment_id), config=graph_config(max_steps)
        )
        if final.get("error"):
            raise RuntimeError(final["error"])
        if not final.get("result"):
            raise RuntimeError("Graph finished without producing a result")
        return final["result"]

    return process
