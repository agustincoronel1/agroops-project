import asyncio

import chromadb
from chromadb.api.models.AsyncCollection import AsyncCollection

from app.core.config import Settings


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
