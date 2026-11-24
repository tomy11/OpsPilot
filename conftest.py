"""Tests must not depend on, or spend, the developer's own configuration.

Settings normally read `.env` and the process environment. A real `.env` can hold an API key and
switch on a paid model, so for the whole test session both sources are ignored.
"""

import os

import pytest

from opspilot_agent.config import AgentSettings
from opspilot_core.config import CoreSettings
from opspilot_docflow.config import DocflowSettings
from opspilot_knowledge.config import KnowledgeSettings

SETTINGS = (CoreSettings, AgentSettings, KnowledgeSettings, DocflowSettings)


def pytest_configure(config: pytest.Config) -> None:
    names = {name.upper() for cls in SETTINGS for name in cls.model_fields}
    for name in list(os.environ):
        if name.upper() in names or name.startswith(("ANTHROPIC_", "OLLAMA_", "VOYAGE_")):
            del os.environ[name]
    for cls in SETTINGS:
        cls.model_config["env_file"] = None
