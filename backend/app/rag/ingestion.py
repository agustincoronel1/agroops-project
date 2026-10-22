import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.config import Settings
from app.infrastructure.chroma import (
    ChromaStore,
    chroma_errors,
    ensure_embedding_dimension,
)
from app.observability.tracing import ingest_trace
from app.rag.chunking import chunk_text
from app.rag.loaders import SUPPORTED_EXTENSIONS, PageText, load_document
from app.services.llm import Embedder

logger = logging.getLogger(__name__)

DEMO_DOCS_DIR = Path(__file__).parent / "demo_docs"


class IngestResult(BaseModel):
    source: str
    document_id: str
    chunks: int
    skipped: bool  # True when identical content was already indexed


def make_document_id(establishment_id: str, source: str) -> str:
    return hashlib.sha256(f"{establishment_id}/{source}".encode()).hexdigest()[:16]


def _supported_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)


def split_pages(pages: list[PageText], size: int, overlap: int) -> list[tuple[int | None, str]]:
    """(page number, chunk) for every chunk of every page. Pure and sync: use asyncio.to_thread."""
    return [(page.page, piece) for page in pages for piece in chunk_text(page.text, size, overlap)]


class IngestionService:
    """Idempotent ingestion: same establishment + file name + content = no-op."""

    def __init__(self, store: ChromaStore, embedder: Embedder, settings: Settings) -> None:
        self._store = store
        self._embedder = embedder
        self._settings = settings

    async def ingest_file(self, path: Path, establishment_id: str) -> IngestResult:
        async with ingest_trace(establishment_id, path.name) as trace_run:
            result = await self._ingest_file(path, establishment_id)
            trace_run.annotate(chunks=result.chunks, skipped=result.skipped)
            return result

    async def _ingest_file(self, path: Path, establishment_id: str) -> IngestResult:
        source = path.name
        document_id = make_document_id(establishment_id, source)
        # Sync file/PDF parsing runs in a thread so it never blocks the event loop.
        pages = await asyncio.to_thread(load_document, path)
        content_hash = hashlib.sha256(
            "\n".join(p.text for p in pages).encode()
        ).hexdigest()

        collection = await self._store.get_collection()
        async with chroma_errors():
            existing = await collection.get(
                where={
                    "$and": [
                        {"establishment_id": establishment_id},
                        {"document_id": document_id},
                    ]
                },
                include=["metadatas"],
            )
        metas = existing["metadatas"] or []
        if metas and all(m.get("content_hash") == content_hash for m in metas):
            return IngestResult(
                source=source, document_id=document_id, chunks=len(metas), skipped=True
            )

        ids: list[str] = []
        texts: list[str] = []
        metadatas: list[dict[str, Any]] = []
        # Chunking is CPU-bound on large documents: keep it off the event loop.
        pieces = await asyncio.to_thread(
            split_pages, pages, self._settings.rag_chunk_size, self._settings.rag_chunk_overlap
        )
        for page_number, piece in pieces:
            chunk_id = f"{document_id}-{len(ids):04d}"
            meta: dict[str, Any] = {
                "establishment_id": establishment_id,
                "source": source,
                "document_id": document_id,
                "chunk_id": chunk_id,
                "content_hash": content_hash,
            }
            if page_number is not None:
                meta["page"] = page_number
            ids.append(chunk_id)
            texts.append(piece)
            metadatas.append(meta)

        embeddings = await self._embedder.embed(texts) if texts else []
        if embeddings:
            # Check before deleting anything: an incompatible model must not drop old chunks.
            await ensure_embedding_dimension(
                collection, len(embeddings[0]), self._settings.embedding_model
            )
        # Content changed (or first time): replace any previous chunks of this document.
        async with chroma_errors():
            if existing["ids"]:
                await collection.delete(ids=existing["ids"])
            if ids:
                await collection.upsert(
                    ids=ids, documents=texts, embeddings=embeddings, metadatas=metadatas
                )
        logger.info("Ingested %s for %s: %d chunks", source, establishment_id, len(ids))
        return IngestResult(
            source=source, document_id=document_id, chunks=len(ids), skipped=False
        )

    async def ingest_directory(
        self, directory: Path, establishment_id: str
    ) -> list[IngestResult]:
        files = await asyncio.to_thread(_supported_files, directory)
        return [await self.ingest_file(p, establishment_id) for p in files]
