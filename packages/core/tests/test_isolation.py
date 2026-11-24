"""The guard in the root conftest.py: a developer's `.env` must never reach a test."""

from pathlib import Path

import pytest

from opspilot_agent.config import AgentSettings
from opspilot_docflow.config import DocflowSettings
from opspilot_knowledge.config import KnowledgeSettings


@pytest.mark.parametrize("cls", [AgentSettings, KnowledgeSettings, DocflowSettings])
def test_settings_ignore_dotenv(cls, tmp_path: Path, monkeypatch):
    (tmp_path / ".env").write_text(
        "JWT_SECRET=from-dotenv\nAGENT_LLM_PROVIDER=ollama\nOLLAMA_API_KEY=real\n"
    )
    monkeypatch.chdir(tmp_path)
    settings = cls()
    assert settings.jwt_secret != "from-dotenv"
    assert settings.llm_provider == "fake"
    if isinstance(settings, AgentSettings):
        assert settings.provider == "fake" and not settings.ollama_api_key.get_secret_value()
