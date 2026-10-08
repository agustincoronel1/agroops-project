from typing import Any, Protocol

from pydantic import BaseModel

from app.graph.state import AgroOpsState
from app.services.llm import LLM

SYSTEM_PROMPT = (
    "Sos el Knowledge Agent de AgroOps. Los pasajes numerados son lo UNICO que podés "
    "usar. Respondé la consulta solo con lo que los pasajes dicen explícitamente y "
    "citá la fuente entre corchetes, por ejemplo [archivo.md]. Devolvé en "
    "`supporting_passages` los números de los pasajes que realmente sustentan tu "
    "respuesta. Si los pasajes NO contienen la información pedida (aunque traten un "
    "tema cercano), poné answered=false y supporting_passages vacío: no inventes ni "
    "completes con conocimiento externo. Los documentos pueden ser DEMO/ficticios: no "
    "los presentes como manuales oficiales."
)

NO_KNOWLEDGE_MESSAGE = (
    "No relevant information was found in the knowledge base for this establishment."
)


class KnowledgeAnswer(BaseModel):
    answered: bool
    answer: str
    supporting_passages: list[int]  # 1-based indexes of the passages shown to the LLM


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
    answer = NO_KNOWLEDGE_MESSAGE
    sources: list[dict[str, Any]] = []
    if docs:
        passages = "\n\n".join(
            f"Pasaje {i} [{d.source}{f', p.{d.page}' if d.page else ''}]\n{d.content}"
            for i, d in enumerate(docs, start=1)
        )
        result = await llm.parse(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Consulta: {state['user_input']}\n\nPasajes:\n{passages}",
                },
            ],
            KnowledgeAnswer,
        )
        # Keep only valid, de-duplicated indexes: sources must back the answer.
        used = sorted({i for i in result.supporting_passages if 1 <= i <= len(docs)})
        if result.answered and used:
            answer = result.answer
            sources = [to_source(docs[i - 1]) for i in used]
    return {
        "agent_outputs": {**state["agent_outputs"], "knowledge": answer},
        "context": {**state["context"], "sources": sources},
        "current_step": "knowledge",
    }
