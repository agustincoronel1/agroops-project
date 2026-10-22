from pathlib import Path

import pytest
from app.core.config import Settings
from app.rag.hybrid import bm25_scores, reciprocal_rank_fusion, tokenize
from app.rag.ingestion import IngestionService
from app.rag.retriever import ChromaRetriever
from tests.test_rag import FakeEmbedder, FakeStore


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_tokenize_folds_accents_and_drops_stopwords() -> None:
    assert tokenize("¿Qué maquinaria está fuera de servicio?") == ["maquinaria", "fuera", "servicio"]
    assert tokenize("Código del LOTE L7731") == ["codigo", "lote", "l7731"]


def test_bm25_ranks_documents_with_the_rare_term_first() -> None:
    docs = [
        tokenize("riego por goteo del lote norte"),
        tokenize("lote L7731 cosechadora"),
        tokenize("tareas de fertilizacion"),
    ]
    scores = bm25_scores(tokenize("L7731"), docs)
    assert scores[1] > 0 and scores[0] == 0 and scores[2] == 0


def test_bm25_is_zero_without_query_terms() -> None:
    assert bm25_scores([], [tokenize("algo")]) == [0.0]
    assert bm25_scores(tokenize("zzz"), []) == []


def test_rrf_prefers_items_found_by_both_rankings_and_is_deterministic() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "a", "d"]])
    assert fused[:2] == ["a", "c"]  # present in both lists
    assert fused == reciprocal_rank_fusion([["a", "b", "c"], ["c", "a", "d"]])
    assert reciprocal_rank_fusion([["x"], ["y"]]) == ["x", "y"]  # tie broken by id


@pytest.mark.anyio
async def test_keyword_recall_beyond_vector_threshold_and_mode_labels(tmp_path: Path) -> None:
    store, embedder = FakeStore(), FakeEmbedder()
    ingestion = IngestionService(store, embedder, _settings(rag_chunk_size=300))  # type: ignore[arg-type]
    mine = tmp_path / "lote.md"
    mine.write_text("Lote L7731 riego por goteo los martes por la mañana.", encoding="utf-8")
    neighbour = tmp_path / "vecino.md"
    neighbour.write_text("Lote L7731 registrado en la finca vecina.", encoding="utf-8")
    await ingestion.ingest_file(mine, "finca-h")
    await ingestion.ingest_file(neighbour, "finca-vecina")

    # Threshold 0.0: no vector hit can pass, so only the keyword ranking can answer.
    strict = ChromaRetriever(store, embedder, _settings(rag_max_distance=0.0))  # type: ignore[arg-type]
    docs = await strict.retrieve("L7731", "finca-h")
    assert [d.source for d in docs] == ["lote.md"]
    assert docs[0].retrieval == "keyword" and docs[0].distance is None
    assert all(d.establishment_id == "finca-h" for d in docs)  # isolation holds

    permissive = ChromaRetriever(store, embedder, _settings(rag_max_distance=2.0))  # type: ignore[arg-type]
    docs = await permissive.retrieve("riego por goteo lote L7731", "finca-h")
    assert docs[0].retrieval == "hybrid" and docs[0].distance is not None
    assert all(d.establishment_id == "finca-h" for d in docs)
