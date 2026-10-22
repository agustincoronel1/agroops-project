import os

# Tests must never send traces to LangSmith (set before any app import).
os.environ["LANGSMITH_TRACING"] = "false"
os.environ.pop("LANGSMITH_API_KEY", None)

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
