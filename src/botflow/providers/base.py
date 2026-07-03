"""Abstract base class for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator


class BaseProvider(ABC):
    """Abstract base class for all LLM providers.

    Each provider subclass implements the unified chat interface.
    All methods are async to support concurrent requests.
    """

    def __init__(self, api_key: str, base_url: str = "", extra_config: dict[str, Any] | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.extra_config = extra_config or {}

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request.

        Returns a unified response dict:
        {
            "id": "chatcmpl-xxx",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello"},
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 5}
            },
            "provider": "openai"
        }
        """

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion response.

        Yields SSE-like chunk dicts:
        {
            "id": "chatcmpl-xxx",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "delta": {"role": "assistant", "content": "Hello"},
                    "index": 0,
                    "finish_reason": None
                }
            ],
            "usage": None  # or usage dict on final chunk
        }
        """

    @abstractmethod
    async def list_models(self) -> list[dict[str, Any]]:
        """List available models from this provider.

        Returns:
            List of model dicts with at minimum {"id": "...", "object": "model"}.
        """

    def _check_response(self, resp: Any, provider_name: str) -> None:
        """Validate HTTP response and raise on errors."""
        from botflow.common.exceptions import ProviderError

        if not 200 <= resp.status_code < 300:
            body = resp.text[:500]
            raise ProviderError(
                f"{provider_name} returned HTTP {resp.status_code}: {body}"
            )
