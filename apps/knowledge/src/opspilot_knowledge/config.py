from functools import lru_cache
from typing import Literal

from opspilot_core.config import CoreSettings


class KnowledgeSettings(CoreSettings):
    # "hash" = offline, no meaning; "ollama" = local multilingual model; "voyage" = Voyage AI API
    embedding_provider: Literal["hash", "ollama", "voyage"] = "hash"
    ollama_embed_url: str = "http://localhost:11434"
    ollama_embed_model: str = "bge-m3"
    knowledge_min_vector: float | None = None  # None = the measured default of the model
    embedding_model: str = "voyage-3.5"
    voyage_api_key: str = ""
    knowledge_top_k: int = 5
    knowledge_seed_on_startup: bool = True
    knowledge_seed_dir: str = ""  # empty = the bundled seed/ folder


@lru_cache
def get_settings() -> KnowledgeSettings:
    return KnowledgeSettings()
