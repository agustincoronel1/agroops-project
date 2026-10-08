from typing import Any, Protocol

from pydantic import BaseModel

from app.graph.state import AgroOpsState
from app.services.llm import LLM

SYSTEM_PROMPT = (
    "Sos el Knowledge Agent de AgroOps. Respondés SOLO con base en los pasajes "
    "provistos y citás su fuente entre corchetes, por ejemplo [archivo.md]. Si los "
    "pasajes no responden la consulta, decilo. No inventes información. Los "
    "documentos pueden ser DEMO/ficticios: no los presentes como manuales oficiales."
)

NO_KNOWLEDGE_MESSAGE = (
    "No relevant information was found in the knowledge base for this establishment."
)


class RetrievedDocument(BaseModel):
    source: str
    content: str
    establishment_id: str
    document_id: str
    chunk_id: str
    page: int | None = None
    distance: float | None = None


class KnowledgeRetriever(Protocol):
    async def retrieve(
        self, query: str, establishment_id: str
    ) -> list[RetrievedDocument]: ...


class NullRetriever:
    """Retriever that never finds anything (used when RAG is not wired)."""

    async def retrieve(
        self, query: str, establishment_id: str
    ) -> list[RetrievedDocument]:
        return []


def to_source(doc: RetrievedDocument) -> dict[str, Any]:
    """Verifiable reference to the chunk that backs the answer."""
    return {
        "source": doc.source,
        "document_id": doc.document_id,
        "chunk_id": doc.chunk_id,
        "page": doc.page,
        "distance": doc.distance,
    }


async def knowledge_node(
    state: AgroOpsState, llm: LLM, retriever: KnowledgeRetriever
) -> dict:
    docs = await retriever.retrieve(state["user_input"], state["establishment_id"])
    if not docs:
        answer = NO_KNOWLEDGE_MESSAGE
    else:
        passages = "\n\n".join(
            f"[{d.source}{f', p.{d.page}' if d.page else ''}]\n{d.content}" for d in docs
        )
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
        "context": {**state["context"], "sources": [to_source(d) for d in docs]},
        "current_step": "knowledge",
    }
