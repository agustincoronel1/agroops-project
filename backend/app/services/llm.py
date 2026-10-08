from typing import Protocol, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.config import Settings

T = TypeVar("T", bound=BaseModel)

ChatMessages = list[dict[str, str]]
EMBED_BATCH_SIZE = 100


class LLMConfigurationError(RuntimeError):
    """Raised when the LLM cannot be used because of missing configuration."""


class LLM(Protocol):
    async def complete(self, messages: ChatMessages) -> str: ...

    async def parse(self, messages: ChatMessages, response_model: type[T]) -> T: ...


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


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
            self._client = AsyncOpenAI(api_key=key.get_secret_value())
        return self._client

    async def complete(self, messages: ChatMessages) -> str:
        response = await self._get_client().chat.completions.create(
            model=self._settings.openai_model,
            messages=messages,  # type: ignore[arg-type]
        )
        return response.choices[0].message.content or ""

    async def parse(self, messages: ChatMessages, response_model: type[T]) -> T:
        response = await self._get_client().chat.completions.parse(
            model=self._settings.openai_model,
            messages=messages,  # type: ignore[arg-type]
            response_format=response_model,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError("LLM returned no parsable structured output")
        return parsed

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            response = await self._get_client().embeddings.create(
                model=self._settings.embedding_model,
                input=texts[i : i + EMBED_BATCH_SIZE],
            )
            vectors.extend(item.embedding for item in response.data)
        return vectors

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
