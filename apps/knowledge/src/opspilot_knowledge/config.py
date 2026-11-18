from functools import lru_cache
from typing import Literal

from opspilot_core.config import CoreSettings


class KnowledgeSettings(CoreSettings):
    # "hash" = offline deterministic embeddings, "voyage" = Voyage AI embeddings API
    embedding_provider: Literal["hash", "voyage"] = "hash"
    embedding_model: str = "voyage-3.5"
    voyage_api_key: str = ""
    knowledge_top_k: int = 5
    knowledge_seed_on_startup: bool = True
    knowledge_seed_dir: str = ""  # empty = the bundled seed/ folder


@lru_cache
def get_settings() -> KnowledgeSettings:
    return KnowledgeSettings()
