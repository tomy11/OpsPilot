"""What actually goes over the wire to Ollama Cloud, captured with a mock transport."""

import json

import httpx2
import pytest

from opspilot_core.config import CoreSettings
from opspilot_core.llm import LLMError, OllamaLLM

TOOL = {"name": "get_order", "input_schema": {"type": "object"}, "strict": True}


def _llm(
    seen: dict,
    *,
    stop: str = "tool_use",
    base_url: str = "https://ollama.com",
    model: str = "glm-5.3-flash:cloud",
) -> OllamaLLM:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(
            url=str(request.url),
            headers={k.lower(): v for k, v in request.headers.items()},
            body=json.loads(request.content),
        )
        return httpx2.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "glm-5.2",
                "content": [
                    {"type": "thinking", "thinking": "hm", "signature": ""},
                    {"type": "tool_use", "id": "t1", "name": "get_order", "input": {"a": 1}},
                ],
                "stop_reason": stop,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    return OllamaLLM(
        CoreSettings(_env_file=None),
        api_key="ollama-dummy",
        base_url=base_url,
        model=model,
        effort="high",
        max_tokens=4000,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


async def _ask(llm: OllamaLLM):
    return await llm.respond(
        system="s", messages=[{"role": "user", "content": "x"}], tools=[dict(TOOL)]
    )


async def test_request_matches_ollama_and_leaks_no_claude_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-claude-secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.invalid")
    seen: dict = {}
    turn = await _ask(_llm(seen))

    assert seen["url"] == "https://ollama.com/v1/messages"
    assert seen["headers"]["authorization"] == "Bearer ollama-dummy"
    assert "x-api-key" not in seen["headers"] and "sk-claude" not in json.dumps(seen)
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert "anthropic-beta" not in seen["headers"] and "fallbacks" not in seen["body"]
    assert seen["body"]["model"] == "glm-5.3-flash"  # hosted name, without ":cloud"
    assert seen["body"]["max_tokens"] == 4000
    assert seen["body"]["output_config"] == {"effort": "high"}
    assert "strict" not in seen["body"]["tools"][0]
    assert turn.tool_calls[0].name == "get_order" and turn.usage["output_tokens"] == 5


async def test_local_server_gets_the_cloud_name_and_never_the_real_key():
    seen: dict = {}
    await _ask(_llm(seen, base_url="http://localhost:11434"))
    assert seen["url"] == "http://localhost:11434/v1/messages"
    assert seen["body"]["model"] == "glm-5.3-flash:cloud"
    assert "ollama-dummy" not in json.dumps(seen)


async def test_truncated_answer_still_reports_billed_usage():
    with pytest.raises(LLMError) as err:
        await _ask(_llm({}, stop="max_tokens"))
    assert err.value.usage["output_tokens"] == 5


def test_key_is_required():
    with pytest.raises(ValueError, match="OLLAMA_API_KEY"):
        OllamaLLM(
            CoreSettings(_env_file=None),
            api_key="",
            base_url="https://ollama.com",
            model="m",
            effort="high",
            max_tokens=1,
        )
