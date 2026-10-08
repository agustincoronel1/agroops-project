"""DEMO operational data. Fictitious; replace with a real data source later."""

from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.core.config import get_settings

DATA_SOURCE = "DEMO"


class Lot(BaseModel):
    lot_id: str
    name: str
    hectares: float
    crop: str
    stage: str


class FarmTask(BaseModel):
    task_id: str
    lot_id: str
    description: str
    due: date
    status: Literal["pending", "in_progress", "done"]


class Machine(BaseModel):
    machine_id: str
    name: str
    status: Literal["operational", "maintenance", "broken"]
    next_service: date


class Incident(BaseModel):
    incident_id: str
    lot_id: str
    description: str
    severity: Literal["low", "medium", "high"]
    resolved: bool


class OperationsSnapshot(BaseModel):
    source: str = DATA_SOURCE
    lots: list[Lot]
    tasks: list[FarmTask]
    machinery: list[Machine]
    incidents: list[Incident]


_LOTS = [
    Lot(lot_id="L1", name="Lote Norte", hectares=120, crop="soja", stage="floración"),
    Lot(lot_id="L2", name="Lote Sur", hectares=85, crop="maíz", stage="V8"),
    Lot(lot_id="L3", name="Lote Río", hectares=60, crop="trigo", stage="macollaje"),
]
_TASKS = [
    FarmTask(task_id="T1", lot_id="L1", description="Aplicación de fungicida",
             due=date(2026, 10, 12), status="pending"),
    FarmTask(task_id="T2", lot_id="L2", description="Fertilización nitrogenada",
             due=date(2026, 10, 9), status="in_progress"),
    FarmTask(task_id="T3", lot_id="L3", description="Control de malezas",
             due=date(2026, 10, 5), status="done"),
]
_MACHINERY = [
    Machine(machine_id="M1", name="Tractor John Deere 7230", status="operational",
            next_service=date(2026, 11, 20)),
    Machine(machine_id="M2", name="Pulverizadora autopropulsada", status="maintenance",
            next_service=date(2026, 10, 10)),
    Machine(machine_id="M3", name="Cosechadora", status="broken",
            next_service=date(2026, 10, 1)),
]
_INCIDENTS = [
    Incident(incident_id="I1", lot_id="L1", description="Presencia de roya en bordes",
             severity="medium", resolved=False),
    Incident(incident_id="I2", lot_id="L2", description="Encharcamiento por lluvias",
             severity="low", resolved=True),
]


def get_lots() -> list[Lot]:
    return list(_LOTS)


def get_tasks() -> list[FarmTask]:
    return list(_TASKS)


def get_machinery() -> list[Machine]:
    return list(_MACHINERY)


def get_incidents() -> list[Incident]:
    return list(_INCIDENTS)


def get_snapshot(establishment_id: str) -> OperationsSnapshot | None:
    """DEMO data belongs only to the demo establishment; others have none (yet)."""
    if establishment_id != get_settings().demo_establishment_id:
        return None
    return OperationsSnapshot(
        lots=get_lots(),
        tasks=get_tasks(),
        machinery=get_machinery(),
        incidents=get_incidents(),
    )
