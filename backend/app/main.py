import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.tasks import router as tasks_router
from app.core.config import get_settings
from app.infrastructure.redis import create_redis_client, ping_redis
from app.graph.builder import build_graph
from app.infrastructure.chroma import ChromaStore
from app.rag.ingestion import DEMO_DOCS_DIR, IngestionService
from app.rag.retriever import ChromaRetriever
from app.services.graph_processor import make_graph_processor
from app.services.job_service import JobService
from app.services.llm import OpenAILLM
from app.services.task_worker import TaskWorker

logger = logging.getLogger(__name__)
settings = get_settings()


async def _ingest_demo_docs(ingestion: IngestionService) -> None:
    """Idempotent first-run ingestion of the DEMO documents; never blocks startup."""
    delay = 2.0
    for _ in range(5):
        try:
            await ingestion.ingest_directory(DEMO_DOCS_DIR, settings.demo_establishment_id)
            return
        except Exception as exc:  # noqa: BLE001 - Chroma may still be booting
            logger.warning("Demo ingestion failed (%s); retrying in %.0fs", exc, delay)
            await asyncio.sleep(delay)
            delay *= 2
    logger.error("Demo ingestion gave up; run `python -m app.rag.ingest_cli` manually")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    redis = create_redis_client(settings.redis_url)
    if not await ping_redis(redis):
        await redis.aclose()
        raise RuntimeError(
            f"Cannot connect to Redis at {settings.redis_url}. "
            "Check that Redis is running and REDIS_URL is correct."
        )
    app.state.redis = redis
    demo_task: asyncio.Task[None] | None = None
    if not settings.has_openai_key:
        logger.warning("OPENAI_API_KEY not set: jobs will fail until it is configured")
    llm = OpenAILLM(settings)
    store = ChromaStore(settings)
    retriever = ChromaRetriever(store, llm, settings)
    graph = build_graph(llm, retriever, max_steps=settings.graph_max_steps)
    worker = TaskWorker(
        JobService(redis), make_graph_processor(graph, settings.graph_max_steps)
    )
    worker.start()
    if settings.ingest_demo_on_startup and settings.has_openai_key:
        ingestion = IngestionService(store, llm, settings)
        demo_task = asyncio.create_task(_ingest_demo_docs(ingestion))
    try:
        yield
    finally:
        if demo_task:
            demo_task.cancel()
        await worker.stop()
        await llm.aclose()
        await redis.aclose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AgroOps production-grade AI intelligence system.",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(tasks_router)


@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    return {
        "message": "AgroOps Intelligence System",
        "docs": "/docs",
    }
