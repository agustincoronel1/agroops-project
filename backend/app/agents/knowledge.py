from typing import Protocol

from pydantic import BaseModel

from app.graph.state import AgroOpsState
from app.services.llm import LLM

SYSTEM_PROMPT = (
    "Sos el Knowledge Agent de AgroOps. Respondés SOLO con base en los pasajes "
    "provistos y citás su fuente. Si no alcanzan, decilo. No inventes información."
)

NO_KNOWLEDGE_MESSAGE = (
    "Knowledge base not connected yet (RAG pending): no documents were retrieved."
)


class RetrievedDocument(BaseModel):
    source: str
    content: str


class KnowledgeRetriever(Protocol):
    async def retrieve(self, query: str) -> list[RetrievedDocument]: ...


class NullRetriever:
    """Placeholder until RAG is implemented: never returns documents."""

    async def retrieve(self, query: str) -> list[RetrievedDocument]:
        return []


async def knowledge_node(
    state: AgroOpsState, llm: LLM, retriever: KnowledgeRetriever
) -> dict:
    docs = await retriever.retrieve(state["user_input"])
    if not docs:
        answer = NO_KNOWLEDGE_MESSAGE
    else:
        passages = "\n\n".join(f"[{d.source}]\n{d.content}" for d in docs)
        answer = await llm.complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Consulta: {state['user_input']}\n\nPasajes:\n{passages}",
                },
            ]
        )
    return {
        "agent_outputs": {**state["agent_outputs"], "knowledge": answer},
        "current_step": "knowledge",
    }
