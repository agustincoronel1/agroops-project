import hashlib
import io
import math
from pathlib import Path
from typing import Any

import pytest
from app.agents.knowledge import NO_KNOWLEDGE_MESSAGE, knowledge_node
from app.core.config import Settings
from app.graph.builder import build_graph, graph_config
from app.graph.state import new_state
from app.rag.chunking import chunk_text
from app.rag.ingestion import DEMO_DOCS_DIR, IngestionService, make_document_id
from app.rag.loaders import load_document
from app.rag.retriever import ChromaRetriever
from pypdf import PdfWriter
from tests.test_graph import FakeLLM

DIM = 64


def _embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding (shared words => similar vectors)."""
    vec = [0.0] * DIM
    for word in text.lower().split():
        if len(word) < 5:  # ignore stopwords so scores reflect topical words
            continue
        idx = int(hashlib.md5(word.strip(".,:;").encode()).hexdigest(), 16) % DIM
        vec[idx] += 1.0
    return vec


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [_embed(t) for t in texts]


def _match(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
    if not where:
        return True
    if "$and" in where:
        return all(_match(meta, w) for w in where["$and"])
    return all(meta.get(k) == v for k, v in where.items())


def _cosine_distance(a: list[float], b: list[float]) -> float:
    na, nb = math.hypot(*a), math.hypot(*b)
    if not na or not nb:
        return 1.0
    return 1 - sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


class FakeCollection:
    """In-memory stand-in for a Chroma async collection (where + cosine)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def upsert(self, ids, documents, embeddings, metadatas) -> None:
        for i, d, e, m in zip(ids, documents, embeddings, metadatas, strict=True):
            self.rows[i] = {"doc": d, "emb": e, "meta": m}

    async def get(self, where=None, include=None, limit=None) -> dict[str, Any]:
        hits = {i: r for i, r in self.rows.items() if _match(r["meta"], where)}
        if limit is not None:
            hits = dict(list(hits.items())[:limit])
        return {
            "ids": list(hits),
            "documents": [r["doc"] for r in hits.values()],
            "metadatas": [r["meta"] for r in hits.values()],
            "embeddings": [r["emb"] for r in hits.values()],
        }

    async def delete(self, ids) -> None:
        for i in ids:
            self.rows.pop(i, None)

    async def query(self, query_embeddings, n_results, where, include) -> dict[str, Any]:
        scored = sorted(
            (
                (_cosine_distance(query_embeddings[0], r["emb"]), r)
                for r in self.rows.values()
                if _match(r["meta"], where)
            ),
            key=lambda t: t[0],
        )[:n_results]
        return {
            "documents": [[r["doc"] for _, r in scored]],
            "metadatas": [[r["meta"] for _, r in scored]],
            "distances": [[d for d, _ in scored]],
        }


class FakeStore:
    def __init__(self) -> None:
        self.collection = FakeCollection()

    async def get_collection(self) -> FakeCollection:
        return self.collection


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    return _settings(rag_chunk_size=300, rag_chunk_overlap=50, rag_max_distance=0.9)


@pytest.fixture
def parts(settings: Settings) -> tuple[FakeStore, FakeEmbedder, IngestionService]:
    store, embedder = FakeStore(), FakeEmbedder()
    return store, embedder, IngestionService(store, embedder, settings)  # type: ignore[arg-type]


def test_chunking_overlap_and_bounds() -> None:
    text = " ".join(f"palabra{i}" for i in range(200))
    chunks = chunk_text(text, 100, 20)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert set(chunks[0].split()[-2:]) & set(chunks[1].split())
    with pytest.raises(ValueError):
        chunk_text("x", 10, 10)


def test_loader_txt_and_unsupported(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hola", encoding="utf-8")
    pages = load_document(tmp_path / "a.txt")
    assert pages[0].text == "hola" and pages[0].page is None
    with pytest.raises(ValueError):
        load_document(tmp_path / "a.docx")


def test_loader_pdf_drops_empty_pages(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    buf = io.BytesIO()
    writer.write(buf)
    path = tmp_path / "blank.pdf"
    path.write_bytes(buf.getvalue())
    assert load_document(path) == []


@pytest.mark.anyio
async def test_ingest_metadata_and_idempotency(parts: Any) -> None:
    store, embedder, ingestion = parts
    first = await ingestion.ingest_directory(DEMO_DOCS_DIR, "demo")
    assert len(first) == 3 and all(not r.skipped and r.chunks > 0 for r in first)
    total, calls = len(store.collection.rows), embedder.calls

    for row in store.collection.rows.values():
        meta = row["meta"]
        assert {"establishment_id", "source", "document_id", "chunk_id"} <= set(meta)
        assert meta["establishment_id"] == "demo"

    second = await ingestion.ingest_directory(DEMO_DOCS_DIR, "demo")
    assert all(r.skipped for r in second)
    assert len(store.collection.rows) == total  # no duplicates
    assert embedder.calls == calls  # and no new embedding cost


@pytest.mark.anyio
async def test_reingest_changed_file_replaces_chunks(parts: Any, tmp_path: Path) -> None:
    store, _, ingestion = parts
    f = tmp_path / "nota.md"
    f.write_text("tractor aceite " * 80, encoding="utf-8")
    r1 = await ingestion.ingest_file(f, "demo")
    assert r1.chunks > 1
    f.write_text("cosechadora cuchillas", encoding="utf-8")
    r2 = await ingestion.ingest_file(f, "demo")
    assert not r2.skipped and r2.document_id == r1.document_id
    assert len(store.collection.rows) == r2.chunks == 1


@pytest.mark.anyio
async def test_retrieval_finds_relevant_chunk_with_sources(
    parts: Any, settings: Settings
) -> None:
    store, embedder, ingestion = parts
    await ingestion.ingest_directory(DEMO_DOCS_DIR, "demo")
    retriever = ChromaRetriever(store, embedder, settings)
    docs = await retriever.retrieve("triple lavado del tanque de la pulverizadora", "demo")
    assert docs and docs[0].establishment_id == "demo"
    assert docs[0].chunk_id.startswith(make_document_id("demo", docs[0].source))
    assert any("triple lavado" in d.content.lower() for d in docs)
    assert docs[0].distance is not None and docs[0].distance <= settings.rag_max_distance
    assert len(docs) <= settings.rag_top_k


@pytest.mark.anyio
async def test_retrieval_never_crosses_establishments(
    parts: Any, tmp_path: Path
) -> None:
    store, embedder, ingestion = parts
    secret = tmp_path / "secreto.md"
    secret.write_text("tractor confidencial de la finca B " * 5, encoding="utf-8")
    await ingestion.ingest_file(secret, "finca-b")
    await ingestion.ingest_directory(DEMO_DOCS_DIR, "demo")
    retriever = ChromaRetriever(store, embedder, _settings(rag_max_distance=2.0))
    for query, est in [("tractor confidencial finca B", "demo"), ("tractor", "finca-b")]:
        docs = await retriever.retrieve(query, est)
        assert docs and all(d.establishment_id == est for d in docs)
    demo_docs = await retriever.retrieve("tractor confidencial", "demo")
    assert all(d.source != "secreto.md" for d in demo_docs)
    assert await retriever.retrieve("tractor", "finca-inexistente") == []
    with pytest.raises(ValueError):
        await retriever.retrieve("tractor", "")


@pytest.mark.anyio
async def test_retrieval_discards_far_results(parts: Any) -> None:
    store, embedder, ingestion = parts
    await ingestion.ingest_directory(DEMO_DOCS_DIR, "demo")
    strict = ChromaRetriever(store, embedder, _settings(rag_max_distance=0.05))
    assert await strict.retrieve("zzz qqq xxx", "demo") == []


@pytest.mark.anyio
async def test_knowledge_node_without_results_does_not_invent() -> None:
    class Empty:
        async def retrieve(self, q: str, e: str) -> list:
            return []

    class ExplodingLLM:
        async def complete(self, messages: Any) -> str:
            raise AssertionError("LLM must not be called without evidence")

    out = await knowledge_node(new_state("t", "q", "demo"), ExplodingLLM(), Empty())  # type: ignore[arg-type]
    assert out["agent_outputs"]["knowledge"] == NO_KNOWLEDGE_MESSAGE
    assert out["context"]["sources"] == []


@pytest.mark.anyio
async def test_establishment_id_flows_through_graph(parts: Any, settings: Settings) -> None:
    store, embedder, ingestion = parts
    await ingestion.ingest_directory(DEMO_DOCS_DIR, "demo")
    retriever = ChromaRetriever(store, embedder, settings)

    graph = build_graph(FakeLLM(["knowledge", "FINISH"]), retriever)
    final = await graph.ainvoke(
        new_state("t1", "triple lavado pulverizadora", "demo"), config=graph_config(4)
    )
    assert final["result"]["sources"]
    assert all(s["source"].endswith("_demo.md") for s in final["result"]["sources"])

    graph = build_graph(FakeLLM(["knowledge", "FINISH"]), retriever)
    other = await graph.ainvoke(
        new_state("t2", "triple lavado pulverizadora", "otra-finca"), config=graph_config(4)
    )
    assert other["result"]["sources"] == []
    assert other["agent_outputs"]["knowledge"] == NO_KNOWLEDGE_MESSAGE


@pytest.mark.anyio
async def test_ingestion_does_not_block_the_event_loop(parts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: slow sync file/PDF parsing must run off the event loop."""
    import asyncio
    import time

    import app.rag.ingestion as ingestion_module

    doc = tmp_path / "lento.txt"
    doc.write_text("contenido de prueba " * 10, encoding="utf-8")
    real_load = ingestion_module.load_document

    def slow_load(path: Path) -> list:
        time.sleep(0.3)  # simulates a heavy PDF parse
        return real_load(path)

    monkeypatch.setattr(ingestion_module, "load_document", slow_load)
    service = parts[2]
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await service.ingest_file(doc, "finca-7")
    finally:
        task.cancel()
    assert ticks >= 10  # the loop kept running for the ~0.3 s parse


@pytest.mark.anyio
async def test_chunking_runs_off_the_event_loop(parts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: chunking a large document is CPU-bound and must run off the event loop."""
    import asyncio
    import time

    import app.rag.ingestion as ingestion_module

    doc = tmp_path / "grande.txt"
    doc.write_text("parrafo de manual " * 40, encoding="utf-8")
    real_split = ingestion_module.split_pages

    def slow_split(pages: list, size: int, overlap: int) -> list:
        time.sleep(0.3)  # simulates chunking a very large document
        return real_split(pages, size, overlap)

    monkeypatch.setattr(ingestion_module, "split_pages", slow_split)
    service = parts[2]
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        result = await service.ingest_file(doc, "finca-7")
    finally:
        task.cancel()
    assert result.chunks >= 1
    assert ticks >= 10


@pytest.mark.anyio
async def test_keyword_ranking_runs_off_the_event_loop(
    parts: Any, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: BM25 over the establishment corpus is CPU-bound and must run off the loop."""
    import asyncio
    import time

    import app.rag.retriever as retriever_module

    store, embedder, service = parts
    doc = tmp_path / "manual.txt"
    doc.write_text("triple lavado del tanque de la pulverizadora " * 5, encoding="utf-8")
    await service.ingest_file(doc, "demo")

    real_order = retriever_module.keyword_order

    def slow_order(query: str, documents: list[str], top_k: int) -> list[int]:
        time.sleep(0.3)  # simulates BM25 over a large corpus
        return real_order(query, documents, top_k)

    monkeypatch.setattr(retriever_module, "keyword_order", slow_order)
    retriever = ChromaRetriever(store, embedder, settings)  # type: ignore[arg-type]
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        docs = await retriever.retrieve("lavado del tanque", "demo")
    finally:
        task.cancel()
    assert docs and docs[0].establishment_id == "demo"
    assert ticks >= 10
