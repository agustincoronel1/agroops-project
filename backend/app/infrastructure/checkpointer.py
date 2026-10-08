from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.redis.aio import AsyncRedisSaver


@asynccontextmanager
async def open_checkpointer(redis_url: str) -> AsyncIterator[AsyncRedisSaver]:
    """Persistent LangGraph checkpointer on Redis (needs Redis Stack: JSON + Search)."""
    async with AsyncRedisSaver.from_conn_string(redis_url) as saver:
        await saver.asetup()
        yield saver
