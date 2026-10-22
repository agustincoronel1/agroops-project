"""Startup reconciliation of jobs against Redis queue and LangGraph checkpoints.

Runs once before the worker starts (single worker => any RUNNING job is an orphan).
The checkpoint is the source of truth for how far a job got; the critical action itself
is idempotent (deterministic action_id + SET NX), so resuming it again never duplicates.

Outcomes for an orphaned RUNNING job:
  * checkpoint still paused at `approval`, decision recorded -> re-queued to resume
  * checkpoint still paused at `approval`, no decision      -> back to WAITING_APPROVAL
  * checkpoint already past `approval`, decision recorded   -> DONE (reconciled)
  * anything else                                            -> left for the plain
    orphan handler (FAILED, never re-executed)
"""

import logging
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.observability.tracing import job_output, job_trace
from app.schemas.actions import action_status_of
from app.schemas.tasks import Job, JobStatus
from app.services.job_service import JobService

logger = logging.getLogger(__name__)

RECONCILED_NOTE = (
    "The service restarted while this job was finishing. The approved decision was "
    "applied exactly once; the final summary could not be generated."
)


class JobReconciler:
    def __init__(self, jobs: JobService, graph: CompiledStateGraph) -> None:
        self._jobs = jobs
        self._graph = graph

    async def run(self) -> dict[str, list[str]]:
        report: dict[str, list[str]] = {
            "requeued_lost_pending": [],
            "requeued_resume": [],
            "restored_waiting": [],
            "completed_from_checkpoint": [],
        }
        jobs = [job async for job in self._jobs.iter_jobs()]
        for job in jobs:
            if job.status is JobStatus.PENDING:
                if await self._jobs.enqueue_pending_if_missing(job.job_id):
                    report["requeued_lost_pending"].append(job.job_id)
            elif job.status is JobStatus.RUNNING:
                outcome = await self._reconcile_running(job)
                if outcome:
                    report[outcome].append(job.job_id)
        for outcome, ids in report.items():
            for job_id in ids:
                logger.warning("Reconciled job %s: %s", job_id, outcome)
        return report

    async def _reconcile_running(self, job: Job) -> str | None:
        snapshot = await self._graph.aget_state(
            {"configurable": {"thread_id": job.job_id}}
        )
        values: dict[str, Any] = snapshot.values or {}
        pending = values.get("pending_action")

        if "approval" in snapshot.next and pending:
            if job.approval is not None:
                # Decision recorded but not applied (or applied without checkpoint):
                # resuming again is safe because the write is idempotent.
                return "requeued_resume" if await self._jobs.requeue_for_resume(job.job_id) else None
            await self._jobs.mark_waiting_approval(job.job_id, pending)
            return "restored_waiting"

        if job.approval is not None and values:
            result = values.get("result") or {
                "answer": RECONCILED_NOTE,
                "agents_consulted": list(values.get("agent_outputs", {})),
                "sources": values.get("context", {}).get("sources", []),
                "action": values.get("action_result"),
                "action_status": action_status_of(values.get("action_result")),
                "reconciled": True,
            }
            await self._jobs.mark_done(job.job_id, result)
            # The graph did not run again, so trace the completion explicitly (resumed=True).
            async with job_trace(job) as trace_run:
                trace_run.set_status("done", reconciled=True)
                trace_run.set_outputs(job_output(
                    job.job_id, JobStatus.DONE,
                    action_status=result.get("action_status"),
                    has_answer=bool(result.get("answer")),
                ))
            return "completed_from_checkpoint"
        return None
