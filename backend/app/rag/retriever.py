import asyncio
from typing import Any

from app.agents.knowledge import RetrievedDocument
from app.core.config import Settings
from app.infrastructure.chroma import (
    ChromaStore,
    chroma_errors,
    ensure_embedding_dimension,
)
from app.observability.tracing import traceable
from app.rag.hybrid import keyword_order, reciprocal_rank_fusion
from app.services.llm import Embedder


def _document(text: str, meta: dict[str, Any], distance: float | None, mode: str) -> RetrievedDocument:
    return RetrievedDocument(
        source=meta["source"],
        content=text,
        establishment_id=meta["establishment_id"],
        document_id=meta["document_id"],
        chunk_id=meta["chunk_id"],
        page=meta.get("page"),
        distance=distance,
        retrieval=mode,  # type: ignore[arg-type]
    )


class ChromaRetriever:
    """Hybrid search over Chroma, always scoped to one establishment.

    Two rankings are fused with reciprocal rank fusion:
    - vector: nearest chunks, kept only if within RAG_MAX_DISTANCE;
    - keyword: BM25 over the establishment's chunks (catches exact words such as codes
      or machine names that embeddings can miss).
    """

    def __init__(self, store: ChromaStore, embedder: Embedder, settings: Settings) -> None:
        self._store = store
        self._embedder = embedder
        self._model = settings.embedding_model
        self._top_k = settings.rag_top_k
        self._max_distance = settings.rag_max_distance

    @traceable(run_type="retriever", name="knowledge.retrieve")
    async def retrieve(
        self, query: str, establishment_id: str
    ) -> list[RetrievedDocument]:
        if not establishment_id:
            raise ValueError("establishment_id is required for retrieval")
        [vector] = await self._embedder.embed([query])
        collection = await self._store.get_collection()
        await ensure_embedding_dimension(collection, len(vector), self._model)
        scope = {"establishment_id": establishment_id}
        async with chroma_errors():
            raw = await collection.query(
                query_embeddings=[vector],
                n_results=self._top_k,
                where=scope,
                include=["documents", "metadatas", "distances"],
            )
            corpus = await collection.get(where=scope, include=["documents", "metadatas"])

        found: dict[str, RetrievedDocument] = {}
        vector_ranking: list[str] = []
        for text, meta, distance in zip(
            (raw["documents"] or [[]])[0],
            (raw["metadatas"] or [[]])[0],
            (raw["distances"] or [[]])[0],
            strict=True,
        ):
            if distance > self._max_distance:
                continue
            found[meta["chunk_id"]] = _document(text, meta, distance, "vector")
            vector_ranking.append(meta["chunk_id"])

        corpus_docs: list[str] = corpus["documents"] or []
        corpus_metas: list[dict[str, Any]] = corpus["metadatas"] or []
        # BM25 scans the whole establishment corpus: CPU-bound, so it runs off the event loop.
        best = await asyncio.to_thread(keyword_order, query, corpus_docs, self._top_k)
        keyword_ranking: list[str] = []
        for i in best:
            chunk_id = corpus_metas[i]["chunk_id"]
            keyword_ranking.append(chunk_id)
            found.setdefault(chunk_id, _document(corpus_docs[i], corpus_metas[i], None, "keyword"))

        both = set(vector_ranking) & set(keyword_ranking)
        docs: list[RetrievedDocument] = []
        for chunk_id in reciprocal_rank_fusion([vector_ranking, keyword_ranking])[: self._top_k]:
            doc = found[chunk_id]
            docs.append(doc.model_copy(update={"retrieval": "hybrid"}) if chunk_id in both else doc)
        return docs
