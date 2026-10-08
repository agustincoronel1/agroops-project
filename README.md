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

### Tests

```bash
pip install -r backend/requirements-dev.txt
cd backend
pytest
```
