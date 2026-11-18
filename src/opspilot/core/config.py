from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    jwt_secret: str = "change-me-dev-only"
    jwt_ttl_minutes: int = 60

    database_url: str = "postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot"
    erp_base_url: str = "http://localhost:8100"
    erp_timeout_s: float = 5.0

    approval_backend: Literal["temporal", "inline"] = "temporal"
    temporal_host: str = "localhost:7233"
    temporal_task_queue: str = "opspilot-approvals"
    approval_timeout_hours: int = 24

    llm_provider: Literal["anthropic", "fake"] = "fake"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 16000
    llm_server_fallback: bool = True
    agent_max_steps: int = 8

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
