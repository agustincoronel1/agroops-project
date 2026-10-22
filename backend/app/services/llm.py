from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import AgentOutputInvalid, LLMNotConfigured, LLMUnavailable
from app.observability.tracing import get_current_run_tree, traceable, wrap_openai_client

T = TypeVar("T", bound=BaseModel)

ChatMessages = list[dict[str, str]]
EMBED_BATCH_SIZE = 100


class LLMConfigurationError(LLMNotConfigured):
    """Raised when the LLM cannot be used because of missing configuration."""


class LLM(Protocol):
    async def complete(self, messages: ChatMessages) -> str: ...

    async def parse(self, messages: ChatMessages, response_model: type[T]) -> T: ...


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@asynccontextmanager
async def _provider_errors() -> AsyncIterator[None]:
    """Provider failures become controlled errors; SDK messages stay in the log."""
    try:
        yield
    except openai.APIError as exc:
        raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc


class OpenAILLM:
    """Thin async wrapper over the OpenAI SDK. The client is created lazily."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self._settings.has_openai_key:
                raise LLMConfigurationError(
                    "OPENAI_API_KEY is not configured. Set it in .env to run the graph."
                )
            key = self._settings.openai_api_key
            assert key is not None  # guaranteed by has_openai_key
            self._client = wrap_openai_client(AsyncOpenAI(api_key=key.get_secret_value()))
        return self._client

    async def complete(self, messages: ChatMessages) -> str:
        async with _provider_errors():
            response = await self._get_client().chat.completions.create(
                model=self._settings.openai_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=0,  # routing and answers must be reproducible
            )
        return response.choices[0].message.content or ""

    async def parse(self, messages: ChatMessages, response_model: type[T]) -> T:
        async with _provider_errors():
            response = await self._get_client().chat.completions.parse(
                model=self._settings.openai_model,
                messages=messages,  # type: ignore[arg-type]
                response_format=response_model,
                temperature=0,  # same decision for the same input (e.g. answered=true/false)
            )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise AgentOutputInvalid("LLM returned no parsable structured output")
        return parsed

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            vectors.extend(await self._embed_batch(texts[i : i + EMBED_BATCH_SIZE]))
        return vectors

    # wrap_openai does not cover embeddings. LangSmith only aggregates tokens/cost for
    # "llm" runs, so trace them as such, reporting the real usage returned by OpenAI so
    # LangSmith can compute cost from its own price map.
    @traceable(run_type="llm", name="openai.embeddings", tags=["embedding"])
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        async with _provider_errors():
            response = await self._get_client().embeddings.create(
                model=self._settings.embedding_model, input=batch
            )
        _report_embedding_usage(response, self._settings.embedding_model)
        return [item.embedding for item in response.data]

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()


def _report_embedding_usage(response: object, model: str) -> None:
    run = get_current_run_tree()
    usage = getattr(response, "usage", None)
    if run is None or usage is None:
        return
    run.add_metadata({"ls_provider": "openai", "ls_model_name": model})
    tokens = int(usage.prompt_tokens)
    run.set(usage_metadata={"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens})
