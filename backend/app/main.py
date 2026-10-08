import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.tasks import router as tasks_router
from app.core.config import get_settings
from app.infrastructure.redis import create_redis_client, ping_redis
from app.graph.builder import build_graph
from app.services.graph_processor import make_graph_processor
from app.services.job_service import JobService
from app.services.llm import OpenAILLM
from app.services.task_worker import TaskWorker

logger = logging.getLogger(__name__)
settings = get_settings()


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
    if settings.openai_api_key is None:
        logger.warning("OPENAI_API_KEY not set: jobs will fail until it is configured")
    llm = OpenAILLM(settings)
    graph = build_graph(llm, max_steps=settings.graph_max_steps)
    worker = TaskWorker(
        JobService(redis), make_graph_processor(graph, settings.graph_max_steps)
    )
    worker.start()
    try:
        yield
    finally:
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
