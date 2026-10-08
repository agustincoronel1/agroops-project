from typing import Protocol, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.config import Settings

T = TypeVar("T", bound=BaseModel)

ChatMessages = list[dict[str, str]]


class LLMConfigurationError(RuntimeError):
    """Raised when the LLM cannot be used because of missing configuration."""


class LLM(Protocol):
    async def complete(self, messages: ChatMessages) -> str: ...

    async def parse(self, messages: ChatMessages, response_model: type[T]) -> T: ...


class OpenAILLM:
    """Thin async wrapper over the OpenAI SDK. The client is created lazily."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            key = self._settings.openai_api_key
            if key is None or not key.get_secret_value():
                raise LLMConfigurationError(
                    "OPENAI_API_KEY is not configured. Set it in .env to run the graph."
                )
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

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
