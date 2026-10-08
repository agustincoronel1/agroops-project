"""Regression tests: crash windows around approval/resume + approval API key.

MOCKED (InMemorySaver + FakeRedis with WATCH/MULTI/EXEC semantics). The same scenarios
are exercised against real Redis Stack/Docker by scripts/hitl_checkpoint_integration.py.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.core.config import Settings, get_settings
from app.infrastructure.redis import get_redis
from app.main import app
from app.schemas.actions import MachineryIncidentRequest
from app.schemas.tasks import ApprovalRequest, JobStatus
from app.services.graph_processor import make_graph_processor
from app.services.incident_service import IncidentService
from app.services.job_service import ApprovalConflictError, JobService
from app.services.reconciliation import RECONCILED_NOTE, JobReconciler
from app.services.task_worker import TaskWorker
from tests.test_hitl import HitlLLM, _cfg, _graph, _incident_keys
from tests.test_tasks import FakeRedis

QUEUE = "agroops:jobs:queue"


class Env:
    """A job waiting for approval, with graph/worker/reconciler wired to shared stores."""

    def __init__(self, decisions: list[str] | None = None) -> None:
        self.redis = FakeRedis()
        self.saver = InMemorySaver()
        self.llm = HitlLLM(decisions or ["register_incident", "FINISH", "FINISH"])
        self.graph = _graph(self.llm, self.redis, self.saver)
        self.jobs = JobService(self.redis)  # type: ignore[arg-type]
        self.worker = TaskWorker(self.jobs, make_graph_processor(self.graph, 4))
        self.reconciler = JobReconciler(self.jobs, self.graph)

    async def waiting_job(self) -> str:
        job = await self.jobs.create_job("registrar incidencia")
        assert await self.jobs.dequeue() == job.job_id
        await self.worker.process_job(job.job_id)
        assert (await self.jobs.get_job(job.job_id)).status is JobStatus.WAITING_APPROVAL  # type: ignore[union-attr]
        return job.job_id

    async def approve_and_take_from_queue(self, job_id: str, approve: bool = True) -> None:
        await self.jobs.submit_approval(job_id, ApprovalRequest(approve=approve))
        assert await self.jobs.dequeue() == job_id


# --- crash window between "decision recorded" and "enqueued" -------------------------------
@pytest.mark.anyio
async def test_failed_exec_leaves_job_waiting_and_retry_succeeds_exactly_once() -> None:
    env = Env()
    job_id = await env.waiting_job()
    env.redis.fail_next_exec = True  # crash/failure at the EXEC step: nothing applied
    with pytest.raises(ConnectionError):
        await env.jobs.submit_approval(job_id, ApprovalRequest(approve=True))

    job = await env.jobs.get_job(job_id)
    assert job is not None and job.status is JobStatus.WAITING_APPROVAL and job.approval is None
    assert env.redis.lists.get(QUEUE, []) == []  # neither decided nor queued: no half state

    await env.jobs.submit_approval(job_id, ApprovalRequest(approve=True))  # client retry works
    assert env.redis.lists[QUEUE] == [job_id]
    with pytest.raises(ApprovalConflictError):
        await env.jobs.submit_approval(job_id, ApprovalRequest(approve=True))


@pytest.mark.anyio
async def test_concurrent_approvals_single_decision_single_enqueue() -> None:
    env = Env()
    job_id = await env.waiting_job()
    results = await asyncio.gather(
        *[
            env.jobs.submit_approval(job_id, ApprovalRequest(approve=i % 2 == 0))
            for i in range(10)
        ],
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert env.redis.lists[QUEUE] == [job_id]


# --- crash after the worker took the job from the queue -------------------------------------
@pytest.mark.anyio
async def test_lost_pending_job_is_requeued_once() -> None:
    env = Env()
    job = await env.jobs.create_job("x")
    await env.jobs.dequeue()  # worker took it, then the process died before mark_running
    first = await env.reconciler.run()
    second = await env.reconciler.run()
    assert first["requeued_lost_pending"] == [job.job_id] and second["requeued_lost_pending"] == []
    assert env.redis.lists[QUEUE] == [job.job_id]


# --- restart DURING resume ------------------------------------------------------------------
@pytest.mark.anyio
async def test_restart_before_action_ran_resumes_and_executes_once() -> None:
    env = Env()
    job_id = await env.waiting_job()
    await env.approve_and_take_from_queue(job_id)
    await env.jobs.mark_running(job_id)  # process died right after starting the resume
    assert _incident_keys(env.redis) == []

    report = await env.reconciler.run()
    assert report["requeued_resume"] == [job_id]
    assert (await env.jobs.get_job(job_id)).status is JobStatus.PENDING  # type: ignore[union-attr]

    await env.worker.process_job((await env.jobs.dequeue()) or "")
    final = await env.jobs.get_job(job_id)
    assert final is not None and final.status is JobStatus.DONE
    assert final.result is not None and final.result["action"]["status"] == "executed"
    assert len(_incident_keys(env.redis)) == 1 and env.llm.incident_parse_calls == 1


@pytest.mark.anyio
async def test_restart_after_action_ran_but_before_checkpoint_does_not_duplicate() -> None:
    env = Env()
    job_id = await env.waiting_job()
    pending = (await env.jobs.get_job(job_id)).pending_action  # type: ignore[union-attr]
    assert pending is not None
    # the write happened, but the process died before the checkpoint/job were updated
    await IncidentService(env.redis).register(  # type: ignore[arg-type]
        pending["action_id"], job_id, "demo", MachineryIncidentRequest(**pending["payload"])
    )
    await env.approve_and_take_from_queue(job_id)
    await env.jobs.mark_running(job_id)

    assert (await env.reconciler.run())["requeued_resume"] == [job_id]
    await env.worker.process_job((await env.jobs.dequeue()) or "")
    final = await env.jobs.get_job(job_id)
    assert final is not None and final.status is JobStatus.DONE
    assert final.result is not None and final.result["action"]["created"] is False  # idempotent
    assert len(_incident_keys(env.redis)) == 1


@pytest.mark.anyio
async def test_restart_after_graph_finished_but_before_job_update_reconciles_to_done() -> None:
    env = Env()
    job_id = await env.waiting_job()
    await env.approve_and_take_from_queue(job_id)
    await env.jobs.mark_running(job_id)
    # the graph completed (action executed, checkpoint saved) but mark_done never ran
    await env.graph.ainvoke(Command(resume={"approve": True}), config=_cfg(job_id))
    assert len(_incident_keys(env.redis)) == 1

    assert (await env.reconciler.run())["completed_from_checkpoint"] == [job_id]
    final = await env.jobs.get_job(job_id)
    assert final is not None and final.status is JobStatus.DONE
    assert final.result is not None and final.result["action"]["status"] == "executed"
    assert len(_incident_keys(env.redis)) == 1 and env.redis.lists.get(QUEUE, []) == []


@pytest.mark.anyio
async def test_reconcile_past_approval_without_final_result_uses_checkpoint_action() -> None:
    env = Env()
    job = await env.jobs.create_job("x")
    await env.jobs.dequeue()
    await env.jobs.mark_running(job.job_id)
    await env.jobs._update(  # approval recorded, then process died mid-resume
        job.job_id,
        approval=SimpleNamespace(approve=True, reason=None, decided_at=job.created_at),
    )
    snapshot = SimpleNamespace(
        next=("supervisor",),
        values={"action_result": {"status": "executed", "incident_id": "i"}, "agent_outputs": {"actions": "s"}, "context": {}},
    )

    class FakeGraph:
        async def aget_state(self, cfg: Any) -> Any:
            return snapshot

    report = await JobReconciler(env.jobs, FakeGraph()).run()  # type: ignore[arg-type]
    assert report["completed_from_checkpoint"] == [job.job_id]
    final = await env.jobs.get_job(job.job_id)
    assert final is not None and final.status is JobStatus.DONE
    assert final.result is not None
    assert final.result["answer"] == RECONCILED_NOTE and final.result["reconciled"] is True
    assert final.result["action"]["status"] == "executed"


@pytest.mark.anyio
async def test_restart_after_interrupt_saved_but_before_waiting_update_restores_waiting() -> None:
    env = Env()
    job = await env.jobs.create_job("registrar incidencia")
    await env.jobs.dequeue()
    await env.jobs.mark_running(job.job_id)
    await env.graph.ainvoke(  # graph paused at the gate; mark_waiting_approval never ran
        __import__("app.graph.state", fromlist=["new_state"]).new_state(job.job_id, "x"),
        config=_cfg(job.job_id),
    )
    assert (await env.reconciler.run())["restored_waiting"] == [job.job_id]
    restored = await env.jobs.get_job(job.job_id)
    assert restored is not None and restored.status is JobStatus.WAITING_APPROVAL
    assert restored.pending_action is not None
    await env.jobs.submit_approval(job.job_id, ApprovalRequest(approve=False))  # decidable again


@pytest.mark.anyio
async def test_unrecoverable_running_orphan_is_still_failed_not_rerun() -> None:
    env = Env()
    job = await env.jobs.create_job("x")
    await env.jobs.dequeue()
    await env.jobs.mark_running(job.job_id)  # no checkpoint at all
    assert all(not v for v in (await env.reconciler.run()).values())
    assert await env.jobs.fail_orphaned_running_jobs() == [job.job_id]
    failed = await env.jobs.get_job(job.job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert env.redis.lists.get(QUEUE, []) == []


# --- /approve API key ---------------------------------------------------------------------
def _client(key: str | None) -> tuple[TestClient, FakeRedis]:
    redis = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, approval_api_key=key  # type: ignore[call-arg]
    )
    return TestClient(app), redis


@pytest.fixture(autouse=True)
def _clear_overrides() -> Any:
    yield
    app.dependency_overrides.clear()


def test_approve_disabled_when_no_key_configured() -> None:
    client, _ = _client(None)
    for headers in ({}, {"X-API-Key": "anything"}):
        r = client.post("/tasks/x/approve", json={"approve": True}, headers=headers)
        assert r.status_code == 503 and "APPROVAL_API_KEY" in r.json()["detail"]
    client, _ = _client("")
    assert client.post("/tasks/x/approve", json={"approve": True}).status_code == 503


def test_approve_requires_correct_key() -> None:
    client, redis = _client("s3cret-key")
    body = {"approve": True}
    assert client.post("/tasks/x/approve", json=body).status_code == 401
    wrong = client.post("/tasks/x/approve", json=body, headers={"X-API-Key": "nope"})
    assert wrong.status_code == 401 and "s3cret-key" not in wrong.text
    ok = client.post("/tasks/x/approve", json=body, headers={"X-API-Key": "s3cret-key"})
    assert ok.status_code == 404  # authenticated; job simply does not exist
    assert redis.lists.get(QUEUE, []) == []  # unauthenticated calls changed nothing


def test_other_endpoints_do_not_need_the_key_and_swagger_documents_it() -> None:
    client, _ = _client("very-secret-value-123")
    assert client.post("/tasks", json={"user_input": "hola"}).status_code == 202
    assert client.get("/tasks/none").status_code == 404
    raw = client.get("/openapi.json")
    assert "very-secret-value-123" not in raw.text  # the value is never published
    spec = raw.json()
    scheme = spec["components"]["securitySchemes"]
    assert any(v["type"] == "apiKey" and v["name"] == "X-API-Key" and v["in"] == "header" for v in scheme.values())
    assert spec["paths"]["/tasks/{job_id}/approve"]["post"]["security"]
    assert "security" not in spec["paths"]["/tasks"]["post"]


@pytest.mark.anyio
async def test_summary_failure_after_applied_action_is_done_with_warning() -> None:
    # (replaces the earlier test that expected FAILED: the action DID happen)
    env = Env(["register_incident"])  # supervisor LLM script runs out right after approval
    job_id = await env.waiting_job()
    await env.approve_and_take_from_queue(job_id)
    await env.worker.process_job(job_id)

    final = await env.jobs.get_job(job_id)
    assert final is not None and final.status is JobStatus.DONE and final.error is None
    assert final.result is not None
    assert final.result["action_status"] == "APPLIED"
    assert final.result["action"]["status"] == "executed" and final.result["answer"] is None
    assert "applied" in final.result["warning"] and "IndexError" in final.result["warning"]
    assert len(_incident_keys(env.redis)) == 1


@pytest.mark.anyio
async def test_applied_action_with_summary_failure_is_never_rerun() -> None:
    env = Env(["register_incident"])
    job_id = await env.waiting_job()
    await env.approve_and_take_from_queue(job_id)
    await env.worker.process_job(job_id)
    snapshot_before = dict(env.redis.data)

    await env.worker.process_job(job_id)  # stale duplicate queue entry
    await env.reconciler.run()  # restart-time reconciliation
    assert await env.jobs.fail_orphaned_running_jobs() == []
    assert dict(env.redis.data) == snapshot_before  # nothing changed, nothing re-executed
    done = await env.jobs.get_job(job_id)
    assert done is not None and done.status is JobStatus.DONE
    assert len(_incident_keys(env.redis)) == 1 and env.llm.incident_parse_calls == 1


@pytest.mark.anyio
async def test_action_failure_still_fails_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env()
    job_id = await env.waiting_job()

    async def boom(*a: Any, **k: Any) -> Any:
        raise ConnectionError("redis write failed")

    monkeypatch.setattr(IncidentService, "register", boom)
    await env.approve_and_take_from_queue(job_id)
    await env.worker.process_job(job_id)
    final = await env.jobs.get_job(job_id)
    assert final is not None and final.status is JobStatus.FAILED
    assert final.result is None and _incident_keys(env.redis) == []


@pytest.mark.anyio
async def test_rejected_action_with_summary_failure_stays_failed_but_says_no_change() -> None:
    env = Env(["register_incident"])
    job_id = await env.waiting_job()
    await env.approve_and_take_from_queue(job_id, approve=False)
    await env.worker.process_job(job_id)
    final = await env.jobs.get_job(job_id)
    assert final is not None and final.status is JobStatus.FAILED
    assert "rejected" in (final.error or "") and _incident_keys(env.redis) == []


@pytest.mark.anyio
async def test_normal_completion_reports_action_status() -> None:
    env = Env()
    job_id = await env.waiting_job()
    await env.approve_and_take_from_queue(job_id)
    await env.worker.process_job(job_id)
    final = await env.jobs.get_job(job_id)
    assert final is not None and final.status is JobStatus.DONE
    assert final.result is not None and final.result["action_status"] == "APPLIED"
    assert "warning" not in final.result and final.result["answer"]
