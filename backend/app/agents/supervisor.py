import json
from typing import Literal

from pydantic import BaseModel

from app.graph.state import AgroOpsState
from app.schemas.actions import action_status_of
from app.services.llm import LLM

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
    "Sos el Supervisor de AgroOps. Decidís qué agente consultar a continuación:\n"
    "- operations: datos operativos del establecimiento (lotes, tareas, maquinaria, "
    "incidencias).\n"
    "- knowledge: base documental del establecimiento (procedimientos, fichas de "
    "maquinaria, descripciones de tareas).\n"
    "- FINISH: ya hay información suficiente para responder (o ningún agente aporta).\n"
    "Podés consultar ambos agentes, de a uno por vez. No repitas un agente ya consultado."
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
            {"role": "system", "content": SYNTHESIS_PROMPT},
            {
                "role": "user",
                "content": f"Consulta: {state['user_input']}\n\nResultados:\n{outputs}",
            },
        ]
    )
    return {
        "next_agent": FINISH,
        "current_step": "supervisor",
        "result": {
            "answer": answer,
            "agents_consulted": consulted,
            "sources": state["context"].get("sources", []),
            "action": state["action_result"],
            "action_status": action_status_of(state["action_result"]),
        },
    }
