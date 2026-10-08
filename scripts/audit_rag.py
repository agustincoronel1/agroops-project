"""Real RAG integration audit (real Chroma at localhost:8001 + real OpenAI embeddings).

Run from the repo root so `.env` is picked up:
    PYTHONPATH=backend .venv/Scripts/python scripts/audit_rag.py
Uses isolated establishments (audit-a / audit-b) and removes ONLY their chunks at the end.
No secrets are printed.
"""

import asyncio
import json
import shutil
import textwrap
from pathlib import Path

from app.core.config import get_settings
from app.infrastructure.chroma import ChromaStore
from app.rag.ingestion import IngestionService
from app.rag.retriever import ChromaRetriever
from app.services.llm import OpenAILLM

DATA = Path("audit_out/rag_data")
EST_B = "audit-b"
SECRET = "ZETA-4471"


def make_pdf(path: Path, pages: list[list[str]]) -> None:
    """Minimal valid multi-page PDF with real extractable text."""
    objs: list[bytes] = []
    n = len(pages)
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, lines in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        text = "BT /F1 11 Tf 40 800 Td 14 TL\n" + "".join(
            f"({ln.replace('(', '[').replace(')', ']')}) Tj T*\n" for ln in lines
        ) + "ET"
        objs.append(f"<< /Length {len(text)} >>\nstream\n{text}\nendstream".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    path.write_bytes(out)


def wrap(s: str) -> list[str]:
    return textwrap.wrap(s, 85)


def build_data() -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    filler = " ".join(
        f"La finca B registra la rotacion del lote {i} con cebada y arveja forrajera cada temporada."
        for i in range(30)
    )
    (DATA / "finca_b_notas.txt").write_text(
        "NOTAS DE LA FINCA B (documento de prueba de auditoria, ficticio).\n" + filler, encoding="utf-8"
    )
    (DATA / "finca_b_procedimientos.md").write_text(
        "# Procedimientos Finca B (prueba, ficticio)\n\n"
        + "\n\n".join(
            f"## Paso {i}\nEl operario de la finca B verifica el comedero {i} y registra el nivel de alimento."
            for i in range(1, 15)
        ),
        encoding="utf-8",
    )
    page1 = wrap(
        "Manual de equipos de la Finca B (documento de auditoria ficticio). "
        "Pagina uno: la ordenadora automatica se limpia con agua caliente al terminar cada turno "
        "y el tanque de frio se controla a 4 grados centigrados."
    )
    page2 = wrap(
        f"Pagina dos: el codigo de acceso del silo de la Finca B es {SECRET}. "
        "La bomba hidraulica modelo QX-900 se revisa cada 77 horas de uso."
    )
    make_pdf(DATA / "finca_b_manual.pdf", [page1, page2])


async def chunk_count(col, est: str, doc_id: str | None = None) -> int:
    where = {"establishment_id": est} if doc_id is None else {
        "$and": [{"establishment_id": est}, {"document_id": doc_id}]
    }
    return len((await col.get(where=where, include=[]))["ids"])


async def main() -> None:
    s = get_settings()
    out: dict = {}
    llm = OpenAILLM(s)
    store = ChromaStore(s)
    ing = IngestionService(store, llm, s)
    ret = ChromaRetriever(store, llm, s)
    col = await store.get_collection()
    build_data()
    try:
        # 1. Ingest TXT / MD / PDF
        res = await ing.ingest_directory(DATA, EST_B)
        out["ingest_1"] = [r.model_dump() for r in res]
        # 2. Metadata, pages, embedding dimension
        got = await col.get(where={"establishment_id": EST_B}, include=["metadatas", "embeddings", "documents"])
        out["total_chunks"] = len(got["ids"])
        out["embedding_dim"] = len(got["embeddings"][0])
        keys = set()
        for m in got["metadatas"]:
            keys |= set(m)
        out["metadata_keys"] = sorted(keys)
        out["pdf_pages_in_metadata"] = sorted({m["page"] for m in got["metadatas"] if m["source"].endswith(".pdf")})
        out["chunk_len_max"] = max(len(d) for d in got["documents"])
        out["per_source_chunks"] = {
            src: sum(1 for m in got["metadatas"] if m["source"] == src)
            for src in sorted({m["source"] for m in got["metadatas"]})
        }
        # 3. Idempotency
        res2 = await ing.ingest_directory(DATA, EST_B)
        out["ingest_2_all_skipped"] = all(r.skipped for r in res2)
        out["total_chunks_after_reingest"] = await chunk_count(col, EST_B)
        # 4. Changed content replaces chunks
        f = DATA / "finca_b_notas.txt"
        f.write_text("Version corta de las notas de la finca B.", encoding="utf-8")
        r3 = await ing.ingest_file(f, EST_B)
        out["changed_file"] = {"skipped": r3.skipped, "chunks_now": r3.chunks,
                               "chunks_in_db": await chunk_count(col, EST_B, r3.document_id)}
        # 5. Retrieval with sources
        docs = await ret.retrieve("Cual es el codigo de acceso del silo?", EST_B)
        out["retrieve_secret_in_b"] = [
            {"source": d.source, "page": d.page, "chunk_id": d.chunk_id, "dist": round(d.distance, 3),
             "has_secret": SECRET in d.content} for d in docs
        ]
        # 6. Isolation: same query as another establishment, and demo-only query in B
        d_demo = await ret.retrieve("Cual es el codigo de acceso del silo?", "demo")
        out["isolation_demo_sees_secret"] = any(SECRET in d.content or d.establishment_id != "demo" for d in d_demo)
        out["isolation_demo_results"] = [(d.source, round(d.distance, 3)) for d in d_demo]
        d_b = await ret.retrieve("triple lavado del tanque de la pulverizadora", EST_B)
        out["isolation_b_sees_demo_docs"] = [(d.source, round(d.distance, 3)) for d in d_b]
        out["unknown_establishment"] = [d.source for d in await ret.retrieve("tractor", "audit-zzz")]
        # 7. Unrelated query -> nothing relevant
        for q in ["Cual es la capital de Francia?", "Receta de milanesas con papas fritas",
                  "dosis exacta de glifosato por hectarea en soja"]:
            r = await ret.retrieve(q, "demo")
            out.setdefault("unrelated_queries_demo", {})[q] = [(d.source, round(d.distance, 3)) for d in r]
        # 8. demo docs intact
        out["demo_chunks"] = await chunk_count(col, "demo")
    finally:
        await col.delete(where={"establishment_id": {"$in": [EST_B, "audit-zzz"]}}) if "--keep" not in __import__("sys").argv else None
        await llm.aclose()
    print(json.dumps(out, indent=2, ensure_ascii=False))


asyncio.run(main())
