import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


async def redis_unavailable_handler(request: Request, exc: RedisError) -> JSONResponse:
    """Turn Redis failures into a controlled 503 (no traceback, no internals)."""
    logger.warning("Redis error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Job store (Redis) is temporarily unavailable. Retry later."},
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RedisError, redis_unavailable_handler)  # type: ignore[arg-type]
