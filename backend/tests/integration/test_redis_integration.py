"""Integration tests against a REAL Redis Stack (JobService transactions and LangGraph checkpoints).

Opt-in: set AGROOPS_INTEGRATION=1 and start Redis with `docker compose up -d redis`.

Two databases are involved, for two different reasons:
- JobService tests use database 15 (AGROOPS_TEST_REDIS_URL). Its queue is flushed before and
  after the test, and database 0 is refused outright: the app's worker there would pick up
  test jobs and call OpenAI.
- The checkpoint test uses database 0 (AGROOPS_TEST_CHECKPOINT_URL). RediSearch, which the
  LangGraph checkpointer needs, only creates indexes in database 0. The test writes no job
  and touches no queue, so the worker cannot see it; it deletes only its own thread's keys.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypedDict

import pytest
from app.infrastructure.checkpointer import open_checkpointer
from app.schemas.tasks import ApprovalRequest, JobStatus
from app.services.job_service import QUEUE_KEY, ApprovalConflictError, JobService
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from redis.asyncio import Redis

REDIS_URL = os.getenv("AGROOPS_TEST_REDIS_URL", "redis://localhost:6379/15")
CHECKPOINT_URL = os.getenv("AGROOPS_TEST_CHECKPOINT_URL", "redis://localhost:6379/0")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("AGROOPS_INTEGRATION") != "1", reason="set AGROOPS_INTEGRATION=1"),
]


@pytest.fixture(autouse=True)
def no_langsmith_traces() -> None:
    """QA guard: integration runs must never send traces to LangSmith."""
    from app.observability.tracing import tracing_enabled

    if tracing_enabled() or os.getenv("LANGSMITH_TRACING", "false").lower() == "true":
        pytest.fail("refusing to run integration tests with LangSmith tracing enabled")
    if os.getenv("LANGSMITH_API_KEY"):
        pytest.fail("refusing to run integration tests while LANGSMITH_API_KEY is set")


@asynccontextmanager
async def _test_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    if client.connection_pool.connection_kwargs.get("db", 0) == 0:
        await client.aclose()
        pytest.fail("refusing to use Redis database 0 (the app's); set AGROOPS_TEST_REDIS_URL")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def _delete_thread_keys(thread_id: str) -> None:
    """Remove only the checkpoint keys of this test's thread (the index is shared, keep it)."""
    client = Redis.from_url(CHECKPOINT_URL, decode_responses=True)
    try:
        keys = [key async for key in client.scan_iter(match=f"*{thread_id}*")]
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_concurrent_approvals_on_real_redis_record_one_decision_and_enqueue_once() -> None:
    async with _test_redis() as redis:
        jobs = JobService(redis)
        job = await jobs.create_job("registrá una incidencia")
        await jobs.mark_waiting_approval(job.job_id, {"action_id": f"{job.job_id}-incident"})

        results = await asyncio.gather(
            jobs.submit_approval(job.job_id, ApprovalRequest(approve=True)),
            jobs.submit_approval(job.job_id, ApprovalRequest(approve=False)),
            return_exceptions=True,
        )

        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, ApprovalConflictError)]
        assert len(winners) == 1 and len(losers) == 1
        stored = await jobs.get_job(job.job_id)
        assert stored is not None and stored.status is JobStatus.PENDING
        # One enqueue from create_job plus exactly one from the winning approval.
        assert await redis.llen(QUEUE_KEY) == 2


GraphState = TypedDict("GraphState", {"value": str})


def _interrupting_graph(saver: AsyncRedisSaver) -> CompiledStateGraph:
    def ask(state: GraphState) -> dict[str, str]:
        decision = interrupt({"question": "approve?"})
        return {"value": f"{state['value']}:{decision}"}

    builder = StateGraph(GraphState)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=saver)


@pytest.mark.anyio
async def test_graph_paused_on_interrupt_resumes_after_a_restart_on_real_redis() -> None:
    thread_id = f"itest-{uuid.uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}
    try:
        # First process: the graph runs until the interrupt and its checkpoint is saved.
        async with open_checkpointer(CHECKPOINT_URL) as saver:
            paused = await _interrupting_graph(saver).ainvoke({"value": "x"}, config=config)
        assert "__interrupt__" in paused

        # "Restart": a new connection and a new compiled graph resume from Redis alone.
        async with open_checkpointer(CHECKPOINT_URL) as saver:
            final = await _interrupting_graph(saver).ainvoke(Command(resume="yes"), config=config)
        assert final["value"] == "x:yes"
    finally:
        await _delete_thread_keys(thread_id)
