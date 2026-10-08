from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import get_settings
from app.infrastructure.redis import create_redis_client, ping_redis

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
    try:
        yield
    finally:
        await redis.aclose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AgroOps production-grade AI intelligence system.",
    lifespan=lifespan,
)

app.include_router(health_router)


@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    return {
        "message": "AgroOps Intelligence System",
        "docs": "/docs",
    }
