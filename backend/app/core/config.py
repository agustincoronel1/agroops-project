from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AgroOps Intelligence System"
    app_version: str = "0.1.0"
    environment: str = "development"
    redis_url: str = "redis://localhost:6379/0"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    graph_max_steps: int = 4
    demo_establishment_id: str = "demo"
    embedding_model: str = "text-embedding-3-small"
    chroma_host: str = "localhost"
    chroma_port: int = 8001
    chroma_collection: str = "agroops_knowledge"
    rag_top_k: int = 4
    rag_max_distance: float = 0.75
    rag_chunk_size: int = 800
    rag_chunk_overlap: int = 120
    ingest_demo_on_startup: bool = True

    @property
    def has_openai_key(self) -> bool:
        return self.openai_api_key is not None and bool(
            self.openai_api_key.get_secret_value()
        )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
