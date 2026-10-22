from typing import Any, Protocol

from pydantic import BaseModel, ValidationError, model_validator

from app.core.errors import AgentOutputInvalid
from app.graph.state import AgroOpsState
from app.schemas.agents import AgentInput, RetrievalMode, SourceRef
from app.services.llm import LLM

SYSTEM_PROMPT = (
    "Sos el Knowledge Agent de AgroOps. Los pasajes numerados son lo ÚNICO que podés usar. "
    "Respondé solo con lo que los pasajes dicen explícitamente: no agregues pasos, cantidades, "
    "frecuencias ni generalizaciones que no estén escritos en ellos. Si los pasajes responden lo "
    "esencial de la consulta, respondé con eso y aclarás qué parte no está cubierta. Citá la "
    "fuente entre corchetes, por ejemplo [archivo.md]. En `supporting_passages` incluí TODOS los "
    "números de los pasajes que aporten a la respuesta, no solo uno. Poné answered=false solo si "
    "NINGÚN pasaje responde la consulta (aunque traten un tema cercano); en ese caso "
    "supporting_passages va vacío y no inventes ni completes con conocimiento externo. Los "
    "documentos pueden ser DEMO/ficticios: no los presentes como manuales oficiales."
)

NO_KNOWLEDGE_MESSAGE = (
    "No relevant information was found in the knowledge base for this establishment."
)


class KnowledgeAnswer(BaseModel):
    answered: bool
    answer: str
    supporting_passages: list[int]  # 1-based indexes of the passages shown to the LLM

    @model_validator(mode="after")
    def _answered_needs_text(self) -> "KnowledgeAnswer":
        if self.answered and not self.answer.strip():
            raise ValueError("answered=true requires a non-empty answer")
        return self


class RetrievedDocument(BaseModel):
    source: str
    content: str
    establishment_id: str
    document_id: str
    chunk_id: str
    page: int | None = None
    distance: float | None = None
    retrieval: RetrievalMode = "vector"


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
    """Verifiable reference to the chunk that backs the answer (validated shape)."""
    return SourceRef(
        source=doc.source,
        document_id=doc.document_id,
        chunk_id=doc.chunk_id,
        page=doc.page,
        distance=doc.distance,
        retrieval=doc.retrieval,
    ).model_dump()


async def knowledge_node(
    state: AgroOpsState, llm: LLM, retriever: KnowledgeRetriever
) -> dict:
    agent_input = AgentInput(
        user_input=state["user_input"], establishment_id=state["establishment_id"]
    )
    docs = await retriever.retrieve(agent_input.user_input, agent_input.establishment_id)
    answer = NO_KNOWLEDGE_MESSAGE
    sources: list[dict[str, Any]] = []
    if docs:
        passages = "\n\n".join(
            f"Pasaje {i} [{d.source}{f', p.{d.page}' if d.page else ''}]\n{d.content}"
            for i, d in enumerate(docs, start=1)
        )
        try:
            result = await llm.parse(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Consulta: {agent_input.user_input}\n\nPasajes:\n{passages}",
                    },
                ],
                KnowledgeAnswer,
            )
        except ValidationError as exc:
            raise AgentOutputInvalid(f"knowledge output: {exc.errors()[0]['msg']}") from exc
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
