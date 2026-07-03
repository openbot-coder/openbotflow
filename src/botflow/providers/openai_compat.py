"""OpenAI-compatible provider (OpenAI, Azure, vLLM, Ollama)."""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import httpx

from botflow.common.exceptions import ProviderError
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider

logger = get_logger("providers.openai")


class OpenAICompatProvider(BaseProvider):
    """Provider for OpenAI-compatible APIs.

    Supports: OpenAI API, Azure OpenAI, vLLM, Ollama, and any OpenAI-compatible endpoint.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(api_key, base_url, extra_config)
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            if "azure" in self.extra_config.get("mode", ""):
                headers = {
                    "api-key": self.api_key,
                    "Content-Type": "application/json",
                }
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
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
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        body.update(kwargs)

        try:
            resp = await self.client.post("/chat/completions", json=body)
            self._check_response(resp, "OpenAICompat")
            data = resp.json()
            return self._to_unified(data, model, "openai")
        except httpx.TimeoutException:
            raise ProviderError(f"OpenAICompat provider timed out for model {model}")
        except httpx.RequestError as e:
            raise ProviderError(f"OpenAICompat request failed: {e}")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        body.update(kwargs)

        try:
            async with self.client.stream("POST", "/chat/completions", json=body) as resp:
                self._check_response(resp, "OpenAICompat")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        chunk = json.loads(line[6:])
                        yield self._chunk_to_unified(chunk, model, "openai")
        except httpx.TimeoutException:
            raise ProviderError(f"OpenAICompat stream timed out for model {model}")
        except httpx.RequestError as e:
            raise ProviderError(f"OpenAICompat stream failed: {e}")

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            resp = await self.client.get("/models")
            self._check_response(resp, "OpenAICompat")
            data = resp.json()
            return data.get("data", [])
        except httpx.RequestError as e:
            logger.warning("Failed to list models from {}: {}", self.base_url, e)
            return []

    def _to_unified(self, data: dict, model: str, provider: str) -> dict[str, Any]:
        """Convert OpenAI response to unified format."""
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        usage = data.get("usage", {}) or {}
        prompt_details = usage.get("prompt_tokens_details", {}) or {}

        return {
            "id": data.get("id", ""),
            "model": data.get("model", model),
            "choices": [
                {
                    "index": choice.get("index", 0),
                    "message": {
                        "role": msg.get("role", "assistant"),
                        "content": msg.get("content", ""),
                    },
                    "finish_reason": choice.get("finish_reason", "stop"),
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cache_tokens": prompt_details.get("cached_tokens", 0),
            },
            "provider": provider,
        }

    def _chunk_to_unified(self, data: dict, model: str, provider: str) -> dict[str, Any]:
        """Convert OpenAI stream chunk to unified format."""
        raw_choices = data.get("choices") or []
        choice = raw_choices[0] if raw_choices and raw_choices[0] is not None else {}
        delta = choice.get("delta") or {}

        chunk = {
            "id": data.get("id", ""),
            "object": "chat.completion.chunk",
            "model": data.get("model", model),
            "choices": [
                {
                    "index": choice.get("index", 0),
                    "delta": {
                        "role": delta.get("role") or "assistant",
                        "content": delta.get("content") or "",
                    },
                    "finish_reason": choice.get("finish_reason"),
                }
            ],
            "usage": None,
            "provider": provider,
        }

        # Forward tool_calls if present
        if delta.get("tool_calls"):
            chunk["choices"][0]["delta"]["tool_calls"] = delta["tool_calls"]

        # Forward reasoning_content (thinking process) if present
        if delta.get("reasoning_content") is not None:
            chunk["choices"][0]["delta"]["reasoning_content"] = delta["reasoning_content"]

        # Include usage on final chunk if provided
        usage = data.get("usage")
        if usage:
            prompt_details = usage.get("prompt_tokens_details", {}) or {}
            chunk["usage"] = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cache_tokens": prompt_details.get("cached_tokens", 0),
            }

        return chunk
