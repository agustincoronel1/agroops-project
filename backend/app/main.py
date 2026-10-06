from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AgroOps production-grade AI intelligence system.",
)

app.include_router(health_router)


@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    return {
        "message": "AgroOps Intelligence System",
        "docs": "/docs",
    }