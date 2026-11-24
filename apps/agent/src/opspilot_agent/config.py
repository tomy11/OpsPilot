from functools import lru_cache
from typing import Literal

from pydantic import SecretStr

from opspilot_core.config import CoreSettings


class AgentSettings(CoreSettings):
    # Overrides LLM_PROVIDER for the Agent only; Knowledge and DocFlow do not support "ollama".
    agent_llm_provider: Literal["", "fake", "anthropic", "ollama"] = ""
    ollama_api_key: SecretStr = SecretStr("")
    ollama_base_url: str = "https://ollama.com"
    ollama_model: str = "glm-5.2"
    ollama_effort: Literal["high", "max"] = "high"
    ollama_max_tokens: int = 4000  # per model call, thinking included

    # Invite-only chat. 1 credit = 0.001 USD, charged from the tokens a run really used.
    credits_enabled: bool = False
    credit_price_input_usd: float = 1.40  # per 1M tokens, glm-5.2 list price
    credit_price_output_usd: float = 4.40
    credit_max_per_run: int = 150
    credit_daily_runs: int = 50
    credit_monthly_budget_usd: float = 20.0  # all accounts together
    agent_run_timeout_s: float = 300.0
    invite_session_days: int = 30
    invite_token_minutes: int = 15  # access token of invited users; refreshed from the cookie

    erp_base_url: str = "http://localhost:8100"
    erp_timeout_s: float = 5.0
    erp_isolate_runs: bool = False
    demo_staff_password: SecretStr = SecretStr("")
    demo_manager_password: SecretStr = SecretStr("")
    login_max_failures_per_ip: int = 5
    login_max_failures_per_user: int = 20
    login_window_s: float = 900.0

    knowledge_base_url: str = "http://localhost:8200"
    knowledge_timeout_s: float = 10.0

    approval_backend: Literal["temporal", "inline"] = "temporal"
    temporal_host: str = "localhost:7233"
    temporal_task_queue: str = "opspilot-approvals"
    approval_timeout_hours: int = 24

    agent_max_steps: int = 8

    @property
    def provider(self) -> str:
        return self.agent_llm_provider or self.llm_provider


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
