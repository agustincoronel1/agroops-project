from typing import Any

from langgraph.types import interrupt

from app.graph.state import AgroOpsState
from app.schemas.actions import ACTION_REGISTER_INCIDENT, MachineryIncidentRequest
from app.services.incident_service import IncidentService
from app.services.llm import LLM

EXTRACT_PROMPT = (
    "Extraé los datos para registrar una incidencia de maquinaria a partir del pedido "
    "del usuario. machine: nombre de la máquina; description: qué ocurrió; severity: "
    "low, medium o high según lo descripto. No inventes datos que el usuario no dio."
)


async def prepare_incident_node(state: AgroOpsState, llm: LLM) -> dict[str, Any]:
    """Step 1 (LLM): build the pending action. No side effects, no interrupt here."""
    request = await llm.parse(
        [
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": state["user_input"]},
        ],
        MachineryIncidentRequest,
    )
    action_id = f"{state['task_id']}-incident"  # deterministic => idempotent execution
    pending = {
        "action_id": action_id,
        "type": ACTION_REGISTER_INCIDENT,
        "establishment_id": state["establishment_id"],
        "payload": request.model_dump(),
        "message": (
            f"Register a {request.severity}-severity incident for machine "
            f"'{request.machine}': {request.description}"
        ),
    }
    return {"pending_action": pending, "current_step": "prepare_incident"}


async def approval_node(state: AgroOpsState, incidents: IncidentService) -> dict[str, Any]:
    """Step 2: human gate. On resume this node re-runs from the top, so everything
    before `interrupt()` is side-effect free and the write below is idempotent."""
    pending = state["pending_action"]
    assert pending is not None
    decision = interrupt(pending)  # pauses the graph; value comes from Command(resume=...)

    if decision.get("approve"):
        incident, created = await incidents.register(
            incident_id=pending["action_id"],
            job_id=state["task_id"],
            establishment_id=pending["establishment_id"],
            request=MachineryIncidentRequest(**pending["payload"]),
        )
        action_result = {
            "status": "executed",
            "incident_id": incident.incident_id,
            "created": created,
        }
        summary = f"Incident {incident.incident_id} registered after human approval."
    else:
        action_result = {"status": "rejected", "reason": decision.get("reason")}
        summary = "Incident registration was rejected by a human; no data was changed."
    return {
        "action_result": action_result,
        "pending_action": None,
        "agent_outputs": {**state["agent_outputs"], "actions": summary},
        "current_step": "approval",
    }
