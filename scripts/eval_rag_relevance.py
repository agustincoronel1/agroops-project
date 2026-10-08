"""Retrieval relevance evaluation on the DEMO corpus (embeddings only, no chat LLM).

    PYTHONPATH=backend .venv/Scripts/python scripts/eval_rag_relevance.py
Reports, per query, the best distance and whether the expected source is retrieved
with the CURRENT threshold (recall) — the threshold itself is never changed here.
"""

import asyncio

from app.core.config import get_settings
from app.infrastructure.chroma import ChromaStore
from app.rag.retriever import ChromaRetriever
from app.services.llm import OpenAILLM

POSITIVE = [
    ("Cada cuantas horas se hace el servicio del tractor?", "maquinaria_demo.md"),
    ("Como se limpia la pulverizadora despues de cada jornada?", "maquinaria_demo.md"),
    ("Que se revisa en la cosechadora antes de la campania?", "maquinaria_demo.md"),
    ("Que hacer cuando una maquina esta fuera de servicio?", "procedimientos_demo.md"),
    ("Que condiciones de viento se requieren para aplicar fitosanitarios?", "procedimientos_demo.md"),
    ("Como se registran las incidencias de severidad alta?", "procedimientos_demo.md"),
    ("Que equipo de proteccion personal usa el operario?", "procedimientos_demo.md"),
    ("Cuando se fertiliza el maiz con nitrogeno?", "tareas_agropecuarias_demo.md"),
    ("Plazo para aplicar fungicida ante roya en soja", "tareas_agropecuarias_demo.md"),
    ("Como se priorizan las tareas?", "tareas_agropecuarias_demo.md"),
    ("control de malezas en trigo macollaje", "tareas_agropecuarias_demo.md"),
]
NEGATIVE = [
    "Cual es la capital de Francia?",
    "Receta de milanesas con papas fritas",
    "Dosis exacta de glifosato por hectarea en soja",
    "Quien gano el mundial de futbol 2022?",
    "Como configurar un router wifi?",
    "Precio de la soja en la bolsa de Chicago hoy",
    "Cual es el codigo de acceso del silo de la Finca B?",
    "Normativa legal de exportacion de granos",
]


async def main() -> None:
    s = get_settings()
    llm = OpenAILLM(s)
    # threshold wide open to observe raw distances; recall uses the real threshold
    wide = ChromaRetriever(ChromaStore(s), llm, s.model_copy(update={"rag_max_distance": 2.0}))
    try:
        print(f"current RAG_MAX_DISTANCE={s.rag_max_distance} top_k={s.rag_top_k}\n")
        hits, pos_d = 0, []
        print("POSITIVE (expected source / best dist / best source / recall@k with threshold)")
        for q, expected in POSITIVE:
            docs = await wide.retrieve(q, s.demo_establishment_id)
            kept = [d for d in docs if d.distance <= s.rag_max_distance]
            ok = any(d.source == expected for d in kept)
            hits += ok
            pos_d.append(docs[0].distance)
            print(f"  {'OK ' if ok else 'MISS'} {docs[0].distance:.3f} {docs[0].source:30s} <- {q}")
        neg_d = []
        print("\nNEGATIVE (best dist / kept by threshold)")
        for q in NEGATIVE:
            docs = await wide.retrieve(q, s.demo_establishment_id)
            kept = [d for d in docs if d.distance <= s.rag_max_distance]
            neg_d.append(docs[0].distance)
            print(f"  {docs[0].distance:.3f} kept={len(kept)} <- {q}")
        print(f"\nrecall (expected source retrieved): {hits}/{len(POSITIVE)}")
        print(f"positive best-dist  min={min(pos_d):.3f} max={max(pos_d):.3f}")
        print(f"negative best-dist  min={min(neg_d):.3f} max={max(neg_d):.3f}")
        print("distance ranges overlap" if min(neg_d) <= max(pos_d) else "distance ranges are separable")
    finally:
        await llm.aclose()


asyncio.run(main())
