import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.core.errors import AgentOutputInvalid
from app.graph.state import AgroOpsState
from app.schemas.actions import action_status_of
from app.schemas.agents import JobResult
from app.services.llm import LLM

logger = logging.getLogger(__name__)

# A bracketed file name such as [procedimientos.md]; other brackets are not citations.
FILE_CITATION = re.compile(r"\[([^\[\]\n]{1,200}\.[A-Za-z0-9]{1,5})\]")

AgentName = Literal["operations", "knowledge", "register_incident"]
AGENTS: tuple[AgentName, ...] = ("operations", "knowledge")
# Key each route leaves in state["agent_outputs"] (used to avoid re-routing).
OUTPUT_KEY: dict[str, str] = {
    "operations": "operations",
    "knowledge": "knowledge",
    "register_incident": "actions",
}
FINISH = "FINISH"

ROUTER_PROMPT = (
    "Sos el Supervisor de AgroOps. Decidís qué agente consultar a continuación, eligiendo "
    "el MÍNIMO necesario:\n"
    "- operations: solo si la consulta pide datos operativos del establecimiento (estado de "
    "maquinaria, tareas pendientes, lotes, incidencias).\n"
    "- knowledge: solo si la consulta pide cómo hacer algo, procedimientos o documentación.\n"
    "- FINISH: si ningún agente aporta a la consulta (por ejemplo, un tema general), o si "
    "ya hay información suficiente.\n"
    "Consultá ambos agentes solo si la consulta mezcla los dos temas, de a uno por vez. "
    "No repitas un agente ya consultado."
)

INCIDENT_ROUTE_PROMPT = (
    "\n- register_incident: SOLO si el usuario pide explícitamente registrar/reportar una "
    "incidencia de maquinaria (acción crítica: requiere aprobación humana)."
)

SYNTHESIS_PROMPT = (
    "Sos el Supervisor de AgroOps. Redactá la respuesta final al usuario sintetizando "
    "los resultados de los agentes. No inventes datos. Si los datos o documentos son DEMO, "
    "aclaralo. Si el Knowledge Agent no encontró información, reconocelo explícitamente. "
    "Citá las fuentes documentales entre corchetes."
)


def synthesis_system_prompt(sources: list[dict]) -> str:
    """Synthesis prompt that names the ONLY citations allowed (real document names).

    Without the list the model invents generic citations such as "[Fuente: Knowledge Agent]".
    """
    names = sorted({str(s["source"]) for s in sources if s.get("source")})
    if not names:
        return (
            SYNTHESIS_PROMPT
            + " No hay documentos recuperados para esta consulta: no cites ningún documento."
        )
    allowed = ", ".join(f"[{name}]" for name in names)
    return (
        SYNTHESIS_PROMPT
        + f" Los únicos documentos citables son: {allowed}. Citá solo esos nombres de archivo"
        " exactos entre corchetes. Nunca cites otra cosa (por ejemplo, nombres de agentes)."
    )


def strip_unknown_citations(text: str, allowed: set[str]) -> str:
    """Remove file citations that do not match a retrieved document (never invent sources)."""
    removed: list[str] = []

    def keep_or_drop(match: re.Match[str]) -> str:
        if match.group(1) in allowed:
            return match.group(0)
        removed.append(match.group(1))
        return ""

    cleaned = FILE_CITATION.sub(keep_or_drop, text)
    if removed:
        logger.warning("Removed %d citation(s) without a retrieved source", len(removed))
    return cleaned


class SupervisorDecision(BaseModel):
    next: Literal["operations", "knowledge", "register_incident", "FINISH"]
    reasoning: str


async def supervisor_node(
    state: AgroOpsState,
    llm: LLM,
    max_steps: int,
    allowed: tuple[str, ...] = AGENTS,
) -> dict:
    """Route to the next agent, or synthesize the final answer when done."""
    consulted = list(state["agent_outputs"])
    next_agent: str = FINISH

    candidates = [a for a in allowed if OUTPUT_KEY[a] not in consulted]
    if state["steps"] < max_steps and candidates:
        router_prompt = ROUTER_PROMPT + (
            INCIDENT_ROUTE_PROMPT if "register_incident" in allowed else ""
        )
        decision = await llm.parse(
            [
                {"role": "system", "content": router_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Consulta: {state['user_input']}\n"
                        f"Agentes ya consultados: {consulted or 'ninguno'}"
                    ),
                },
            ],
            SupervisorDecision,
        )
        # Guard against loops: never re-route to an agent that already ran.
        if decision.next in candidates:
            next_agent = decision.next

    if next_agent != FINISH:
        return {
            "next_agent": next_agent,
            "steps": state["steps"] + 1,
            "current_step": "supervisor",
        }

    outputs = json.dumps(state["agent_outputs"], ensure_ascii=False, indent=2)
    answer = await llm.complete(
        [
            {
                "role": "system",
                "content": synthesis_system_prompt(state["context"].get("sources", [])),
            },
            {
                "role": "user",
                "content": f"Consulta: {state['user_input']}\n\nResultados:\n{outputs}",
            },
        ]
    )
    sources = state["context"].get("sources", [])
    allowed = {str(s["source"]) for s in sources if s.get("source")}
    try:
        result = JobResult(
            answer=strip_unknown_citations(answer, allowed),
            agents_consulted=consulted,
            sources=sources,
            action=state["action_result"],
            action_status=action_status_of(state["action_result"]),
        )
    except ValidationError as exc:
        raise AgentOutputInvalid(f"final answer: {exc.errors()[0]['msg']}") from exc
    return {
        "next_agent": FINISH,
        "current_step": "supervisor",
        "result": result.model_dump(mode="json"),
    }
