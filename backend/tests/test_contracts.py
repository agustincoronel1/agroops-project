"""Contracts: embedding dimensions, public errors, Pydantic boundaries and citations."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import app.infrastructure.chroma as chroma_module
import httpx
import openai
import pytest
from app.agents.knowledge import KnowledgeAnswer
from app.agents.operations import operations_node
from app.agents.supervisor import strip_unknown_citations
from app.core.config import Settings
from app.core.errors import (
    AgentOutputInvalid,
    EmbeddingDimensionMismatch,
    KnowledgeBaseUnavailable,
    LLMUnavailable,
    ServiceError,
    public_error,
)
from app.graph.state import new_state
from app.infrastructure.chroma import ChromaStore
from app.rag.ingestion import IngestionService
from app.rag.retriever import ChromaRetriever
from app.schemas.agents import AgentInput, JobResult, OperationsAnswer, SourceRef
from app.schemas.tasks import JobStatus
from app.services.job_service import JobService
from app.services.llm import OpenAILLM
from app.services.task_worker import TaskWorker
from pydantic import ValidationError
from tests.test_rag import FakeEmbedder, FakeStore
from tests.test_tasks import FakeRedis


class SizedEmbedder:
    """Deterministic embedder with a configurable vector size."""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# --- 3. embedding dimension compatibility ---------------------------------------------
@pytest.mark.anyio
async def test_ingest_with_other_dimension_fails_and_keeps_existing_chunks(tmp_path: Path) -> None:
    store = FakeStore()
    doc = tmp_path / "manual.md"
    doc.write_text("triple lavado del equipo " * 10, encoding="utf-8")
    await IngestionService(store, FakeEmbedder(), _settings()).ingest_file(doc, "demo")  # type: ignore[arg-type]
    before = dict(store.collection.rows)

    service = IngestionService(store, SizedEmbedder(32), _settings())  # type: ignore[arg-type]
    other = tmp_path / "otro.md"
    other.write_text("riego por goteo " * 10, encoding="utf-8")
    with pytest.raises(EmbeddingDimensionMismatch) as info:
        await service.ingest_file(other, "demo")
    assert info.value.code == "EMBEDDING_DIMENSION_MISMATCH"
    assert "64-d" in str(info.value) and "32-d" in str(info.value)
    assert store.collection.rows == before  # nothing was deleted or written


@pytest.mark.anyio
async def test_retrieval_with_other_dimension_fails_with_controlled_error() -> None:
    store = FakeStore()
    await IngestionService(store, FakeEmbedder(), _settings()).ingest_file(  # type: ignore[arg-type]
        _write_tmp("a.md", "tractor aceite " * 10), "demo"
    )
    retriever = ChromaRetriever(store, SizedEmbedder(32), _settings())  # type: ignore[arg-type]
    with pytest.raises(EmbeddingDimensionMismatch):
        await retriever.retrieve("tractor", "demo")


@pytest.mark.anyio
async def test_empty_collection_accepts_any_dimension(tmp_path: Path) -> None:
    store = FakeStore()
    doc = tmp_path / "nota.md"
    doc.write_text("cosechadora cuchillas " * 5, encoding="utf-8")
    result = await IngestionService(store, SizedEmbedder(16), _settings()).ingest_file(  # type: ignore[arg-type]
        doc, "demo"
    )
    assert result.chunks >= 1


def _write_tmp(name: str, text: str) -> Path:
    import tempfile

    path = Path(tempfile.mkdtemp()) / name
    path.write_text(text, encoding="utf-8")
    return path


# --- 4. public errors never expose internals --------------------------------------------
def test_public_error_shows_code_and_message_only() -> None:
    error = KnowledgeBaseUnavailable("connect to secret-host:8001 failed")
    text = public_error(error)
    assert text.startswith("KNOWLEDGE_UNAVAILABLE: ")
    assert "secret-host" not in text


def test_unexpected_exception_becomes_generic_internal_error() -> None:
    text = public_error(RuntimeError("password=hunter2 in traceback"))
    assert text.startswith("INTERNAL_ERROR: ")
    assert "hunter2" not in text and "RuntimeError" not in text


@pytest.mark.anyio
async def test_unreachable_chroma_raises_controlled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(**_: Any) -> None:
        raise httpx.ConnectError("[Errno -2] Name or service not known: chroma-internal")

    monkeypatch.setattr(chroma_module.chromadb, "AsyncHttpClient", refuse)
    store = ChromaStore(_settings(chroma_host="chroma-internal", chroma_port=8000))
    with pytest.raises(KnowledgeBaseUnavailable) as info:
        await store.get_collection()
    assert "chroma-internal" not in public_error(info.value)


@pytest.mark.anyio
async def test_llm_provider_error_is_controlled() -> None:
    class Completions:
        async def create(self, **_: Any) -> None:
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://internal"))

    llm = OpenAILLM(_settings(openai_api_key="sk-test"))  # type: ignore[arg-type]
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))  # type: ignore[assignment]
    with pytest.raises(LLMUnavailable) as info:
        await llm.complete([{"role": "user", "content": "hola"}])
    assert "internal" not in public_error(info.value)


@pytest.mark.anyio
async def test_worker_stores_only_the_public_message_for_service_errors() -> None:
    async def processor(job: Any) -> dict[str, Any]:
        raise KnowledgeBaseUnavailable("chroma at 10.0.0.7 refused")

    jobs = JobService(FakeRedis())  # type: ignore[arg-type]
    job = await jobs.create_job("hola")
    await TaskWorker(jobs, processor).process_job(job.job_id)
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.status is JobStatus.FAILED
    assert final.error == public_error(KnowledgeBaseUnavailable())
    assert "10.0.0.7" not in (final.error or "")


# --- 2. Pydantic at the agent boundaries -------------------------------------------------
def test_agent_input_rejects_bad_establishment_and_empty_input() -> None:
    with pytest.raises(ValidationError):
        AgentInput(user_input="hola", establishment_id="../etc")
    with pytest.raises(ValidationError):
        AgentInput(user_input="", establishment_id="demo")


def test_knowledge_answer_requires_text_when_answered() -> None:
    with pytest.raises(ValidationError):
        KnowledgeAnswer(answered=True, answer="   ", supporting_passages=[1])
    assert KnowledgeAnswer(answered=False, answer="", supporting_passages=[]).answered is False


def test_source_ref_and_job_result_validate_shapes() -> None:
    with pytest.raises(ValidationError):
        SourceRef(source="a.md", document_id="d", chunk_id="c", retrieval="magic")
    with pytest.raises(ValidationError):
        JobResult(answer="")
    ok = JobResult(answer="respuesta", sources=[SourceRef(source="a.md", document_id="d", chunk_id="c")])
    assert ok.model_dump(mode="json")["sources"][0]["retrieval"] == "vector"


@pytest.mark.anyio
async def test_operations_rejects_empty_llm_output() -> None:
    class EmptyLLM:
        async def complete(self, messages: Any) -> str:
            return "   "

    with pytest.raises(AgentOutputInvalid) as info:
        await operations_node(new_state("t", "tareas", "demo"), EmptyLLM())  # type: ignore[arg-type]
    assert info.value.code == "AGENT_OUTPUT_INVALID"
    assert OperationsAnswer(answer=" ok ").answer == "ok"


def test_service_error_detail_is_not_public() -> None:
    error = AgentOutputInvalid("internal detail")
    assert isinstance(error, ServiceError)
    assert str(error) == "internal detail" and "internal detail" not in public_error(error)


# --- 1. citations are verifiable -------------------------------------------------------------
def test_unknown_file_citations_are_removed_and_known_ones_kept() -> None:
    text = "Según [procedimientos.md] y [otro.md], y [Knowledge Agent] dice algo."
    cleaned = strip_unknown_citations(text, {"procedimientos.md"})
    assert "[procedimientos.md]" in cleaned
    assert "[otro.md]" not in cleaned
    assert "[Knowledge Agent]" in cleaned  # not a file name: left alone


@pytest.mark.anyio
async def test_llm_decisions_are_requested_with_temperature_zero() -> None:
    """Same input must give the same routing / answered decision (reproducibility)."""
    seen: dict[str, Any] = {}

    class Completions:
        async def create(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

        async def parse(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            parsed = KnowledgeAnswer(answered=False, answer="", supporting_passages=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])

    llm = OpenAILLM(_settings(openai_api_key="sk-test"))  # type: ignore[arg-type]
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))  # type: ignore[assignment]
    await llm.complete([{"role": "user", "content": "hola"}])
    assert seen["temperature"] == 0
    await llm.parse([{"role": "user", "content": "hola"}], KnowledgeAnswer)
    assert seen["temperature"] == 0


def test_orphaned_job_message_is_public_and_has_no_exception_name() -> None:
    from app.services.job_service import ORPHANED_JOB_ERROR

    assert ORPHANED_JOB_ERROR.startswith("INTERRUPTED: ")
    assert "Error" not in ORPHANED_JOB_ERROR
