from functools import lru_cache
from typing import Literal

from pydantic import SecretStr

from opspilot_core.config import CoreSettings


class AgentSettings(CoreSettings):
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


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
