# AgroOps Intelligence System

Operational intelligence system for agricultural estates (final project, AI Engineering course).

## Local development

```bash
docker compose up --build
```

Then open:

- http://localhost:8000/docs
- http://localhost:8000/health

Docker Compose currently starts FastAPI (backend) and Redis. Configuration is read from environment variables (see `.env.example`).

### Configuration

Copy `.env.example` to `.env` and set `OPENAI_API_KEY` (and optionally `OPENAI_MODEL`); Docker Compose picks them up. Without a key the API starts, but jobs end as `FAILED` with a clear error.

### Try the multi-agent flow

```bash
curl -X POST localhost:8000/tasks -H "Content-Type: application/json" \
  -d '{"user_input": "¿Qué maquinaria está fuera de servicio y qué incidencias hay abiertas?"}'
curl localhost:8000/tasks/<job_id>
```

A Supervisor (LLM) routes to the Operations agent (local **demo** data) and/or the Knowledge agent (RAG not connected yet, returns no documents) and synthesizes the answer in `result`.

### Tests

```bash
pip install -r backend/requirements-dev.txt
cd backend
pytest
```
