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

`POST /tasks` also accepts an optional `establishment_id` (default `demo`). Knowledge searches are always filtered by it, so establishments never see each other's documents.

### RAG (Knowledge Agent)

Docker Compose also starts ChromaDB (persistent volume `chroma_data`; exposed on host port 8001). Documents are chunked with overlap, embedded with OpenAI (`EMBEDDING_MODEL`) and stored with `establishment_id`, `source`, `document_id`, `chunk_id` and `page` (PDF). Retrieval uses `RAG_TOP_K` and discards chunks farther than `RAG_MAX_DISTANCE`.

- On first start (with `OPENAI_API_KEY` set) three **fictitious DEMO** documents in `backend/app/rag/demo_docs/` are ingested for the `demo` establishment. Ingestion is idempotent: unchanged files are skipped, changed files replace their chunks.
- Ingest your own PDF/TXT/MD files:

```bash
docker compose exec backend python -m app.rag.ingest_cli --path /path/in/container --establishment-id finca-1
```

- Results include `sources` (file, document_id, chunk_id, page) for verification. If nothing relevant is found the answer says so.

A Supervisor (LLM) routes to the Operations agent (local **demo** data) and/or the Knowledge agent (RAG over ChromaDB) and synthesizes the answer in `result`.

### Human-in-the-Loop (critical actions)

Asking for a machinery incident (e.g. "registrá una incidencia: la cosechadora pierde presión hidráulica") makes the Supervisor route to the incident action. The graph then **pauses** (`interrupt()`) and the job becomes `WAITING_APPROVAL` with a `pending_action` describing what would be written. Nothing is changed until a human decides:

```bash
curl -X POST localhost:8000/tasks/<job_id>/approve -H "Content-Type: application/json" \
  -H "X-API-Key: $APPROVAL_API_KEY" \
  -d '{"approve": true, "reason": "ok"}'      # or "approve": false to reject
```

`/approve` is protected with the `X-API-Key` header (Swagger: **Authorize**). Set `APPROVAL_API_KEY` in `.env`; if it is empty the endpoint is disabled (`503`), wrong/missing key gives `401`.

Approving writes the (DEMO) incident to Redis exactly once; rejecting changes nothing. A second decision on the same action returns `409`. Other jobs keep running while one waits. The job result reports `action_status` (`APPLIED` / `REJECTED`); if an approved action was applied but the LLM then fails to write the summary, the job is `DONE` with `action_status: APPLIED`, `answer: null` and a `warning` (it is not shown as `FAILED`).

Graph state is checkpointed in Redis (`AsyncRedisSaver`, `thread_id = job_id`), so a waiting job survives backend/Docker restarts and can be approved afterwards. On startup the service reconciles jobs interrupted by a crash using Redis and the checkpoints (resume re-queued, executed action reflected in the job, lost queue entries restored) without ever duplicating the action. This requires **Redis Stack** (RedisJSON + RediSearch); Docker Compose uses `redis/redis-stack-server` with AOF and the `redis_data` volume.

### Tests

```bash
pip install -r backend/requirements-dev.txt
cd backend
pytest
```
