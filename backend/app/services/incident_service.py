from datetime import UTC, datetime

from redis.asyncio import Redis

from app.schemas.actions import MachineryIncident, MachineryIncidentRequest


def incident_key(incident_id: str) -> str:
    return f"agroops:incident:{incident_id}"


def incident_index_key(establishment_id: str) -> str:
    return f"agroops:incidents:{establishment_id}"


class IncidentService:
    """DEMO critical action: register a machinery incident in Redis (idempotent)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def register(
        self,
        incident_id: str,
        job_id: str,
        establishment_id: str,
        request: MachineryIncidentRequest,
    ) -> tuple[MachineryIncident, bool]:
        """Create the incident once. Returns (incident, created); repeats are no-ops."""
        incident = MachineryIncident(
            incident_id=incident_id,
            job_id=job_id,
            establishment_id=establishment_id,
            created_at=datetime.now(UTC),
            **request.model_dump(),
        )
        # The record written with SET NX is the commit point. The index goes first (SADD is
        # idempotent and list_incidents skips ids without a record), so a Redis error in
        # between never leaves a stored incident that the job reports as failed.
        await self._redis.sadd(incident_index_key(establishment_id), incident_id)
        created = await self._redis.set(
            incident_key(incident_id), incident.model_dump_json(), nx=True
        )
        if not created:
            existing = await self._redis.get(incident_key(incident_id))
            return MachineryIncident.model_validate_json(existing), False
        return incident, True

    async def list_incidents(self, establishment_id: str) -> list[MachineryIncident]:
        ids = await self._redis.smembers(incident_index_key(establishment_id))
        incidents = []
        for incident_id in sorted(ids):
            raw = await self._redis.get(incident_key(incident_id))
            if raw:
                incidents.append(MachineryIncident.model_validate_json(raw))
        return incidents
