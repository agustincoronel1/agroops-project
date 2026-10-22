"""Regression tests for audit bugs BUG-01 … BUG-06 (all MOCKED / static)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from app.agents.knowledge import (
    NO_KNOWLEDGE_MESSAGE,
    KnowledgeAnswer,
    RetrievedDocument,
    knowledge_node,
)
from app.agents.operations import NO_OPERATIONS_DATA_MESSAGE, operations_node
from app.agents.operations_data import get_snapshot
from app.graph.state import new_state
from app.infrastructure.redis import get_redis
from app.main import app
from app.schemas.tasks import JobStatus
from app.services.job_service import ORPHANED_JOB_ERROR, QUEUE_KEY, JobService
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from tests.test_graph import FakeLLM
from tests.test_tasks import FakeRedis

ROOT = Path(__file__).resolve().parents[2]


# --- BUG-01: orphaned RUNNING jobs -------------------------------------------------
@pytest.mark.anyio
async def test_orphaned_running_jobs_are_failed_not_rerun() -> None:
    redis = FakeRedis()
    jobs = JobService(redis)  # type: ignore[arg-type]
    running = await jobs.create_job("a")
    await jobs.mark_running(running.job_id)
    done = await jobs.create_job("b")
    await jobs.mark_done(done.job_id, {"ok": True})
    pending = await jobs.create_job("c")
    queue_before = list(redis.lists[QUEUE_KEY])

    recovered = await jobs.fail_orphaned_running_jobs()

    assert recovered == [running.job_id]
    failed = await jobs.get_job(running.job_id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert failed.error == ORPHANED_JOB_ERROR
    assert (await jobs.get_job(done.job_id)).status is JobStatus.DONE  # type: ignore[union-attr]
    assert (await jobs.get_job(pending.job_id)).status is JobStatus.PENDING  # type: ignore[union-attr]
    assert redis.lists[QUEUE_KEY] == queue_before  # nothing re-enqueued / re-executed
    assert await jobs.fail_orphaned_running_jobs() == []  # idempotent


# --- BUG-02: Redis down -> controlled 503 --------------------------------------------
class DeadRedis:
    async def _boom(self, *a: Any, **k: Any) -> None:
        raise RedisConnectionError("Error -2 connecting to redis:6379 (secret-internal-host)")

    set = get = rpush = _boom


@pytest.fixture
def dead_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_redis] = lambda: DeadRedis()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_post_tasks_returns_503_when_redis_down(dead_client: TestClient) -> None:
    r = dead_client.post("/tasks", json={"user_input": "x"})
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"]
    assert "secret-internal-host" not in r.text and "Traceback" not in r.text


def test_get_task_returns_503_when_redis_down(dead_client: TestClient) -> None:
    r = dead_client.get("/tasks/abc")
    assert r.status_code == 503
    assert "secret-internal-host" not in r.text


# --- BUG-03: Operations respects establishment_id --------------------------------------
def test_demo_data_only_for_demo_establishment() -> None:
    assert get_snapshot("demo") is not None
    assert get_snapshot("finca-b") is None
    assert get_snapshot("") is None


@pytest.mark.anyio
async def test_operations_gives_foreign_establishment_no_data() -> None:
    llm = FakeLLM([])
    out = await operations_node(new_state("t", "q", "finca-b"), llm)
    assert out["agent_outputs"]["operations"] == NO_OPERATIONS_DATA_MESSAGE
    assert llm.complete_calls == []  # no LLM call, no demo data sent anywhere

    out = await operations_node(new_state("t", "q", "demo"), llm)
    assert len(llm.complete_calls) == 1
    assert "DEMO" in llm.complete_calls[0][1]["content"]


# --- BUG-06 (tracked as RAG item): sources must support the answer ----------------------
def _doc(i: int) -> RetrievedDocument:
    return RetrievedDocument(
        source=f"doc{i}.md", content=f"c{i}", establishment_id="demo",
        document_id=f"d{i}", chunk_id=f"d{i}-0", distance=0.5,
    )


class Retriever:
    async def retrieve(self, q: str, e: str) -> list[RetrievedDocument]:
        return [_doc(1), _doc(2), _doc(3)]


async def _knowledge(answer: KnowledgeAnswer) -> dict[str, Any]:
    return await knowledge_node(
        new_state("t", "q", "demo"), FakeLLM([], knowledge=answer), Retriever()  # type: ignore[arg-type]
    )


@pytest.mark.anyio
async def test_only_supporting_passages_become_sources() -> None:
    out = await _knowledge(KnowledgeAnswer(answered=True, answer="ok", supporting_passages=[2, 2]))
    assert [s["source"] for s in out["context"]["sources"]] == ["doc2.md"]
    assert out["agent_outputs"]["knowledge"] == "ok"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [
        KnowledgeAnswer(answered=False, answer="no se", supporting_passages=[1]),
        KnowledgeAnswer(answered=True, answer="inventado", supporting_passages=[]),
        KnowledgeAnswer(answered=True, answer="inventado", supporting_passages=[0, 99]),
    ],
)
async def test_unsupported_answers_yield_no_sources_and_no_answer(answer: KnowledgeAnswer) -> None:
    out = await _knowledge(answer)
    assert out["context"]["sources"] == []
    assert out["agent_outputs"]["knowledge"] == NO_KNOWLEDGE_MESSAGE


def test_rag_threshold_not_changed_arbitrarily() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None).rag_max_distance == 0.75  # type: ignore[call-arg]


# --- BUG-04: Redis persistence in compose --------------------------------------------
def test_compose_redis_uses_named_volume_and_aof() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    redis = compose["services"]["redis"]
    assert "appendonly yes" in redis["environment"]["REDIS_ARGS"]  # Redis Stack image
    assert any(v.startswith("redis_data:/data") for v in redis["volumes"])
    assert "redis_data" in compose["volumes"] and "chroma_data" in compose["volumes"]


# --- BUG-05: repo hygiene --------------------------------------------------------------
def test_gitignore_covers_venv_and_caches() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    for entry in (".venv/", "__pycache__/", ".env"):
        assert entry in lines



@pytest.mark.anyio
async def test_synthesis_lists_only_supporting_documents_as_citations() -> None:
    """Regression: the final answer must not invent citations such as '[Fuente: Knowledge Agent]'."""
    from app.graph.builder import build_graph, graph_config
    from app.graph.state import new_state

    llm = FakeLLM(
        ["knowledge", "FINISH"],
        knowledge=KnowledgeAnswer(answered=True, answer="ok", supporting_passages=[1, 3]),
    )
    graph = build_graph(llm, Retriever(), 4)  # type: ignore[arg-type]
    final = await graph.ainvoke(new_state("t", "q", "demo"), config=graph_config(4))
    assert [s["source"] for s in final["result"]["sources"]] == ["doc1.md", "doc3.md"]

    system = llm.complete_calls[-1][0]["content"]
    allowed = system.split("Los únicos documentos citables son:")[1]
    assert "[doc1.md], [doc3.md]" in allowed
    assert "doc2.md" not in system  # retrieved but not supporting: not citable
    assert "Knowledge Agent" not in allowed


@pytest.mark.anyio
async def test_synthesis_without_sources_forbids_citations() -> None:
    from app.graph.builder import build_graph, graph_config
    from app.graph.state import new_state

    llm = FakeLLM(["FINISH"])
    graph = build_graph(llm)
    await graph.ainvoke(new_state("t", "q", "demo"), config=graph_config(4))
    system = llm.complete_calls[-1][0]["content"]
    assert "no cites ningún documento" in system
    assert "Los únicos documentos citables" not in system
