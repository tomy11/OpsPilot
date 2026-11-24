"""LLM client abstraction.

`AnthropicLLM` calls Claude through the official Anthropic SDK (Messages API + client tools).
`OllamaLLM` reuses it against Ollama Cloud's Anthropic-compatible endpoint.
`FakeLLM` lives in `opspilot_agent.fake_llm` and gives a deterministic planner for tests and offline demos.
"""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from opspilot_core.config import CoreSettings

if TYPE_CHECKING:
    import anthropic


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMTurn:
    # Assistant content blocks exactly as returned, to append back into the conversation.
    content: list[dict[str, Any]]
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    async def respond(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn: ...


class LLMError(Exception):
    """`usage` is set when the provider answered (and billed) but the answer is unusable."""

    def __init__(self, message: str, *, usage: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.usage = usage or {}


class AnthropicLLM:
    def __init__(
        self, settings: CoreSettings, client: "anthropic.AsyncAnthropic | None" = None
    ) -> None:
        import anthropic  # imported lazily: the SDK is heavy and unused in fake/offline mode

        self._sdk = anthropic
        self.settings = settings
        self.client = client or anthropic.AsyncAnthropic()

    async def _create(self, params: dict[str, Any]) -> Any:
        anthropic = self._sdk
        try:
            if self.settings.llm_server_fallback:
                # Server-side fallback: if the model declines, the API re-runs on a fallback model.
                return await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
                )
            return await self.client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise LLMError("LLM rate limited, retry later") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"LLM API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("LLM connection error") from exc

    async def structured(
        self, *, system: str, content: list[dict[str, Any]], schema: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One user turn -> JSON matching `schema` (structured outputs). Returns (data, usage)."""
        s = self.settings
        response = await self._create(
            {
                "model": s.llm_model,
                "max_tokens": s.llm_max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": content}],
                "output_config": {
                    "effort": s.llm_effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            }
        )
        if response.stop_reason == "refusal":
            raise LLMError("model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("LLM response truncated (max_tokens)")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text), response.usage.model_dump(mode="json", exclude_none=True)

    async def respond(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        s = self.settings
        params: dict[str, Any] = {
            "model": s.llm_model,
            "max_tokens": s.llm_max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": s.llm_effort},
        }
        if tools:
            params["tools"] = tools
        response = await self._create(params)
        usage = response.usage.model_dump(mode="json", exclude_none=True)

        if response.stop_reason == "refusal":
            return LLMTurn(
                content=[],
                text="ขออภัย ไม่สามารถดำเนินการคำขอนี้ได้",
                stop_reason="refusal",
                usage=usage,
            )
        if response.stop_reason == "max_tokens":
            raise LLMError("LLM response truncated (max_tokens)", usage=usage)

        content = [
            b.model_dump(by_alias=True, exclude_none=True, mode="json") for b in response.content
        ]
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        text = "\n".join(b.text for b in response.content if b.type == "text")
        return LLMTurn(
            content=content,
            tool_calls=tool_calls,
            text=text,
            stop_reason=response.stop_reason,
            usage=usage,
        )


def _is_ollama_com(base_url: str) -> bool:
    host = urlsplit(base_url).hostname or ""
    return host == "ollama.com" or host.endswith(".ollama.com")


class OllamaLLM(AnthropicLLM):
    """Open models (GLM) on Ollama Cloud through its Anthropic-compatible `/v1/messages`.

    Either through your own signed-in Ollama server (`glm-5.3-flash:cloud`, no key; the server
    ignores the token) or https://ollama.com directly (`glm-5.3-flash`, API key).

    Differences from Claude, per https://docs.ollama.com/api/anthropic-compatibility:
    Bearer auth, `anthropic-version: 2023-06-01`, no server-side fallback, no `strict` tools.
    Tool arguments are still validated by Pydantic before anything runs. The SDK does not retry
    here: a retried call could be billed twice while only one usage record comes back.
    """

    def __init__(
        self,
        settings: CoreSettings,
        *,
        api_key: str,
        base_url: str,
        model: str,
        effort: str,
        max_tokens: int,
        timeout_s: float = 120.0,
        http_client: Any = None,
    ) -> None:
        if _is_ollama_com(base_url):
            if not api_key:
                raise ValueError("OLLAMA_API_KEY is required for https://ollama.com")
            model = model.removesuffix(":cloud")  # hosted names have no suffix
        else:
            api_key = "ollama"  # a local server needs no key, so never hand it the real one
        import anthropic

        super().__init__(
            settings.model_copy(
                update={
                    "llm_model": model,
                    "llm_effort": effort,
                    "llm_max_tokens": max_tokens,
                    "llm_server_fallback": False,
                }
            ),
            anthropic.AsyncAnthropic(
                auth_token=api_key,
                base_url=base_url,
                # Never let an ANTHROPIC_API_KEY in the environment travel to another provider.
                default_headers={
                    "anthropic-version": "2023-06-01",
                    "X-Api-Key": anthropic.Omit(),
                },
                max_retries=0,
                timeout=timeout_s,
                http_client=http_client,
            ),
        )

    async def respond(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        tools = [{k: v for k, v in tool.items() if k != "strict"} for tool in tools]
        return await super().respond(system=system, messages=messages, tools=tools)
