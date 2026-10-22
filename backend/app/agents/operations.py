from pydantic import ValidationError

from app.agents.operations_data import get_snapshot
from app.core.errors import AgentOutputInvalid
from app.graph.state import AgroOpsState
from app.schemas.agents import AgentInput, OperationsAnswer
from app.services.llm import LLM

NO_OPERATIONS_DATA_MESSAGE = (
    "No operational data is available for this establishment."
)

SYSTEM_PROMPT = (
    "Sos el Operations Agent de AgroOps. Analizás datos operativos de un "
    "establecimiento agropecuario (lotes, tareas, maquinaria, incidencias) y "
    "respondés SOLO en base a los datos provistos. Los datos son de DEMOSTRACIÓN: "
    "aclaralo brevemente. Si los datos no alcanzan, decilo. Sé conciso."
)


async def operations_node(state: AgroOpsState, llm: LLM) -> dict:
    agent_input = AgentInput(
        user_input=state["user_input"], establishment_id=state["establishment_id"]
    )
    snapshot = get_snapshot(agent_input.establishment_id)
    if snapshot is None:
        return {
            "agent_outputs": {
                **state["agent_outputs"],
                "operations": NO_OPERATIONS_DATA_MESSAGE,
            },
            "current_step": "operations",
        }
    raw = await llm.complete(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Consulta: {agent_input.user_input}\n\n"
                    f"Datos (DEMO):\n{snapshot.model_dump_json(indent=2)}"
                ),
            },
        ]
    )
    try:
        analysis = OperationsAnswer(answer=raw).answer
    except ValidationError as exc:
        raise AgentOutputInvalid(f"operations output: {exc.errors()[0]['msg']}") from exc
    return {
        "agent_outputs": {**state["agent_outputs"], "operations": analysis},
        "current_step": "operations",
    }
