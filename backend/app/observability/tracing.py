"""LangSmith tracing helpers.

Tracing is strictly optional: when disabled (default) every helper degrades to a
no-op, and LangSmith failures are swallowed by the SDK (they never raise into the
request/worker path). The API key lives only in ``Settings`` (SecretStr) and in the
process environment; it is never put in trace inputs, metadata or logs.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, TypeVar

from langsmith import trace, traceable
from langsmith import utils as ls_utils
from langsmith.run_helpers import get_current_run_tree
from langsmith.wrappers import wrap_openai

from app.core.config import Settings

logger = logging.getLogger(__name__)

C = TypeVar("C")

__all__ = [
    "get_current_run_tree",
    "configure_tracing",
    "ingest_trace",
    "job_metadata",
    "job_output",
    "job_trace",
    "traceable",
    "tracing_enabled",
    "wrap_openai_client",
]


def configure_tracing(settings: Settings) -> bool:
    """Export LangSmith settings to the environment the SDK reads. Returns enabled state.

    Tracing is enabled only if requested AND an API key is present.
    """
    wanted = settings.langsmith_tracing
    has_key = settings.has_langsmith_key
    if wanted and not has_key:
        logger.warning("LANGSMITH_TRACING=true but LANGSMITH_API_KEY is empty: tracing disabled")
    enabled = wanted and has_key
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    key = settings.langsmith_api_key
    if enabled and key is not None:
        os.environ["LANGSMITH_API_KEY"] = key.get_secret_value()
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        if settings.langsmith_endpoint:
            os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    _reset_sdk_caches()
    logger.info(
        "LangSmith tracing %s%s",
        "enabled" if enabled else "disabled",
        f" (project={settings.langsmith_project})" if enabled else "",
    )
    return enabled


def _reset_sdk_caches() -> None:
    # The SDK caches env lookups; clear them so changed settings take effect.
    for name in ("get_env_var", "get_tracer_project"):
        fn = getattr(ls_utils, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()


def tracing_enabled() -> bool:
    return bool(ls_utils.tracing_is_enabled())


def wrap_openai_client(client: C) -> C:
    """Trace chat/parse calls (with real usage + model for LangSmith cost) when enabled."""
    if not tracing_enabled():
        return client
    return wrap_openai(client, chat_name="openai.chat")  # type: ignore[type-var]


def job_output(
    job_id: str,
    status: str,
    *,
    agents_consulted: Iterable[str] = (),
    sources_count: int = 0,
    action_status: str | None = None,
    has_answer: bool = False,
    error_type: str | None = None,
) -> dict[str, Any]:
    """Structured output of the ``agroops.job`` root run. Same keys for every status.

    Only identifiers, names, counts and flags: never the answer text, prompts, the user
    question, source content or secrets. ``action_status`` mirrors the API value
    (APPLIED/REJECTED/None) and is ``PENDING_APPROVAL`` while a human decision is open.
    """
    return {
        "job_id": job_id,
        "status": status,
        "agents_consulted": sorted(str(a) for a in agents_consulted),
        "sources_count": int(sources_count),
        "action_status": str(action_status) if action_status is not None else None,
        "has_answer": bool(has_answer),
        "error_type": error_type,
    }


def job_metadata(job: Any) -> dict[str, Any]:
    """Searchable, non-sensitive identifiers for a job. ``thread_id`` groups the
    original run and any HITL resume of the same job in LangSmith Threads."""
    return {
        "job_id": job.job_id,
        "thread_id": job.job_id,
        "establishment_id": job.establishment_id,
        "resumed": job.approval is not None,
    }


@asynccontextmanager
async def job_trace(job: Any) -> AsyncIterator[Any]:
    """Root run for one worker execution of a job; yields a handle with ``set_status``.

    Nested graph/LLM/embedding runs attach to it through the active trace context.
    """
    if not tracing_enabled():
        yield _NoopHandle()
        return
    meta = job_metadata(job)
    async with _guarded_trace(
        name="agroops.job",
        run_type="chain",
        inputs={"job_id": job.job_id, "establishment_id": job.establishment_id},
        metadata=meta,
        tags=["agroops", "worker"],
    ) as handle:
        yield handle


@asynccontextmanager
async def ingest_trace(establishment_id: str, source: str) -> AsyncIterator[Any]:
    """Root run for ingesting one document (outside any job), tagged with its establishment.

    Embedding calls made while indexing nest under it, so they are never orphan traces.
    """
    if not tracing_enabled():
        yield _NoopHandle()
        return
    async with _guarded_trace(
        name="agroops.ingest",
        run_type="chain",
        inputs={"source": source, "establishment_id": establishment_id},
        metadata={"establishment_id": establishment_id, "source": source},
        tags=["agroops", "ingest"],
    ) as handle:
        yield handle


@asynccontextmanager
async def _guarded_trace(**kwargs: Any) -> AsyncIterator[Any]:
    """``trace()`` that degrades to a no-op if LangSmith fails to open or close the run.

    Observability must never fail the job it observes, so a start/exit error is logged
    (exception type only, never its message) and the block runs untraced.
    """
    cm = trace(**kwargs)
    try:
        run = await cm.__aenter__()
    except Exception as exc:  # noqa: BLE001
        logger.warning("LangSmith trace not started (%s); continuing untraced", type(exc).__name__)
        yield _NoopHandle()
        return
    try:
        yield _RunHandle(run)
    except BaseException as exc:
        await _safe_exit(cm, type(exc), exc, exc.__traceback__)
        raise
    else:
        await _safe_exit(cm, None, None, None)


async def _safe_exit(cm: Any, exc_type: Any, exc: Any, tb: Any) -> None:
    try:
        await cm.__aexit__(exc_type, exc, tb)
    except Exception as err:  # noqa: BLE001
        logger.warning("LangSmith trace not finished (%s)", type(err).__name__)


class _NoopHandle:
    def set_status(self, status: str, **extra: Any) -> None:
        return None

    def annotate(self, **metadata: Any) -> None:
        return None

    def set_outputs(self, outputs: dict[str, Any]) -> None:
        return None


class _RunHandle:
    """Annotates the open run. It never ends the run: the ``trace`` context manager does
    that on exit, so the latency covers the whole block."""

    def __init__(self, run: Any) -> None:
        self._run = run

    def annotate(self, **metadata: Any) -> None:
        try:
            self._run.add_metadata(metadata)
        except Exception:  # noqa: BLE001 - observability must never break the worker
            logger.debug("Could not annotate trace", exc_info=True)

    def set_outputs(self, outputs: dict[str, Any]) -> None:
        # Without this the root run is saved with outputs=None ("No outputs" in LangSmith).
        try:
            self._run.set(outputs=outputs)
        except Exception:  # noqa: BLE001 - observability must never break the worker
            logger.debug("Could not set trace outputs", exc_info=True)

    def set_status(self, status: str, **extra: Any) -> None:
        self.annotate(job_status=status, **extra)
        if status == "failed":
            # Recorded as an error on the run; the exception text is not stored.
            self._run.error = str(extra.get("error_type", "failed"))
