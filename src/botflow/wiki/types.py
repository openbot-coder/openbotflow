"""BotflowLLM — LangChain BaseChatModel bridge to botflow GroupRouter."""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ChatMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool

from botflow.common.logger import get_logger
from botflow.router import GroupRouter

log = get_logger("wiki.types")

# LangChain types → OpenAI role names
_ROLE_MAP: dict[str, str] = {
    "human": "user",
    "ai": "assistant",
    "system": "system",
    "tool": "tool",
}


class BotflowLLM(BaseChatModel):
    """Bridge botflow's GroupRouter into LangChain's BaseChatModel interface.

    This allows LangChain agents (create_react_agent) to use the botflow
    provider system with weighted routing, cooldown, and fallback.
    """

    model_group: str = "fast"
    router: GroupRouter
    temperature: float | None = None
    max_tokens: int | None = None
    _bound_tools: list[BaseTool] | None = None

    @property
    def _llm_type(self) -> str:
        return "botflow-llm"

    def bind_tools(
        self,
        tools: list[BaseTool],
        **kwargs: Any,
    ) -> BotflowLLM:
        """Bind tools for tool-calling support."""
        result = self.model_copy(deep=False)
        result._bound_tools = list(tools)
        return result

    def with_structured_output(self, *args: Any, **kwargs: Any) -> BotflowLLM:
        """Not implemented — tools are bound via bind_tools."""
        raise NotImplementedError("BotflowLLM does not support with_structured_output.")

    def _convert_messages(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        """Convert LangChain messages to botflow/OpenAI format."""
        import json as _json
        converted = []
        for msg in messages:
            role = _ROLE_MAP.get(msg.type, msg.type)
            if isinstance(msg, ChatMessage):
                converted.append({"role": msg.role, "content": msg.content})
            elif isinstance(msg, AIMessage):
                d: dict[str, Any] = {"role": role, "content": msg.content or ""}
                if msg.tool_calls:
                    openai_tool_calls = []
                    for tc in msg.tool_calls:
                        openai_tool_calls.append({
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": _json.dumps(tc.get("args", {}), ensure_ascii=False),
                            },
                        })
                    d["tool_calls"] = openai_tool_calls
                converted.append(d)
            elif hasattr(msg, "tool_call_id") and msg.tool_call_id:
                converted.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content,
                })
            else:
                converted.append({"role": role, "content": msg.content})
        return converted

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Synchronous generate — not supported, use _agenerate."""
        raise NotImplementedError("BotflowLLM only supports async generation. Use _agenerate.")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Async generate — routes through botflow GroupRouter."""
        converted = self._convert_messages(messages)

        # Build OpenAI-format tool definitions from bound tools
        extra_kwargs: dict[str, Any] = {}
        if self._bound_tools:
            openai_tools: list[dict[str, Any]] = []
            for tool in self._bound_tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.args,
                    },
                })
            extra_kwargs["tools"] = openai_tools
            extra_kwargs["tool_choice"] = "auto"

        result = await self.router.route(
            messages=converted,
            temperature=kwargs.pop("temperature", self.temperature),
            max_tokens=kwargs.pop("max_tokens", self.max_tokens),
            **extra_kwargs,
            **kwargs,
        )

        # Parse botflow response into LangChain ChatResult
        choices = result.get("choices", [])
        if not choices:
            raise ValueError(f"Empty response from router: {result}")

        choice = choices[0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        tool_calls_raw = message.get("tool_calls", [])

        # Convert tool_calls to LangChain format
        tool_calls = []
        for tc in tool_calls_raw:
            func = tc.get("function", {})
            tool_calls.append({
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "args": _parse_json_args(func.get("arguments", "{}")),
            })

        ai_message = AIMessage(
            content=content,
            tool_calls=tool_calls,
            response_metadata={
                "model": result.get("model", ""),
                "provider": result.get("provider", ""),
                "usage": result.get("usage", {}),
            },
        )

        generation = ChatGeneration(message=ai_message)
        return ChatResult(
            generations=[generation],
            llm_output={
                "model": result.get("model", ""),
                "token_usage": result.get("usage", {}),
            },
        )

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_group": self.model_group}


def _parse_json_args(raw: str) -> dict[str, Any]:
    """Safely parse JSON tool call arguments."""
    import json
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"input": str(parsed)}
    except (json.JSONDecodeError, TypeError):
        return {"input": raw}
