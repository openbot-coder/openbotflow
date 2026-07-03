"""Anthropic Claude provider.

Uses the official anthropic SDK for proper streaming, tool use,
and extended thinking support.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from anthropic import AsyncAnthropic

from botflow.common.exceptions import ProviderError
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider

logger = get_logger("providers.anthropic")


class AnthropicProvider(BaseProvider):
    """Provider for Anthropic Claude API.

    Uses the official anthropic SDK for proper connection pooling,
    retry logic, and protocol compliance.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(api_key, base_url, extra_config)
        self._client: AsyncAnthropic | None = None

    @property
    def client(self) -> AsyncAnthropic:
        """Get or create the Anthropic client (lazy initialization)."""
        if self._client is None:
            self._client = AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.extra_config.get("timeout", 120.0),
            )
        return self._client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        system, messages = self._extract_system(messages)
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or 4096,
        }
        if system:
            params["system"] = system
        if temperature is not None:
            params["temperature"] = temperature
        params.update(kwargs)

        try:
            response = await self.client.messages.create(**params)
            return self._to_unified(response.model_dump(), model)
        except Exception as e:
            raise ProviderError(f"Anthropic request failed: {e}")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        system, messages = self._extract_system(messages)
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or 4096,
        }
        if system:
            params["system"] = system
        if temperature is not None:
            params["temperature"] = temperature
        params.update(kwargs)

        try:
            async with self.client.messages.stream(**params) as stream:
                async for event in stream:
                    yield self._event_to_chunk(event, model)
        except Exception as e:
            raise ProviderError(f"Anthropic stream failed: {e}")

    async def list_models(self) -> list[dict[str, Any]]:
        # Anthropic does not have a public /v1/models endpoint
        return [{"id": self.extra_config.get("default_model", "claude-sonnet-4-20250514"), "object": "model"}]

    def _extract_system(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict]]:
        """Extract system message from messages (Anthropic uses separate system param)."""
        system = ""
        filtered = []
        for msg in messages:
            if msg.get("role") == "system":
                system = msg.get("content", "")
            else:
                filtered.append(msg)
        return system, filtered

    def _to_unified(self, data: dict, model: str) -> dict[str, Any]:
        """Convert Anthropic response to unified format."""
        content = ""
        content_blocks = data.get("content", []) or []
        text_blocks = [b for b in content_blocks if b.get("type") == "text"]
        if text_blocks:
            content = text_blocks[0].get("text", "")

        usage = data.get("usage", {}) or {}

        return {
            "id": data.get("id", ""),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": data.get("stop_reason", "end_turn"),
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                "cache_tokens": usage.get("cache_read_input_tokens", 0),
            },
            "provider": "anthropic",
        }

    def _event_to_chunk(self, event: Any, model: str) -> dict[str, Any]:
        """Convert Anthropic streaming event to unified chunk format."""
        chunk: dict[str, Any] = {
            "id": "",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": None,
                }
            ],
            "usage": None,
            "provider": "anthropic",
        }

        # Handle different event types
        event_type = getattr(event, "type", "")

        if event_type == "message_start" and hasattr(event, "message"):
            msg = event.message
            chunk["id"] = getattr(msg, "id", "")
            chunk["choices"][0]["delta"] = {
                "role": getattr(msg, "role", "assistant"),
                "content": "",
            }

        elif event_type == "content_block_delta" and hasattr(event, "delta"):
            delta = event.delta
            if hasattr(delta, "text"):
                chunk["choices"][0]["delta"] = {"content": delta.text}
            # Handle thinking content
            elif hasattr(delta, "thinking"):
                chunk["choices"][0]["delta"] = {"reasoning_content": delta.thinking}

        elif event_type == "message_delta" and hasattr(event, "delta"):
            delta = event.delta
            chunk["choices"][0]["finish_reason"] = getattr(delta, "stop_reason", None)
            # Usage is typically on the final event
            if hasattr(event, "usage"):
                usage = event.usage
                chunk["usage"] = {
                    "prompt_tokens": getattr(usage, "input_tokens", 0),
                    "completion_tokens": getattr(usage, "output_tokens", 0),
                    "total_tokens": getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0),
                    "cache_tokens": getattr(usage, "cache_read_input_tokens", 0),
                }

        elif event_type == "content_block_start" and hasattr(event, "content_block"):
            block = event.content_block
            if getattr(block, "type", "") == "tool_use":
                chunk["choices"][0]["delta"] = {
                    "tool_calls": [{
                        "id": getattr(block, "id", ""),
                        "type": "function",
                        "function": {
                            "name": getattr(block, "name", ""),
                            "arguments": "",
                        },
                    }]
                }

        return chunk
