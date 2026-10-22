from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core.errors import ActionRejectedFailure
from app.graph.builder import graph_config
from app.graph.state import new_state
from app.observability.tracing import job_metadata
from app.schemas.actions import ActionStatus, action_status_of
from app.schemas.tasks import Job
from app.services.task_worker import JobOutcome, Processor


def _degraded_result(values: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """DONE result for an applied action whose summary step failed (no internal details)."""
    return {
        "answer": None,
        "agents_consulted": list(values.get("agent_outputs", {})),
        "sources": values.get("context", {}).get("sources", []),
        "action": action,
        "action_status": ActionStatus.APPLIED,
        "warning": (
            "The approved action was applied, but the final summary could not be generated."
        ),
    }


def make_graph_processor(graph: CompiledStateGraph, max_steps: int) -> Processor:
    """Build the worker processor that runs (or resumes) a job through the graph.

    thread_id == job_id isolates each job's checkpoints. A job with a recorded human
    approval is resumed with Command(resume=...) instead of starting a new run.
    """

    async def process(job: Job) -> JobOutcome:
        config = {
            **graph_config(max_steps),
            "configurable": {"thread_id": job.job_id},
            "run_name": "agroops.graph",
            "metadata": job_metadata(job),
            "tags": ["agroops"],
        }
        if job.approval is not None and job.pending_action is not None:
            graph_input: object = Command(
                resume={"approve": job.approval.approve, "reason": job.approval.reason}
            )
        else:
            graph_input = new_state(job.job_id, job.user_input, job.establishment_id)
        try:
            final = await graph.ainvoke(graph_input, config=config)
        except Exception as exc:
            # Action outcome and summary generation are separate concerns: if the
            # human-approved action was APPLIED, a later failure (e.g. the LLM writing
            # the summary) must not turn the job into FAILED.
            if job.approval is not None:
                snapshot = await graph.aget_state(config)
                values = snapshot.values or {}
                action = values.get("action_result")
                if action_status_of(action) == ActionStatus.APPLIED:
                    return JobOutcome(result=_degraded_result(values, action))
                if action:  # rejected: nothing changed, but keep the failure visible
                    raise ActionRejectedFailure(f"{type(exc).__name__}: {exc}") from exc
            raise

        interrupts = final.get("__interrupt__")
        if interrupts:
            return JobOutcome(
                pending_action=dict(interrupts[0].value),
                trace_summary={
                    "agents_consulted": list(final.get("agent_outputs") or {}),
                    "sources_count": len((final.get("context") or {}).get("sources") or []),
                },
            )
        if final.get("error"):
            raise RuntimeError(final["error"])
        if not final.get("result"):
            raise RuntimeError("Graph finished without producing a result")
        return JobOutcome(result=final["result"])

    return process
