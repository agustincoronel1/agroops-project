from app.agents.operations_data import get_snapshot
from app.graph.state import AgroOpsState
from app.services.llm import LLM

SYSTEM_PROMPT = (
    "Sos el Operations Agent de AgroOps. Analizás datos operativos de un "
    "establecimiento agropecuario (lotes, tareas, maquinaria, incidencias) y "
    "respondés SOLO en base a los datos provistos. Los datos son de DEMOSTRACIÓN: "
    "aclaralo brevemente. Si los datos no alcanzan, decilo. Sé conciso."
)


async def operations_node(state: AgroOpsState, llm: LLM) -> dict:
    snapshot = get_snapshot()
    analysis = await llm.complete(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Consulta: {state['user_input']}\n\n"
                    f"Datos (DEMO):\n{snapshot.model_dump_json(indent=2)}"
                ),
            },
        ]
    )
    return {
        "agent_outputs": {**state["agent_outputs"], "operations": analysis},
        "current_step": "operations",
    }
