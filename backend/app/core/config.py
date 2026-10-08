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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
