import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import chromadb
import httpx
from chromadb.api.models.AsyncCollection import AsyncCollection
from chromadb.errors import ChromaError

from app.core.config import Settings
from app.core.errors import EmbeddingDimensionMismatch, KnowledgeBaseUnavailable

logger = logging.getLogger(__name__)


@asynccontextmanager
async def chroma_errors() -> AsyncIterator[None]:
    """Turn Chroma/network failures into a controlled error. Details go to the log only."""
    try:
        yield
    except (OSError, httpx.HTTPError) as exc:
        logger.warning("Chroma unreachable: %s", exc)
        raise KnowledgeBaseUnavailable(f"chroma unreachable: {exc}") from exc
    except ChromaError as exc:
        logger.warning("Chroma error: %s", exc)
        raise KnowledgeBaseUnavailable(f"chroma error: {exc}") from exc


async def ensure_embedding_dimension(
    collection: AsyncCollection, dimension: int, model: str
) -> None:
    """Refuse vectors whose size differs from the one already stored in the collection.

    An empty collection has no dimension yet, so the first write defines it.
    """
    async with chroma_errors():
        sample: Any = await collection.get(limit=1, include=["embeddings"])
    stored = sample.get("embeddings")
    if stored is None or len(stored) == 0:
        return
    stored_dimension = len(stored[0])
    if stored_dimension != dimension:
        raise EmbeddingDimensionMismatch(
            f"collection stores {stored_dimension}-d vectors, model {model} produces {dimension}-d"
        )


class ChromaStore:
    """Lazily connects to the Chroma server and reuses one async collection.

    Connecting lazily keeps the API usable (health, jobs) while Chroma boots.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._collection: AsyncCollection | None = None
        self._lock = asyncio.Lock()

    async def get_collection(self) -> AsyncCollection:
        if self._collection is None:
            async with self._lock:
                if self._collection is None:
                    async with chroma_errors():
                        client = await chromadb.AsyncHttpClient(
                            host=self._settings.chroma_host,
                            port=self._settings.chroma_port,
                        )
                        # Embeddings are computed by us (OpenAI), never by Chroma.
                        self._collection = await client.get_or_create_collection(
                            name=self._settings.chroma_collection,
                            metadata={"hnsw:space": "cosine"},
                            embedding_function=None,
                        )
        return self._collection
