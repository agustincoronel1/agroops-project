"""LangSmith tracing tests. MOCKED: a recording Client replaces the network (no traces sent)."""

import logging
import os
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.config import Settings
from app.graph.builder import build_graph
from app.observability.tracing import configure_tracing, job_trace, tracing_enabled
from app.rag.retriever import ChromaRetriever
from app.schemas.tasks import JobStatus
from app.services.graph_processor import make_graph_processor
from app.services.job_service import JobService
from app.services.llm import OpenAILLM
from app.services.task_worker import TaskWorker
from langsmith import Client, traceable, tracing_context
from langsmith import utils as ls_utils
from tests.test_graph import FakeLLM
from tests.test_tasks import FakeRedis

SECRET = "lsv2_pt_SECRET_SHOULD_NEVER_APPEAR"
OPENAI_SECRET = "sk-SECRET-OPENAI-KEY"


class RecordingClient(Client):
    """Records every run create/update instead of calling LangSmith."""

    def __init__(self) -> None:
        super().__init__(api_url="http://localhost:1", api_key="test", auto_batch_tracing=False)
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def create_run(self, name: str, inputs: Any, run_type: str, **kwargs: Any) -> None:  # type: ignore[override]
        self.created.append({"name": name, "inputs": inputs, "run_type": run_type, **kwargs})

    def update_run(self, run_id: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self.updated.append({"id": str(run_id), **kwargs})

    def runs(self) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for r in self.created:
            merged[str(r["id"])] = dict(r)
        for u in self.updated:
            merged.setdefault(u["id"], {}).update(u)
        return {r["name"] + ":" + k: r for k, r in merged.items()}

    def by_name(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.runs().values() if r.get("name") == name]


def _metadata(run: dict[str, Any]) -> dict[str, Any]:
    return ((run.get("extra") or {}).get("metadata")) or {}


def _restore_env(snapshot: dict[str, str | None]) -> None:
    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for name in ("get_env_var", "get_tracer_project"):
        fn = getattr(ls_utils, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()


@pytest.fixture
def env_snapshot() -> Any:
    keys = ["LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT"]
    snap = {k: os.environ.get(k) for k in keys}
    yield
    _restore_env(snap)


def test_tracing_disabled_by_default(env_snapshot: Any) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.langsmith_tracing is False
    assert configure_tracing(settings) is False
    assert tracing_enabled() is False


def test_tracing_requires_api_key(env_snapshot: Any) -> None:
    settings = Settings(langsmith_tracing=True, _env_file=None)  # type: ignore[call-arg]
    assert configure_tracing(settings) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_tracing_enabled_with_key_exports_env(env_snapshot: Any) -> None:
    settings = Settings(
        langsmith_tracing=True, langsmith_api_key=SECRET, langsmith_project="p", _env_file=None  # type: ignore[call-arg]
    )
    assert configure_tracing(settings) is True
    assert os.environ["LANGSMITH_PROJECT"] == "p"
    assert SECRET not in repr(settings)


@pytest.mark.anyio
async def test_job_trace_is_noop_when_disabled() -> None:
    job = SimpleNamespace(job_id="j", establishment_id="demo", approval=None)
    async with job_trace(job) as handle:
        handle.set_status("done")  # must not raise


@pytest.mark.anyio
async def test_worker_trace_nests_graph_nodes_with_job_metadata() -> None:
    client = RecordingClient()
    redis = FakeRedis()
    jobs = JobService(redis)  # type: ignore[arg-type]
    graph = build_graph(FakeLLM(["operations", "FINISH"]))
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    job = await jobs.create_job("¿maquinaria rota?", establishment_id="finca-7")
    with tracing_context(enabled=True, client=client):
        await worker.process_job(job.job_id)
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.status is JobStatus.DONE

    [root] = client.by_name("agroops.job")
    meta = _metadata(root)
    assert meta["job_id"] == job.job_id
    assert meta["thread_id"] == job.job_id
    assert meta["establishment_id"] == "finca-7"
    assert meta["job_status"] == "done"
    assert root.get("parent_run_id") is None

    [graph_run] = client.by_name("agroops.graph")
    assert str(graph_run["parent_run_id"]) == str(root["id"])
    assert _metadata(graph_run)["job_id"] == job.job_id
    names = {r["name"] for r in client.runs().values()}
    assert {"supervisor", "operations"} <= names
    for node in client.by_name("supervisor") + client.by_name("operations"):
        assert _metadata(node)["job_id"] == job.job_id  # inherited


@pytest.mark.anyio
async def test_failed_job_marks_trace_failed() -> None:
    class Broken(FakeLLM):
        async def parse(self, messages: Any, response_model: Any) -> Any:
            raise TimeoutError("down")

    client = RecordingClient()
    jobs = JobService(FakeRedis())  # type: ignore[arg-type]
    worker = TaskWorker(jobs, make_graph_processor(build_graph(Broken([])), 4))
    job = await jobs.create_job("x")
    with tracing_context(enabled=True, client=client):
        await worker.process_job(job.job_id)
    [root] = client.by_name("agroops.job")
    assert _metadata(root)["job_status"] == "failed"
    assert root.get("error")


class _Embeddings:
    async def create(self, *, model: str, input: list[str]) -> Any:
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in input],
            usage=SimpleNamespace(prompt_tokens=7, total_tokens=7),
        )


@pytest.mark.anyio
async def test_embeddings_and_retriever_traced_with_real_usage_no_secrets() -> None:
    client = RecordingClient()
    llm = OpenAILLM(Settings(openai_api_key=OPENAI_SECRET, _env_file=None))  # type: ignore[call-arg]
    llm._client = SimpleNamespace(embeddings=_Embeddings())  # type: ignore[assignment]

    class Collection:
        async def query(self, **_: Any) -> dict[str, Any]:
            return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

        async def get(self, **_: Any) -> dict[str, Any]:
            return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}

    class Store:
        async def get_collection(self) -> Collection:
            return Collection()

    retriever = ChromaRetriever(Store(), llm, Settings(_env_file=None))  # type: ignore[arg-type,call-arg]
    with tracing_context(enabled=True, client=client):
        await retriever.retrieve("hola", "finca-7")

    [ret] = client.by_name("knowledge.retrieve")
    assert ret["run_type"] == "retriever"
    [emb] = client.by_name("openai.embeddings")
    assert emb["run_type"] == "llm" and "embedding" in emb["tags"]
    assert str(emb["parent_run_id"]) == str(ret["id"])
    blob = repr(client.created) + repr(client.updated)
    assert OPENAI_SECRET not in blob and SECRET not in blob
    usage = (emb.get("extra") or {}).get("metadata", {}).get("usage_metadata") or emb.get("usage_metadata")
    assert usage and usage["input_tokens"] == 7


# --- HITL, ingestion, real OpenAI SDK path (mock HTTP transport), failure isolation ---------


def _names(client: RecordingClient) -> set[str]:
    return {r["name"] for r in client.runs().values()}


@pytest.mark.anyio
async def test_hitl_waiting_and_resume_share_thread_and_trace_approval() -> None:
    from app.schemas.tasks import ApprovalRequest
    from app.services.incident_service import IncidentService
    from langgraph.checkpoint.memory import InMemorySaver
    from tests.test_hitl import HitlLLM

    client = RecordingClient()
    redis, saver = FakeRedis(), InMemorySaver()
    jobs = JobService(redis)  # type: ignore[arg-type]
    graph = build_graph(
        HitlLLM(["register_incident", "FINISH", "FINISH"]),
        None,
        4,
        checkpointer=saver,
        incidents=IncidentService(redis),  # type: ignore[arg-type]
    )
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    job = await jobs.create_job("registrar incidencia en la cosechadora", "finca-9")

    with tracing_context(enabled=True, client=client):
        await worker.process_job(job.job_id)
    assert (await jobs.get_job(job.job_id)).status is JobStatus.WAITING_APPROVAL  # type: ignore[union-attr]
    assert {"prepare_incident", "approval"} <= _names(client)

    await jobs.submit_approval(job.job_id, ApprovalRequest(approve=True))
    queued = await jobs.dequeue()
    with tracing_context(enabled=True, client=client):
        await worker.process_job(queued)  # type: ignore[arg-type]
    assert (await jobs.get_job(job.job_id)).status is JobStatus.DONE  # type: ignore[union-attr]

    roots = sorted(client.by_name("agroops.job"), key=lambda r: str(r["id"]))
    assert len(roots) == 2
    statuses = [_metadata(r)["job_status"] for r in roots]
    assert statuses.count("waiting_approval") == 1 and statuses.count("done") == 1
    for root in roots:
        meta = _metadata(root)
        assert meta["job_id"] == job.job_id and meta["thread_id"] == job.job_id
        assert meta["establishment_id"] == "finca-9"
    resumed = [_metadata(r)["resumed"] for r in roots]
    assert resumed.count(True) == 1 and resumed.count(False) == 1
    # approval ran twice: interrupted in the first execution, executed after the resume
    approvals = client.by_name("approval")
    assert len(approvals) == 2
    assert all(_metadata(a)["job_id"] == job.job_id for a in approvals)


class _Boom(RecordingClient):
    def create_run(self, *a: Any, **k: Any) -> None:  # type: ignore[override]
        raise ConnectionError("langsmith unreachable")

    def update_run(self, *a: Any, **k: Any) -> None:  # type: ignore[override]
        raise ConnectionError("langsmith unreachable")


@pytest.mark.anyio
async def test_langsmith_unreachable_does_not_break_job() -> None:
    # Real client, default background batching, endpoint that refuses connections.
    unreachable = Client(api_url="http://127.0.0.1:9", api_key="test")
    jobs = JobService(FakeRedis())  # type: ignore[arg-type]
    graph = build_graph(FakeLLM(["operations", "FINISH"]))
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    job = await jobs.create_job("consulta")
    ls_logger = logging.getLogger("langsmith")
    previous = ls_logger.level
    ls_logger.setLevel(logging.CRITICAL)  # the batch thread logs every failed POST
    try:
        with tracing_context(enabled=True, client=unreachable):
            await worker.process_job(job.job_id)
    finally:
        ls_logger.setLevel(previous)
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.status is JobStatus.DONE


@pytest.mark.anyio
async def test_failure_opening_root_trace_does_not_break_job() -> None:
    jobs = JobService(FakeRedis())  # type: ignore[arg-type]
    graph = build_graph(FakeLLM(["operations", "FINISH"]))
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    job = await jobs.create_job("consulta")
    with tracing_context(enabled=True, client=_Boom()):
        await worker.process_job(job.job_id)
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.status is JobStatus.DONE


@pytest.mark.anyio
async def test_tracing_disabled_sends_nothing() -> None:
    client = RecordingClient()
    jobs = JobService(FakeRedis())  # type: ignore[arg-type]
    graph = build_graph(FakeLLM(["operations", "FINISH"]))
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    job = await jobs.create_job("consulta")
    assert not tracing_enabled()  # conftest forces LANGSMITH_TRACING=false
    await worker.process_job(job.job_id)  # no tracing_context: env says off
    assert client.created == [] and client.updated == []
    assert (await jobs.get_job(job.job_id)).status is JobStatus.DONE  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_ingestion_is_a_root_run_tagged_with_establishment(tmp_path: Any) -> None:
    from app.rag.ingestion import IngestionService
    from tests.test_rag import FakeEmbedder, FakeStore

    doc = tmp_path / "manual.md"
    doc.write_text("Procedimiento de limpieza de la cosechadora. " * 20, encoding="utf-8")
    client = RecordingClient()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    service = IngestionService(FakeStore(), FakeEmbedder(), settings)  # type: ignore[arg-type]
    with tracing_context(enabled=True, client=client):
        first = await service.ingest_file(doc, "finca-7")
        second = await service.ingest_file(doc, "finca-7")
    assert first.skipped is False and second.skipped is True

    roots = client.by_name("agroops.ingest")
    assert len(roots) == 2 and all(r.get("parent_run_id") is None for r in roots)
    assert all(_metadata(r)["establishment_id"] == "finca-7" for r in roots)
    assert sorted(_metadata(r)["skipped"] for r in roots) == [False, True]


@pytest.mark.anyio
async def test_chat_and_embedding_calls_nest_under_job_with_real_sdk_mock_transport() -> None:
    import httpx
    from app.observability.tracing import wrap_openai_client
    from app.services.llm import OpenAILLM
    from openai import AsyncOpenAI

    chat_body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hola"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }
    embed_body = {
        "object": "list",
        "model": "text-embedding-3-small",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = chat_body if request.url.path.endswith("/chat/completions") else embed_body
        return httpx.Response(200, json=body)

    client = RecordingClient()
    settings = Settings(openai_api_key=OPENAI_SECRET, _env_file=None)  # type: ignore[call-arg]
    llm = OpenAILLM(settings)

    @traceable(run_type="chain", name="test.job")
    async def job() -> str:
        text = await llm.complete([{"role": "user", "content": "hola"}])
        await llm.embed(["hola"])
        return text

    with tracing_context(enabled=True, client=client):
        # Created while tracing is on, as in production (client is lazy and env-driven).
        sdk = AsyncOpenAI(
            api_key=OPENAI_SECRET,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        llm._client = wrap_openai_client(sdk)
        assert await job() == "hola"

    [root] = client.by_name("test.job")
    [chat] = client.by_name("openai.chat")
    [emb] = client.by_name("openai.embeddings")
    assert str(chat["parent_run_id"]) == str(root["id"])
    assert str(emb["parent_run_id"]) == str(root["id"])
    assert chat["run_type"] == "llm" and emb["run_type"] == "llm"
    chat_meta = _metadata(chat)
    assert chat_meta["ls_provider"] == "openai" and chat_meta["ls_model_name"] == "gpt-4o-mini"
    assert chat_meta["usage_metadata"]["input_tokens"] == 11
    assert chat_meta["usage_metadata"]["output_tokens"] == 3
    assert _metadata(emb)["usage_metadata"]["input_tokens"] == 7
    blob = repr(client.created) + repr(client.updated)
    assert OPENAI_SECRET not in blob


# --- Root run output (regression: the agroops.job root used to be saved with "No outputs") ---


def _root_outputs(client: RecordingClient, job_status: str) -> dict[str, Any]:
    [root] = [r for r in client.by_name("agroops.job") if _metadata(r).get("job_status") == job_status]
    assert root.get("outputs") is not None, "root run must carry outputs"
    return root["outputs"]


@pytest.mark.anyio
async def test_done_root_output_matches_api_result_without_content() -> None:
    client = RecordingClient()
    jobs = JobService(FakeRedis())  # type: ignore[arg-type]
    worker = TaskWorker(jobs, make_graph_processor(build_graph(FakeLLM(["operations", "FINISH"])), 4))
    job = await jobs.create_job("¿cómo va el lote 3?", "finca-7")
    with tracing_context(enabled=True, client=client):
        await worker.process_job(job.job_id)
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.result is not None

    out = _root_outputs(client, "done")
    assert out == {
        "job_id": job.job_id,
        "status": "DONE",
        "agents_consulted": ["operations"],
        "sources_count": len(final.result["sources"]),
        "action_status": None,
        "has_answer": True,
        "error_type": None,
    }
    assert final.result["agents_consulted"] == out["agents_consulted"]
    # no answer text, prompts or user question inside the trace output
    blob = repr(out)
    assert "completion-1" not in blob and "lote 3" not in blob


@pytest.mark.anyio
async def test_failed_root_output_has_error_type_only() -> None:
    class Broken(FakeLLM):
        async def parse(self, messages: Any, response_model: Any) -> Any:
            raise TimeoutError("simulated upstream timeout with a private detail")

    client = RecordingClient()
    jobs = JobService(FakeRedis())  # type: ignore[arg-type]
    worker = TaskWorker(jobs, make_graph_processor(build_graph(Broken([])), 4))
    job = await jobs.create_job("x")
    with tracing_context(enabled=True, client=client):
        await worker.process_job(job.job_id)
    assert (await jobs.get_job(job.job_id)).status is JobStatus.FAILED  # type: ignore[union-attr]

    out = _root_outputs(client, "failed")
    assert out["status"] == "FAILED" and out["error_type"] == "TimeoutError"
    assert out["has_answer"] is False and out["sources_count"] == 0
    assert "private detail" not in repr(out)


@pytest.mark.anyio
async def test_waiting_and_resumed_root_outputs_follow_hitl() -> None:
    from app.schemas.tasks import ApprovalRequest
    from app.services.incident_service import IncidentService
    from langgraph.checkpoint.memory import InMemorySaver
    from tests.test_hitl import HitlLLM

    client = RecordingClient()
    redis, saver = FakeRedis(), InMemorySaver()
    jobs = JobService(redis)  # type: ignore[arg-type]
    graph = build_graph(
        HitlLLM(["operations", "register_incident", "FINISH", "FINISH"]),
        None,
        4,
        checkpointer=saver,
        incidents=IncidentService(redis),  # type: ignore[arg-type]
    )
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    job = await jobs.create_job("consulta y registro de incidencia", "finca-9")
    with tracing_context(enabled=True, client=client):
        await worker.process_job(job.job_id)

    waiting = _root_outputs(client, "waiting_approval")
    assert waiting == {
        "job_id": job.job_id,
        "status": "WAITING_APPROVAL",
        "agents_consulted": ["operations"],
        "sources_count": 0,
        "action_status": "PENDING_APPROVAL",
        "has_answer": False,
        "error_type": None,
    }

    await jobs.submit_approval(job.job_id, ApprovalRequest(approve=True))
    queued = await jobs.dequeue()
    with tracing_context(enabled=True, client=client):
        await worker.process_job(queued)  # type: ignore[arg-type]

    resumed = _root_outputs(client, "done")
    assert resumed["status"] == "DONE" and resumed["action_status"] == "APPLIED"
    assert resumed["has_answer"] is True and resumed["job_id"] == job.job_id
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.result is not None
    assert resumed["agents_consulted"] == sorted(final.result["agents_consulted"])
    assert "operations" in resumed["agents_consulted"]
    assert resumed["sources_count"] == len(final.result["sources"])


@pytest.mark.anyio
async def test_root_output_is_noop_when_tracing_disabled() -> None:
    client = RecordingClient()
    jobs = JobService(FakeRedis())  # type: ignore[arg-type]
    worker = TaskWorker(jobs, make_graph_processor(build_graph(FakeLLM(["operations", "FINISH"])), 4))
    job = await jobs.create_job("consulta")
    await worker.process_job(job.job_id)  # tracing off: must not raise or send anything
    assert client.created == [] and client.updated == []
