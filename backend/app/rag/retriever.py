from app.agents.knowledge import RetrievedDocument
from app.core.config import Settings
from app.infrastructure.chroma import ChromaStore
from app.services.llm import Embedder


class ChromaRetriever:
    """Semantic search over Chroma, always scoped to one establishment."""

    def __init__(self, store: ChromaStore, embedder: Embedder, settings: Settings) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = settings.rag_top_k
        self._max_distance = settings.rag_max_distance

    async def retrieve(
        self, query: str, establishment_id: str
    ) -> list[RetrievedDocument]:
        if not establishment_id:
            raise ValueError("establishment_id is required for retrieval")
        [vector] = await self._embedder.embed([query])
        collection = await self._store.get_collection()
        raw = await collection.query(
            query_embeddings=[vector],
            n_results=self._top_k,
            where={"establishment_id": establishment_id},
            include=["documents", "metadatas", "distances"],
        )
        docs: list[RetrievedDocument] = []
        for text, meta, distance in zip(
            (raw["documents"] or [[]])[0],
            (raw["metadatas"] or [[]])[0],
            (raw["distances"] or [[]])[0],
            strict=True,
        ):
            if distance > self._max_distance:
                continue
            docs.append(
                RetrievedDocument(
                    source=meta["source"],
                    content=text,
                    establishment_id=meta["establishment_id"],
                    document_id=meta["document_id"],
                    chunk_id=meta["chunk_id"],
                    page=meta.get("page"),
                    distance=distance,
                )
            )
        return docs
