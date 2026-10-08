"""Ingest documents (PDF/TXT/MD) into the knowledge base.

Usage (inside the backend container or a configured local env):
    python -m app.rag.ingest_cli                      # demo docs -> demo establishment
    python -m app.rag.ingest_cli --path docs/ --establishment-id finca-1
"""

import argparse
import asyncio
from pathlib import Path

from app.core.config import get_settings
from app.infrastructure.chroma import ChromaStore
from app.rag.ingestion import DEMO_DOCS_DIR, IngestionService
from app.services.llm import OpenAILLM

async def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEMO_DOCS_DIR)
    parser.add_argument("--establishment-id", default=settings.demo_establishment_id)
    args = parser.parse_args()

    llm = OpenAILLM(settings)
    try:
        service = IngestionService(ChromaStore(settings), llm, settings)
        if args.path.is_dir():
            results = await service.ingest_directory(args.path, args.establishment_id)
        else:
            results = [await service.ingest_file(args.path, args.establishment_id)]
    finally:
        await llm.aclose()
    for r in results:
        state = "skipped (unchanged)" if r.skipped else "ingested"
        print(f"{r.source}: {state}, {r.chunks} chunks")


if __name__ == "__main__":
    asyncio.run(main())
