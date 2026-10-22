"""Integration tests against a REAL Chroma server.

Opt-in: set AGROOPS_INTEGRATION=1 (and optionally AGROOPS_TEST_CHROMA_HOST / _PORT).
Each test writes to its own temporary collection, which is deleted afterwards, so
production collections are never touched. Embeddings are local and deterministic:
no OpenAI calls, no credits.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import chromadb
import pytest
from app.core.config import Settings
from app.core.errors import EmbeddingDimensionMismatch, KnowledgeBaseUnavailable, public_error
from app.infrastructure.chroma import ChromaStore
from app.rag.ingestion import IngestionService
from app.rag.retriever import ChromaRetriever
from tests.test_rag import FakeEmbedder

HOST = os.getenv("AGROOPS_TEST_CHROMA_HOST", "localhost")
PORT = int(os.getenv("AGROOPS_TEST_CHROMA_PORT", "8001"))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("AGROOPS_INTEGRATION") != "1", reason="set AGROOPS_INTEGRATION=1"),
]


@pytest.fixture(autouse=True)
def no_langsmith_traces() -> None:
    """QA guard: integration runs must never send traces to LangSmith."""
    from app.observability.tracing import tracing_enabled

    if tracing_enabled() or os.getenv("LANGSMITH_TRACING", "false").lower() == "true":
        pytest.fail("refusing to run integration tests with LangSmith tracing enabled")
    if os.getenv("LANGSMITH_API_KEY"):
        pytest.fail("refusing to run integration tests while LANGSMITH_API_KEY is set")


@pytest.fixture
def settings() -> Iterator[Settings]:
    name = f"itest_{uuid.uuid4().hex[:10]}"
    yield Settings(  # type: ignore[call-arg]
        _env_file=None,
        chroma_host=HOST,
        chroma_port=PORT,
        chroma_collection=name,
        rag_max_distance=2.0,
        rag_chunk_size=300,
        rag_chunk_overlap=30,
    )
    try:
        chromadb.HttpClient(host=HOST, port=PORT).delete_collection(name)
    except Exception:  # noqa: BLE001 - cleanup must not hide the test result
        pass


class SizedEmbedder:
    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


@pytest.mark.anyio
async def test_write_read_and_hybrid_search_on_real_chroma(settings: Settings, tmp_path: Path) -> None:
    store = ChromaStore(settings)
    embedder = FakeEmbedder()
    ingestion = IngestionService(store, embedder, settings)  # type: ignore[arg-type]
    mine = tmp_path / "lote.md"
    mine.write_text("Lote L7731 riego por goteo los martes por la mañana.", encoding="utf-8")
    other = tmp_path / "vecino.md"
    other.write_text("Lote L7731 registrado en la finca vecina.", encoding="utf-8")
    first = await ingestion.ingest_file(mine, "finca-h")
    await ingestion.ingest_file(other, "finca-vecina")
    assert first.chunks >= 1 and first.skipped is False

    again = await ingestion.ingest_file(mine, "finca-h")
    assert again.skipped is True  # idempotent on real storage too

    collection = await store.get_collection()
    stored = await collection.get(where={"establishment_id": "finca-h"}, include=["metadatas"])
    assert stored["metadatas"] and {"source", "document_id", "chunk_id"} <= set(stored["metadatas"][0])

    keyword_only = ChromaRetriever(store, embedder, settings.model_copy(update={"rag_max_distance": 0.0}))  # type: ignore[arg-type]
    docs = await keyword_only.retrieve("L7731", "finca-h")
    assert [d.source for d in docs] == ["lote.md"]
    assert docs[0].retrieval == "keyword" and docs[0].establishment_id == "finca-h"

    vector_and_keyword = ChromaRetriever(store, embedder, settings)  # type: ignore[arg-type]
    docs = await vector_and_keyword.retrieve("riego por goteo lote L7731", "finca-h")
    assert docs and all(d.establishment_id == "finca-h" for d in docs)
    assert docs[0].retrieval == "hybrid"


@pytest.mark.anyio
async def test_incompatible_dimension_is_refused_without_data_loss(settings: Settings, tmp_path: Path) -> None:
    store = ChromaStore(settings)
    doc = tmp_path / "manual.md"
    doc.write_text("triple lavado del equipo " * 8, encoding="utf-8")
    await IngestionService(store, FakeEmbedder(), settings).ingest_file(doc, "demo")  # type: ignore[arg-type]
    collection = await store.get_collection()
    before = await collection.count()

    other = tmp_path / "otro.md"
    other.write_text("riego por goteo " * 8, encoding="utf-8")
    service = IngestionService(store, SizedEmbedder(32), settings)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingDimensionMismatch):
        await service.ingest_file(other, "demo")
    assert await collection.count() == before


@pytest.mark.anyio
async def test_unreachable_chroma_gives_a_public_error_without_internals() -> None:
    unreachable = Settings(  # type: ignore[call-arg]
        _env_file=None, chroma_host="127.0.0.1", chroma_port=1, chroma_collection="nope"
    )
    with pytest.raises(KnowledgeBaseUnavailable) as info:
        await ChromaStore(unreachable).get_collection()
    text = public_error(info.value)
    assert text.startswith("KNOWLEDGE_UNAVAILABLE: ")
    assert "127.0.0.1" not in text and ":1" not in text
