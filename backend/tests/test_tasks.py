import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import StateGraph
from langchain_core.messages import HumanMessage

from app.graph.state import AgroOpsState, new_state
from app.infrastructure.redis import get_redis
from app.main import app
from app.schemas.tasks import Job, JobStatus
from app.services.job_service import JobService
from app.services.task_worker import TaskWorker


class FakePipeline:
    """WATCH/MULTI/EXEC with optimistic-lock semantics (all-or-nothing EXEC)."""

    def __init__(self, redis: "FakeRedis") -> None:
        self._r = redis
        self._watched: dict[str, int] = {}
        self._buffer: list[tuple[str, str, str]] = []
        self._multi = False

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def watch(self, key: str) -> None:
        self._watched[key] = self._r.versions.get(key, 0)

    async def get(self, key: str) -> str | None:
        return self._r.data.get(key)

    def multi(self) -> None:
        self._multi = True

    def set(self, key: str, value: str) -> "FakePipeline":
        self._buffer.append(("set", key, value))
        return self

    def rpush(self, key: str, value: str) -> "FakePipeline":
        self._buffer.append(("rpush", key, value))
        return self

    async def execute(self) -> list[object]:
        from redis.exceptions import WatchError

        await asyncio.sleep(0)  # let concurrent coroutines interleave
        if self._r.fail_next_exec:
            self._r.fail_next_exec = False
            raise ConnectionError("simulated crash before EXEC")  # nothing applied
        if any(self._r.versions.get(k, 0) != v for k, v in self._watched.items()):
            raise WatchError("watched key changed")
        for op, key, value in self._buffer:
            if op == "set":
                self._r.data[key] = value
                self._r.versions[key] = self._r.versions.get(key, 0) + 1
            else:
                self._r.lists.setdefault(key, []).append(value)
        return [True] * len(self._buffer)


class FakeRedis:
    """Minimal in-memory stand-in for the async Redis commands we use."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.versions: dict[str, int] = {}
        self.fail_next_exec = False

    async def set(self, key: str, value: str, nx: bool = False) -> bool | None:
        if nx and key in self.data:
            return None
        self.data[key] = value
        self.versions[key] = self.versions.get(key, 0) + 1
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return list(self.lists.get(key, []))

    async def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    async def smembers(self, key: str) -> Any:
        return set(self.sets.get(key, set()))

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    async def scan_iter(self, match: str = "*"):  # type: ignore[no-untyped-def]
        import fnmatch

        for key in list(self.data):
            if fnmatch.fnmatch(key, match):
                yield key

    async def blpop(self, keys: list[str], timeout: int = 0) -> tuple[str, str] | None:
        for key in keys:
            if self.lists.get(key):
                return key, self.lists[key].pop(0)
        await asyncio.sleep(0.005)  # emulate blocking wait
        return None


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def client(redis: FakeRedis) -> Iterator[TestClient]:
    app.dependency_overrides[get_redis] = lambda: redis
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_and_get_job(client: TestClient, redis: FakeRedis) -> None:
    resp = client.post("/tasks", json={"user_input": "regar lote 3"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "PENDING"
    assert redis.lists["agroops:jobs:queue"] == [body["job_id"]]

    got = client.get(f"/tasks/{body['job_id']}")
    assert got.status_code == 200
    data = got.json()
    assert data["job_id"] == body["job_id"]
    assert data["status"] == "PENDING"
    assert data["user_input"] == "regar lote 3"
    assert data["result"] is None


def test_create_job_accepts_establishment_id(client: TestClient) -> None:
    default = client.post("/tasks", json={"user_input": "a"}).json()["job_id"]
    custom = client.post(
        "/tasks", json={"user_input": "a", "establishment_id": "finca-1"}
    ).json()["job_id"]
    assert client.get(f"/tasks/{default}").json()["establishment_id"] == "demo"
    assert client.get(f"/tasks/{custom}").json()["establishment_id"] == "finca-1"
    bad = client.post("/tasks", json={"user_input": "a", "establishment_id": "../x"})
    assert bad.status_code == 422


def test_get_unknown_job_returns_404(client: TestClient) -> None:
    assert client.get("/tasks/nope").status_code == 404


def test_create_job_validates_input(client: TestClient) -> None:
    assert client.post("/tasks", json={"user_input": ""}).status_code == 422


@pytest.mark.anyio
async def test_worker_transitions_pending_running_done(redis: FakeRedis) -> None:
    jobs = JobService(redis)  # type: ignore[arg-type]
    seen: list[JobStatus] = []

    async def processor(job: Job) -> dict[str, Any]:
        current = await jobs.get_job(job.job_id)
        assert current is not None
        seen.append(current.status)
        return {"ok": True}

    job = await jobs.create_job("hola")
    assert job.status is JobStatus.PENDING

    worker = TaskWorker(jobs, processor)
    job_id = await jobs.dequeue()
    assert job_id == job.job_id
    await worker.process_job(job_id)

    assert seen == [JobStatus.RUNNING]
    final = await jobs.get_job(job.job_id)
    assert final is not None
    assert final.status is JobStatus.DONE
    assert final.result == {"ok": True}
    assert final.error is None


@pytest.mark.anyio
async def test_worker_marks_failed_on_exception(redis: FakeRedis) -> None:
    jobs = JobService(redis)  # type: ignore[arg-type]

    async def processor(job: Job) -> dict[str, Any]:
        raise ValueError("boom")

    job = await jobs.create_job("hola")
    await TaskWorker(jobs, processor).process_job(job.job_id)

    final = await jobs.get_job(job.job_id)
    assert final is not None
    assert final.status is JobStatus.FAILED
    assert final.error == "ValueError: boom"
    assert final.result is None


@pytest.mark.anyio
async def test_worker_background_loop_processes_and_stops(redis: FakeRedis) -> None:
    jobs = JobService(redis)  # type: ignore[arg-type]

    async def processor(job: Job) -> dict[str, Any]:
        return {"done": job.user_input}

    worker = TaskWorker(jobs, processor, poll_timeout=0)
    worker.start()
    job = await jobs.create_job("x")
    for _ in range(100):
        current = await jobs.get_job(job.job_id)
        if current and current.status is JobStatus.DONE:
            break
        await asyncio.sleep(0.01)
    await worker.stop()
    assert current is not None and current.status is JobStatus.DONE


def test_agroops_state() -> None:
    state = new_state("t1", "hola")
    assert state["task_id"] == "t1"
    assert state["user_input"] == "hola"
    assert state["messages"] == []
    assert state["context"] == {}
    assert state["result"] is None and state["error"] is None
    assert set(AgroOpsState.__annotations__) >= {
        "task_id",
        "user_input",
        "messages",
        "current_step",
        "context",
        "result",
        "error",
    }
    # LangGraph accepts the schema and wires the messages reducer.
    graph = StateGraph(AgroOpsState)
    assert "messages" in graph.channels
    state["messages"] = [HumanMessage(content="hola")]
