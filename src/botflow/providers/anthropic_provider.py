"""Anthropic Claude provider."""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import httpx

from botflow.common.exceptions import ProviderError
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider

logger = get_logger("providers.anthropic")


class AnthropicProvider(BaseProvider):
    """Provider for Anthropic Claude API.

    Converts Anthropic format to/from the unified internal format.
    """

    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(api_key, base_url, extra_config)
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.API_VERSION,
                    "Content-Type": "application/json",
                },
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
        body = self._build_body(messages, model, temperature, max_tokens, stream=False, **kwargs)

        try:
            resp = await self.client.post("/v1/messages", json=body)
            self._check_response(resp, "Anthropic")
            data = resp.json()
            return self._to_unified(data, model)
        except httpx.TimeoutException:
            raise ProviderError(f"Anthropic timed out for model {model}")
        except httpx.RequestError as e:
            raise ProviderError(f"Anthropic request failed: {e}")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        body = self._build_body(messages, model, temperature, max_tokens, stream=True, **kwargs)

        try:
            async with self.client.stream("POST", "/v1/messages", json=body) as resp:
                self._check_response(resp, "Anthropic")
                event_type = ""
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        chunk = json.loads(line[5:].strip())
                        yield self._chunk_to_unified(chunk, model, event_type)
        except httpx.TimeoutException:
            raise ProviderError(f"Anthropic stream timed out for model {model}")
        except httpx.RequestError as e:
            raise ProviderError(f"Anthropic stream failed: {e}")

    async def list_models(self) -> list[dict[str, Any]]:
        # Anthropic does not have a public /v1/models endpoint
        # Return configured model names
        return [{"id": self.extra_config.get("default_model", "claude-sonnet-4-20250514"), "object": "model"}]

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        else:
            body["max_tokens"] = 4096  # Anthropic requires max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        # Extract system prompt if present
        system_msgs = [m for m in messages if m.get("role") == "system"]
        if system_msgs:
            body["system"] = system_msgs[-1]["content"]
            body["messages"] = [m for m in messages if m.get("role") != "system"]
        body.update(kwargs)
        return body

    def _to_unified(self, data: dict, model: str) -> dict[str, Any]:
        """Convert Anthropic response to unified format."""
        content = ""
        tool_calls = None
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
                "total_tokens": (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
                "cache_tokens": 0,  # Anthropic doesn't expose cache_tokens in standard response
            },
            "provider": "anthropic",
        }

    def _chunk_to_unified(self, data: dict, model: str, event_type: str) -> dict[str, Any]:
        """Convert Anthropic SSE chunk to unified format."""
        chunk: dict[str, Any] = {
            "id": data.get("message_id", ""),
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

        if event_type == "message_start" and "message" in data:
            msg = data["message"]
            chunk["id"] = msg.get("id", "")
            chunk["choices"][0]["delta"] = {"role": msg.get("role", "assistant"), "content": ""}

        elif event_type == "content_block_delta":
            delta = data.get("delta", {}) or {}
            chunk["choices"][0]["delta"] = {"content": delta.get("text", "")}

        elif event_type == "message_delta":
            delta = data.get("delta", {}) or {}
            chunk["choices"][0]["finish_reason"] = delta.get("stop_reason")
            usage = data.get("usage", {}) or {}
            chunk["usage"] = {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                "cache_tokens": 0,
            }

        return chunk
