import hashlib
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.config import Settings
from app.infrastructure.chroma import ChromaStore
from app.rag.chunking import chunk_text
from app.rag.loaders import SUPPORTED_EXTENSIONS, load_document
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


class IngestionService:
    """Idempotent ingestion: same establishment + file name + content = no-op."""

    def __init__(self, store: ChromaStore, embedder: Embedder, settings: Settings) -> None:
        self._store = store
        self._embedder = embedder
        self._settings = settings

    async def ingest_file(self, path: Path, establishment_id: str) -> IngestResult:
        source = path.name
        document_id = make_document_id(establishment_id, source)
        pages = load_document(path)
        content_hash = hashlib.sha256(
            "\n".join(p.text for p in pages).encode()
        ).hexdigest()

        collection = await self._store.get_collection()
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
        for page in pages:
            for piece in chunk_text(
                page.text, self._settings.rag_chunk_size, self._settings.rag_chunk_overlap
            ):
                chunk_id = f"{document_id}-{len(ids):04d}"
                meta: dict[str, Any] = {
                    "establishment_id": establishment_id,
                    "source": source,
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "content_hash": content_hash,
                }
                if page.page is not None:
                    meta["page"] = page.page
                ids.append(chunk_id)
                texts.append(piece)
                metadatas.append(meta)

        embeddings = await self._embedder.embed(texts) if texts else []
        # Content changed (or first time): replace any previous chunks of this document.
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
        files = sorted(
            p for p in directory.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        return [await self.ingest_file(p, establishment_id) for p in files]
