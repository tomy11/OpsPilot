"""LLM client abstraction.

`AnthropicLLM` calls Claude through the official Anthropic SDK (Messages API + client tools).
`FakeLLM` lives in `aiw.agent.fake_llm` and gives a deterministic planner for tests and offline demos.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from aiw.core.config import Settings

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
    pass


class AnthropicLLM:
    def __init__(
        self, settings: Settings, client: "anthropic.AsyncAnthropic | None" = None
    ) -> None:
        import anthropic  # imported lazily: the SDK is heavy and unused in fake/offline mode

        self._sdk = anthropic
        self.settings = settings
        self.client = client or anthropic.AsyncAnthropic()

    async def respond(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        s = self.settings
        anthropic = self._sdk
        params: dict[str, Any] = {
            "model": s.llm_model,
            "max_tokens": s.llm_max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": s.llm_effort},
        }
        try:
            if s.llm_server_fallback:
                # Server-side fallback: if the model declines, the API re-runs on a fallback model.
                response = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
                )
            else:
                response = await self.client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise LLMError("LLM rate limited, retry later") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"LLM API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("LLM connection error") from exc

        if response.stop_reason == "refusal":
            return LLMTurn(content=[], text="ขออภัย ไม่สามารถดำเนินการคำขอนี้ได้", stop_reason="refusal")
        if response.stop_reason == "max_tokens":
            raise LLMError("LLM response truncated (max_tokens)")

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
            usage=response.usage.model_dump(mode="json", exclude_none=True),
        )
