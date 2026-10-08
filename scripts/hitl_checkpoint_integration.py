"""REAL integration (Redis Stack + AsyncRedisSaver + Docker), NO OpenAI: scripted LLM stub.

Run inside the backend image while the backend service is STOPPED (so its worker does
not compete for the queue), feeding this file through stdin:

    docker compose stop backend
    docker compose run --rm --no-deps -T backend python - <phase> [args] < scripts/hitl_checkpoint_integration.py

Phases
  start                         job -> WAITING_APPROVAL (prints job_id)
  inspect <job>                 print job + incident keys
  race <job>                    10 concurrent approvals against real Redis (WATCH/MULTI/EXEC)
  approve <job> yes|no          submit approval only (decision recorded + enqueued)
  fail_summary <job>            resume where the LLM fails AFTER the incident was applied
  hang_after_action <job>       take job from queue and resume; HANGS after the incident was written
  hang_before_action <job>      same, but HANGS before the incident is written
  hang_waiting_update           run a fresh job; HANGS after the interrupt checkpoint, before WAITING update
  lost_pending                  create a job and take it from the queue, then exit (simulated crash state)
  recover [job ...]             run startup reconciliation, then process the queue until empty
Kill the container (docker kill) while a hang_* phase is running to simulate a hard crash.
"""

import asyncio
import json
import sys
from typing import Any

from pydantic import BaseModel

from app.core.config import get_settings
from app.graph.builder import build_graph
from app.infrastructure.checkpointer import open_checkpointer
from app.infrastructure.redis import create_redis_client
from app.schemas.actions import MachineryIncidentRequest
from app.schemas.tasks import ApprovalRequest
from app.services.graph_processor import make_graph_processor
from app.services.incident_service import IncidentService
from app.services.job_service import ApprovalConflictError, JobService
from app.services.reconciliation import JobReconciler
from app.services.task_worker import TaskWorker


class ScriptedLLM:
    def __init__(
        self, decisions: list[str], hang_on_route: bool = False, fail_on_route: bool = False
    ) -> None:
        self.decisions = list(decisions)
        self.hang_on_route = hang_on_route
        self.fail_on_route = fail_on_route
        self.extractions = 0

    async def parse(self, messages: Any, response_model: type[BaseModel]) -> Any:
        if response_model is MachineryIncidentRequest:
            self.extractions += 1
            return MachineryIncidentRequest(
                machine="Cosechadora", description="Falla hidraulica (integration test)", severity="high"
            )
        if self.fail_on_route:
            raise TimeoutError("simulated LLM outage while routing/summarising")
        if self.hang_on_route:
            print("HANGING in supervisor (after approval node)", flush=True)
            await asyncio.sleep(300)
        decision = self.decisions.pop(0) if self.decisions else "FINISH"
        return response_model(next=decision, reasoning="scripted")

    async def complete(self, messages: Any) -> str:
        return "scripted final answer"


class SlowIncidents(IncidentService):
    async def register(self, *a: Any, **k: Any) -> Any:
        print("HANGING before writing the incident", flush=True)
        await asyncio.sleep(300)
        return await super().register(*a, **k)


async def build(
    decisions: list[str],
    *,
    hang_route: bool = False,
    slow_incidents: bool = False,
    fail_route: bool = False,
):
    s = get_settings()
    redis = create_redis_client(s.redis_url)
    cm = open_checkpointer(s.redis_url)
    saver = await cm.__aenter__()
    llm = ScriptedLLM(decisions, hang_route, fail_route)
    incidents = (SlowIncidents if slow_incidents else IncidentService)(redis)
    graph = build_graph(llm, None, 4, checkpointer=saver, incidents=incidents)
    jobs = JobService(redis)
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    return redis, cm, jobs, worker, JobReconciler(jobs, graph), llm


async def show(redis: Any, jobs: JobService, job_id: str) -> None:
    j = await jobs.get_job(job_id)
    print(json.dumps({
        "job_id": job_id, "status": j.status.value, "has_pending": bool(j.pending_action),
        "approval": None if j.approval is None else j.approval.approve,
        "action": (j.result or {}).get("action"), "reconciled": (j.result or {}).get("reconciled"),
        "error": j.error,
    }))
    print("incident keys:", sorted([k async for k in redis.scan_iter("agroops:incident:*")]))
    print("queue:", await redis.lrange("agroops:jobs:queue", 0, -1))


async def main() -> None:
    phase, args = sys.argv[1], sys.argv[2:]
    redis, cm, jobs, worker, reconciler, llm = await build(
        ["register_incident"] if phase in ("start", "hang_waiting_update") else ["FINISH"],
        hang_route=phase == "hang_after_action",
        slow_incidents=phase == "hang_before_action",
        fail_route=phase == "fail_summary",
    )
    if phase == "start":
        job = await jobs.create_job("registrar incidencia de la cosechadora", "demo")
        assert await jobs.dequeue() == job.job_id
        await worker.process_job(job.job_id)
        await show(redis, jobs, job.job_id)
    elif phase == "inspect":
        await show(redis, jobs, args[0])
    elif phase == "race":
        results = await asyncio.gather(
            *[jobs.submit_approval(args[0], ApprovalRequest(approve=i % 2 == 0)) for i in range(10)],
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, Exception)]
        others = [repr(r) for r in results if isinstance(r, Exception) and not isinstance(r, ApprovalConflictError)]
        print("successes:", len(ok), "| conflicts:", sum(isinstance(r, ApprovalConflictError) for r in results),
              "| other errors:", others)
        await show(redis, jobs, args[0])
    elif phase == "approve":
        await jobs.submit_approval(args[0], ApprovalRequest(approve=args[1] == "yes", reason="integration"))
        await show(redis, jobs, args[0])
    elif phase in ("hang_after_action", "hang_before_action"):
        assert await jobs.dequeue() == args[0]
        print("RESUMING", args[0], flush=True)
        await worker.process_job(args[0])
        print("UNEXPECTED: finished without hanging")
    elif phase == "fail_summary":  # resume where the LLM fails AFTER the action was applied
        assert await jobs.dequeue() == args[0]
        await worker.process_job(args[0])
        await show(redis, jobs, args[0])
        j = await jobs.get_job(args[0])
        print("result:", json.dumps({k: (j.result or {}).get(k) for k in ("answer", "action_status", "warning")}))
    elif phase == "hang_waiting_update":
        job = await jobs.create_job("registrar incidencia", "demo")
        assert await jobs.dequeue() == job.job_id
        original = jobs.mark_waiting_approval

        async def slow(job_id: str, pending: dict[str, Any]) -> Any:
            print("HANGING: interrupt checkpoint saved, job not yet WAITING_APPROVAL", job_id, flush=True)
            await asyncio.sleep(300)
            return await original(job_id, pending)

        jobs.mark_waiting_approval = slow  # type: ignore[method-assign]
        print("JOB", job.job_id, flush=True)
        await worker.process_job(job.job_id)
    elif phase == "lost_pending":
        job = await jobs.create_job("job perdido tras dequeue", "demo")
        assert await jobs.dequeue() == job.job_id
        print("JOB", job.job_id, "(taken from queue, never started)")
    elif phase == "recover":
        print("reconcile report:", json.dumps(await reconciler.run()))
        print("plain orphan handler failed:", await jobs.fail_orphaned_running_jobs())
        processed = []
        while (job_id := await jobs.dequeue(1)) is not None:
            await worker.process_job(job_id)
            processed.append(job_id)
        print("processed from queue:", processed, "| LLM extractions in this process:", llm.extractions)
        for job_id in args:
            await show(redis, jobs, job_id)
    await cm.__aexit__(None, None, None)
    await redis.aclose()


asyncio.run(main())
