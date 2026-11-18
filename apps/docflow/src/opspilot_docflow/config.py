from functools import lru_cache

from opspilot_core.config import CoreSettings


class DocflowSettings(CoreSettings):
    erp_base_url: str = "http://localhost:8100"
    erp_timeout_s: float = 5.0
    max_upload_mb: int = 10
    vat_rate: float = 0.07
    # Clean documents (no errors, no warnings) at or below this total post without a human.
    # Mirrors the expense policy: up to 5,000 THB needs only a supervisor. 0 = always review.
    auto_post_limit: float = 5000.0


@lru_cache
def get_settings() -> DocflowSettings:
    return DocflowSettings()
