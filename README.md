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

### Tests

```bash
pip install -r backend/requirements-dev.txt
cd backend
pytest
```
