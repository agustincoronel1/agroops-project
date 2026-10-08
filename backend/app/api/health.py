from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.config import get_settings
from app.infrastructure.redis import get_redis, ping_redis
from app.schemas.health import HealthResponse

router = APIRouter(tags=["Health"])

settings = get_settings()


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Redis unavailable"}},
)
async def health_check(
    redis: Annotated[Redis, Depends(get_redis)],
) -> HealthResponse | JSONResponse:
    redis_ok = await ping_redis(redis)
    response = HealthResponse(
        status="ok" if redis_ok else "degraded",
        service=settings.app_name,
        version=settings.app_version,
        redis="ok" if redis_ok else "unavailable",
    )
    if redis_ok:
        return response
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=response.model_dump(),
    )
