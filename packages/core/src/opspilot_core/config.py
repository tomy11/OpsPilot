"""Settings shared by every OpsPilot service. Apps subclass `CoreSettings` with their own fields."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    # Every service verifies the same HS256 tokens; replace with an OIDC issuer + JWKS in prod.
    jwt_secret: str = "change-me-dev-only"
    jwt_ttl_minutes: int = 60

    database_url: str = "postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot"

    llm_provider: Literal["anthropic", "fake"] = "fake"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 16000
    llm_server_fallback: bool = True

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
