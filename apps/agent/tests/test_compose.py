"""Every setting documented for operators must actually reach the containers."""

from pathlib import Path

import pytest

from opspilot_agent.config import AgentSettings

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
NAMES = {line.split("=")[0] for line in EXAMPLE.splitlines() if line[:1].isupper() and "=" in line}


@pytest.mark.parametrize(
    "name", sorted(n for n in NAMES if n.startswith(("CREDIT", "OLLAMA", "AGENT_LLM")))
)
def test_chat_settings_in_env_example_reach_the_containers(name):
    assert f"${{{name}:-" in COMPOSE


def test_empty_variables_mean_the_default(monkeypatch):
    for name in ("CREDIT_MAX_PER_RUN", "CREDIT_PRICE_INPUT_USD", "LOGIN_WINDOW_S"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("CREDIT_DAILY_RUNS", "7")
    s = AgentSettings()
    assert (s.credit_max_per_run, s.credit_price_input_usd, s.credit_daily_runs) == (150, None, 7)
