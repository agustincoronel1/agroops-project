import json
from typing import Literal

from pydantic import BaseModel

from app.graph.state import AgroOpsState
from app.services.llm import LLM

AgentName = Literal["operations", "knowledge"]
AGENTS: tuple[AgentName, ...] = ("operations", "knowledge")
FINISH = "FINISH"

ROUTER_PROMPT = (
    "Sos el Supervisor de AgroOps. Decidís qué agente consultar a continuación:\n"
    "- operations: datos operativos del establecimiento (lotes, tareas, maquinaria, "
    "incidencias).\n"
    "- knowledge: conocimiento técnico/documental (buenas prácticas, normativas). "
    "Actualmente la base documental aún no está conectada.\n"
    "- FINISH: ya hay información suficiente para responder (o ningún agente aporta).\n"
    "Podés consultar ambos agentes, de a uno por vez. No repitas un agente ya consultado."
)

SYNTHESIS_PROMPT = (
    "Sos el Supervisor de AgroOps. Redactá la respuesta final al usuario sintetizando "
    "los resultados de los agentes. No inventes datos. Si los datos son DEMO o la base "
    "de conocimiento no está disponible, aclaralo."
)


class SupervisorDecision(BaseModel):
    next: Literal["operations", "knowledge", "FINISH"]
    reasoning: str


async def supervisor_node(state: AgroOpsState, llm: LLM, max_steps: int) -> dict:
    """Route to the next agent, or synthesize the final answer when done."""
    consulted = list(state["agent_outputs"])
    next_agent: str = FINISH

    if state["steps"] < max_steps and len(consulted) < len(AGENTS):
        decision = await llm.parse(
            [
                {"role": "system", "content": ROUTER_PROMPT},
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
        if decision.next != FINISH and decision.next not in consulted:
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
        "result": {"answer": answer, "agents_consulted": consulted},
    }
