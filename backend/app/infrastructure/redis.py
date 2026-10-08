import logging

from fastapi import Request
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


def create_redis_client(url: str) -> Redis:
    """Create an async Redis client. The connection pool is created lazily."""
    return Redis.from_url(url, decode_responses=True)


async def ping_redis(client: Redis) -> bool:
    """Return True if Redis answers PING, False on any error."""
    try:
        return bool(await client.ping())
    except Exception as exc:  # noqa: BLE001 - health probes must never raise
        logger.warning("Redis ping failed: %s", exc)
        return False


def get_redis(request: Request) -> Redis:
    """FastAPI dependency returning the shared client created in lifespan."""
    return request.app.state.redis
