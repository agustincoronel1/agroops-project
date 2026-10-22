"""HITL + checkpointer tests. MOCKED: InMemorySaver and FakeRedis (no real services)."""

import asyncio
from typing import Any

import pytest
from app.core.config import Settings, get_settings
from app.graph.builder import build_graph, graph_config
from app.graph.state import new_state
from app.infrastructure.redis import get_redis
from app.main import app
from app.schemas.actions import MachineryIncidentRequest
from app.schemas.tasks import ApprovalRequest, JobStatus
from app.services.graph_processor import make_graph_processor
from app.services.incident_service import IncidentService, incident_key
from app.services.job_service import (
    ApprovalConflictError,
    JobNotFoundError,
    JobService,
)
from app.services.task_worker import TaskWorker
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, ValidationError
from tests.test_graph import FakeLLM
from tests.test_tasks import FakeRedis


class HitlLLM(FakeLLM):
    """Scripted supervisor decisions + a fixed incident extraction."""

    def __init__(
        self, decisions: list[str], request: MachineryIncidentRequest | None = None
    ) -> None:
        super().__init__(decisions)
        self.request = request or MachineryIncidentRequest(
            machine="Cosechadora", description="Pierde presion hidraulica", severity="high"
        )
        self.incident_parse_calls = 0

    async def parse(self, messages: Any, response_model: type[BaseModel]) -> Any:
        if response_model is MachineryIncidentRequest:
            self.incident_parse_calls += 1
            return self.request
        return await super().parse(messages, response_model)


def _graph(llm: FakeLLM, redis: FakeRedis, saver: InMemorySaver) -> Any:
    return build_graph(
        llm, None, 4, checkpointer=saver, incidents=IncidentService(redis)  # type: ignore[arg-type]
    )


def _cfg(thread_id: str) -> dict[str, Any]:
    return {**graph_config(4), "configurable": {"thread_id": thread_id}}


def _incident_keys(redis: FakeRedis) -> list[str]:
    return [k for k in redis.data if k.startswith("agroops:incident:")]


@pytest.mark.anyio
async def test_interrupt_pauses_before_executing_action() -> None:
    redis, saver = FakeRedis(), InMemorySaver()
    graph = _graph(HitlLLM(["register_incident", "FINISH"]), redis, saver)
    out = await graph.ainvoke(new_state("job1", "registrar incidencia"), config=_cfg("job1"))
    interrupts = out["__interrupt__"]
    assert interrupts[0].value["type"] == "register_machinery_incident"
    assert interrupts[0].value["payload"]["machine"] == "Cosechadora"
    assert _incident_keys(redis) == []  # nothing executed yet
    assert (await graph.aget_state(_cfg("job1"))).next == ("approval",)


@pytest.mark.anyio
async def test_approve_executes_exactly_once() -> None:
    redis, saver = FakeRedis(), InMemorySaver()
    llm = HitlLLM(["register_incident", "FINISH"])
    graph = _graph(llm, redis, saver)
    await graph.ainvoke(new_state("job1", "x", "demo"), config=_cfg("job1"))
    out = await graph.ainvoke(
        Command(resume={"approve": True, "reason": "ok"}), config=_cfg("job1")
    )

    assert out["result"]["action"]["status"] == "executed"
    assert out["result"]["action"]["created"] is True
    assert _incident_keys(redis) == [incident_key("job1-incident")]
    assert llm.incident_parse_calls == 1  # LLM extraction NOT re-run on resume
    stored = await IncidentService(redis).list_incidents("demo")  # type: ignore[arg-type]
    assert len(stored) == 1 and stored[0].severity == "high" and stored[0].job_id == "job1"


@pytest.mark.anyio
async def test_reject_does_not_modify_data() -> None:
    redis, saver = FakeRedis(), InMemorySaver()
    graph = _graph(HitlLLM(["register_incident", "FINISH"]), redis, saver)
    await graph.ainvoke(new_state("job1", "x"), config=_cfg("job1"))
    before = (dict(redis.data), {k: set(v) for k, v in redis.sets.items()})
    out = await graph.ainvoke(
        Command(resume={"approve": False, "reason": "no"}), config=_cfg("job1")
    )
    assert out["result"]["action"] == {"status": "rejected", "reason": "no"}
    assert (dict(redis.data), {k: set(v) for k, v in redis.sets.items()}) == before
    assert _incident_keys(redis) == []


@pytest.mark.anyio
async def test_incident_register_is_idempotent() -> None:
    svc = IncidentService(FakeRedis())  # type: ignore[arg-type]
    req = MachineryIncidentRequest(machine="M", description="d", severity="low")
    first, created1 = await svc.register("i1", "j1", "demo", req)
    second, created2 = await svc.register("i1", "j1", "demo", req)
    assert (created1, created2) == (True, False)
    assert first.incident_id == second.incident_id
    assert len(await svc.list_incidents("demo")) == 1


@pytest.mark.anyio
async def test_threads_are_isolated() -> None:
    redis, saver = FakeRedis(), InMemorySaver()
    req_a = MachineryIncidentRequest(machine="A", description="a", severity="low")
    req_b = MachineryIncidentRequest(machine="B", description="b", severity="high")
    ga = _graph(HitlLLM(["register_incident", "FINISH"], req_a), redis, saver)
    gb = _graph(HitlLLM(["register_incident", "FINISH"], req_b), redis, saver)
    await ga.ainvoke(new_state("A", "x"), config=_cfg("A"))
    await gb.ainvoke(new_state("B", "x"), config=_cfg("B"))

    await ga.ainvoke(Command(resume={"approve": True}), config=_cfg("A"))
    assert _incident_keys(redis) == [incident_key("A-incident")]
    state_b = await gb.aget_state(_cfg("B"))  # B is still paused with ITS OWN action
    assert state_b.next == ("approval",)
    assert state_b.values["pending_action"]["payload"]["machine"] == "B"

    await gb.ainvoke(Command(resume={"approve": False}), config=_cfg("B"))
    assert _incident_keys(redis) == [incident_key("A-incident")]  # B rejected: unchanged


# --- JobService approval rules ---------------------------------------------------------
async def _waiting_job(jobs: JobService, text: str = "x") -> str:
    job = await jobs.create_job(text)
    await jobs.dequeue()  # the worker consumed the creation entry
    await jobs.mark_running(job.job_id)
    await jobs.mark_waiting_approval(
        job.job_id, {"action_id": f"{job.job_id}-incident", "type": "t"}
    )
    return job.job_id


@pytest.mark.anyio
async def test_submit_approval_rules() -> None:
    redis = FakeRedis()
    jobs = JobService(redis)  # type: ignore[arg-type]
    with pytest.raises(JobNotFoundError):
        await jobs.submit_approval("nope", ApprovalRequest(approve=True))
    pending = await jobs.create_job("p")
    await jobs.dequeue()
    with pytest.raises(ApprovalConflictError):  # not waiting
        await jobs.submit_approval(pending.job_id, ApprovalRequest(approve=True))

    job_id = await _waiting_job(jobs)
    out = await jobs.submit_approval(job_id, ApprovalRequest(approve=True, reason="r"))
    assert out.status is JobStatus.PENDING and out.approval and out.approval.approve
    with pytest.raises(ApprovalConflictError):  # duplicate
        await jobs.submit_approval(job_id, ApprovalRequest(approve=False))
    assert redis.lists["agroops:jobs:queue"].count(job_id) == 1  # enqueued once


@pytest.mark.anyio
async def test_concurrent_duplicate_approvals_only_one_wins() -> None:
    redis = FakeRedis()
    jobs = JobService(redis)  # type: ignore[arg-type]
    job_id = await _waiting_job(jobs)
    results = await asyncio.gather(
        *[jobs.submit_approval(job_id, ApprovalRequest(approve=True)) for _ in range(5)],
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert redis.lists["agroops:jobs:queue"].count(job_id) == 1


# --- Worker end-to-end ---------------------------------------------------------------------
@pytest.mark.anyio
async def test_worker_waiting_does_not_block_and_resume_runs_once() -> None:
    redis, saver = FakeRedis(), InMemorySaver()
    jobs = JobService(redis)  # type: ignore[arg-type]
    llm = HitlLLM(["register_incident", "FINISH", "FINISH"])
    worker = TaskWorker(jobs, make_graph_processor(_graph(llm, redis, saver), 4))

    waiting = await jobs.create_job("registrar incidencia")
    await worker.process_job(waiting.job_id)
    job = await jobs.get_job(waiting.job_id)
    assert job is not None and job.status is JobStatus.WAITING_APPROVAL
    assert job.pending_action and job.pending_action["payload"]["machine"] == "Cosechadora"

    # a normal job right after is NOT blocked by the waiting one
    normal = await jobs.create_job("consulta normal")
    await worker.process_job(normal.job_id)
    done = await jobs.get_job(normal.job_id)
    assert done is not None and done.status is JobStatus.DONE and _incident_keys(redis) == []

    await jobs.submit_approval(waiting.job_id, ApprovalRequest(approve=True))
    queued = await jobs.dequeue()
    assert queued == waiting.job_id
    await worker.process_job(queued)
    final = await jobs.get_job(waiting.job_id)
    assert final is not None and final.status is JobStatus.DONE
    assert final.result is not None and final.result["action"]["status"] == "executed"
    assert final.pending_action is None
    assert len(_incident_keys(redis)) == 1

    # stale/duplicate queue entry must neither re-run nor re-execute
    await worker.process_job(waiting.job_id)
    again = await jobs.get_job(waiting.job_id)
    assert again is not None and again.status is JobStatus.DONE
    assert len(_incident_keys(redis)) == 1 and llm.incident_parse_calls == 1


@pytest.mark.anyio
async def test_invalid_incident_extraction_fails_job() -> None:
    with pytest.raises(ValidationError):
        MachineryIncidentRequest(machine="M", description="d", severity="catastrophic")  # type: ignore[arg-type]

    class BrokenLLM(HitlLLM):
        async def parse(self, messages: Any, response_model: type[BaseModel]) -> Any:
            if response_model is MachineryIncidentRequest:
                raise ValueError("LLM returned no parsable structured output")
            return await FakeLLM.parse(self, messages, response_model)

    redis, saver = FakeRedis(), InMemorySaver()
    jobs = JobService(redis)  # type: ignore[arg-type]
    graph = _graph(BrokenLLM(["register_incident"]), redis, saver)
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    job = await jobs.create_job("x")
    await worker.process_job(job.job_id)
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.status is JobStatus.FAILED
    assert _incident_keys(redis) == []


@pytest.mark.anyio
async def test_graph_without_checkpointer_never_offers_incident_action() -> None:
    graph = build_graph(FakeLLM(["operations", "FINISH"]))
    final = await graph.ainvoke(new_state("t", "q"), config=graph_config(4))
    assert final["result"]["action"] is None


# --- API ----------------------------------------------------------------------------------
@pytest.fixture
def client_redis() -> Any:
    redis = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, approval_api_key="test-key"  # type: ignore[call-arg]
    )
    yield TestClient(app, headers={"X-API-Key": "test-key"}), redis
    app.dependency_overrides.clear()


def test_approve_endpoint_flow(client_redis: Any) -> None:
    client, redis = client_redis
    assert client.post("/tasks/nope/approve", json={"approve": True}).status_code == 404
    job_id = client.post("/tasks", json={"user_input": "x"}).json()["job_id"]
    r = client.post(f"/tasks/{job_id}/approve", json={"approve": True})
    assert r.status_code == 409 and "not waiting" in r.json()["detail"]

    async def _wait() -> None:  # put the job in WAITING_APPROVAL as the worker would
        jobs = JobService(redis)
        await jobs.mark_running(job_id)
        await jobs.mark_waiting_approval(job_id, {"action_id": "a1", "type": "t"})

    asyncio.run(_wait())
    got = client.get(f"/tasks/{job_id}").json()
    assert got["status"] == "WAITING_APPROVAL" and got["pending_action"]["action_id"] == "a1"
    assert client.post(f"/tasks/{job_id}/approve", json={}).status_code == 422
    too_long = {"approve": True, "reason": "x" * 501}
    assert client.post(f"/tasks/{job_id}/approve", json=too_long).status_code == 422
    ok = client.post(f"/tasks/{job_id}/approve", json={"approve": False, "reason": "no"})
    assert ok.status_code == 200 and ok.json() == {"job_id": job_id, "status": "PENDING"}
    assert client.post(f"/tasks/{job_id}/approve", json={"approve": True}).status_code == 409
    assert client.get(f"/tasks/{job_id}").json()["approval"]["approve"] is False
