from typing import Any

import pytest
from pydantic import BaseModel

from app.agents.knowledge import NO_KNOWLEDGE_MESSAGE, RetrievedDocument
from app.agents.supervisor import SupervisorDecision
from app.graph.builder import build_graph, graph_config
from app.schemas.tasks import Job, JobStatus
from app.services.graph_processor import make_graph_processor
from app.services.job_service import JobService
from app.services.llm import LLMConfigurationError, OpenAILLM
from app.services.task_worker import TaskWorker
from app.core.config import Settings
from tests.test_tasks import FakeRedis


class FakeLLM:
    """Scripted LLM: `decisions` are returned in order by parse()."""

    def __init__(self, decisions: list[str]) -> None:
        self.decisions = list(decisions)
        self.complete_calls: list[list[dict[str, str]]] = []

    async def parse(self, messages: Any, response_model: type[BaseModel]) -> Any:
        return response_model(next=self.decisions.pop(0), reasoning="test")

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.complete_calls.append(messages)
        return f"completion-{len(self.complete_calls)}"


async def _run(llm: FakeLLM, retriever: Any = None, max_steps: int = 4) -> dict[str, Any]:
    from app.graph.state import new_state

    graph = build_graph(llm, retriever, max_steps)
    return await graph.ainvoke(new_state("t1", "consulta"), config=graph_config(max_steps))


@pytest.mark.anyio
async def test_routes_to_operations_only() -> None:
    llm = FakeLLM(["operations", "FINISH"])
    final = await _run(llm)
    assert list(final["agent_outputs"]) == ["operations"]
    assert final["result"]["agents_consulted"] == ["operations"]
    # operations analysis + final synthesis; data is labelled DEMO
    assert len(llm.complete_calls) == 2
    assert "DEMO" in llm.complete_calls[0][1]["content"]


@pytest.mark.anyio
async def test_routes_to_both_agents_without_real_documents() -> None:
    llm = FakeLLM(["operations", "knowledge", "FINISH"])
    final = await _run(llm)
    assert final["agent_outputs"]["knowledge"] == NO_KNOWLEDGE_MESSAGE
    assert final["result"]["agents_consulted"] == ["operations", "knowledge"]


@pytest.mark.anyio
async def test_knowledge_uses_retriever_documents() -> None:
    class Retriever:
        async def retrieve(
            self, query: str, establishment_id: str
        ) -> list[RetrievedDocument]:
            assert establishment_id == "demo"
            return [
                RetrievedDocument(
                    source="doc-x",
                    content="texto",
                    establishment_id="demo",
                    document_id="d1",
                    chunk_id="d1-0000",
                    page=2,
                )
            ]

    llm = FakeLLM(["knowledge", "FINISH"])
    final = await _run(llm, Retriever())
    assert "doc-x" in llm.complete_calls[0][1]["content"]
    assert final["result"]["agents_consulted"] == ["knowledge"]
    assert final["result"]["sources"] == [
        {
            "source": "doc-x",
            "document_id": "d1",
            "chunk_id": "d1-0000",
            "page": 2,
            "distance": None,
        }
    ]


@pytest.mark.anyio
async def test_no_infinite_loop_when_llm_repeats_agent() -> None:
    llm = FakeLLM(["operations", "operations", "operations"])
    final = await _run(llm)
    assert final["result"]["agents_consulted"] == ["operations"]
    assert final["steps"] == 1


@pytest.mark.anyio
async def test_max_steps_forces_finish() -> None:
    llm = FakeLLM(["operations", "knowledge"])
    final = await _run(llm, max_steps=1)
    assert final["result"]["agents_consulted"] == ["operations"]


def test_supervisor_decision_rejects_unknown_agent() -> None:
    with pytest.raises(ValueError):
        SupervisorDecision(next="hacker", reasoning="x")


@pytest.mark.anyio
async def test_worker_runs_graph_end_to_end() -> None:
    redis = FakeRedis()
    jobs = JobService(redis)  # type: ignore[arg-type]
    graph = build_graph(FakeLLM(["operations", "FINISH"]))
    worker = TaskWorker(jobs, make_graph_processor(graph, 4))
    job = await jobs.create_job("¿maquinaria rota?")
    await worker.process_job(job.job_id)
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.status is JobStatus.DONE
    assert final.result is not None
    assert final.result["answer"].startswith("completion-")


@pytest.mark.anyio
async def test_graph_failure_marks_job_failed() -> None:
    class BrokenLLM(FakeLLM):
        async def parse(self, messages: Any, response_model: type[BaseModel]) -> Any:
            raise TimeoutError("llm down")

    redis = FakeRedis()
    jobs = JobService(redis)  # type: ignore[arg-type]
    worker = TaskWorker(jobs, make_graph_processor(build_graph(BrokenLLM([])), 4))
    job = await jobs.create_job("x")
    await worker.process_job(job.job_id)
    final = await jobs.get_job(job.job_id)
    assert final is not None and final.status is JobStatus.FAILED
    assert final.error == "TimeoutError: llm down"


@pytest.mark.anyio
async def test_missing_api_key_fails_clearly() -> None:
    llm = OpenAILLM(Settings(openai_api_key=None, _env_file=None))  # type: ignore[call-arg]
    with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
        await llm.complete([{"role": "user", "content": "hi"}])


def test_job_model_unchanged() -> None:
    assert JobStatus.DONE.value == "DONE" and Job.model_fields["result"]
