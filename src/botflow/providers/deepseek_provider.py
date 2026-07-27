"""DeepSeek provider using the official deepseek Python SDK.

Provides ``DeepSeekProvider`` — a ``BaseProvider`` subclass that routes
chat completions through ``deepseek.DeepSeekClient``.

Features:
- Native ``reasoning_content`` handling (DeepSeek R1 / thinking mode)
- Tool-call forwarding (OpenAI-compatible format)
- Stream support with per-chunk reasoning extraction
- Robust fallback: if content is empty but reasoning_content exists,
  fall back to reasoning_content as content

Optional dependency: ``pip install deepseek``
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from botflow.common.exceptions import ProviderError
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider

logger = get_logger("providers.deepseek")

# ---------------------------------------------------------------------------
# Allowed kwargs passed to the SDK (whitelist)
# ---------------------------------------------------------------------------
_ALLOWED_CHAT_KWARGS = frozenset({
    "frequency_penalty",
    "logit_bias",
    "max_tokens",
    "n",
    "presence_penalty",
    "stop",
    "temperature",
    "top_logprobs",
    "top_p",
})


def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure response dict has the required structure."""
    if "choices" not in data:
        data["choices"] = []
    return data


def _normalize_stream_chunk(data: dict[str, Any]) -> dict[str, Any] | None:
    """Ensure stream chunk has choices, or return None to skip."""
    if not data.get("choices"):
        return None
    return data


class DeepSeekProvider(BaseProvider):
    """Provider for DeepSeek API using the official deepseek SDK.

    The SDK (``deepseek>=1.0.0``) wraps the OpenAI client with DeepSeek-specific
    defaults and error types.

    Features:
    - Native ``reasoning_content`` for R1 / thinking mode
    - Tool-call forwarding
    - Stream support
    - Fallback: if content is empty but reasoning_content exists,
      use reasoning_content as content
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(api_key, base_url, extra_config)

        try:
            from deepseek import DeepSeekClient
        except ImportError:
            raise ImportError(
                "deepseek package is required for DeepSeekProvider. "
                "Install it with: pip install deepseek"
            )

        # Initialize the official SDK client
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        self._client: Any = DeepSeekClient(**client_kwargs)

    # ------------------------------------------------------------------
    # chat()
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a non-streaming chat completion via the official SDK.

        Returns a unified response dict with ``reasoning_content`` if present.
        """
        try:
            call_kwargs = self._build_chat_kwargs(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                **kwargs,
            )

            response = await self._client.async_chat_completion(**call_kwargs)

            # Normalize: SDK may return Pydantic model or dict
            if hasattr(response, "model_dump"):
                data = response.model_dump()
            elif isinstance(response, dict):
                data = response
            else:
                data = response  # hope for the best

            return self._normalize_chat_response(data, model)

        except Exception as e:
            raise ProviderError(f"DeepSeekSDK request failed: {e}") from e

    # ------------------------------------------------------------------
    # chat_stream()
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream chat completion via the official SDK.

        Yields unified chunk dicts with ``reasoning_content`` when present.
        """
        try:
            call_kwargs = self._build_chat_kwargs(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            )

            stream_gen = self._client.async_stream_response(**call_kwargs)

            async for chunk in stream_gen:
                # Normalize: SDK may yield Pydantic model or dict
                if hasattr(chunk, "model_dump"):
                    data = chunk.model_dump()
                elif isinstance(chunk, dict):
                    data = chunk
                else:
                    continue

                normalized = _normalize_stream_chunk(data)
                if normalized is None:
                    continue

                yield self._normalize_stream_response(normalized, model)

        except Exception as e:
            raise ProviderError(f"DeepSeekSDK stream failed: {e}") from e

    # ------------------------------------------------------------------
    # list_models()
    # ------------------------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        """List models — DeepSeek SDK does not support this, return empty."""
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_chat_kwargs(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build keyword arguments for the SDK call."""
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if temperature is not None:
            call_kwargs["temperature"] = temperature
        if max_tokens is not None:
            call_kwargs["max_tokens"] = max_tokens

        # Forward only whitelisted kwargs
        for k, v in kwargs.items():
            if k in _ALLOWED_CHAT_KWARGS:
                call_kwargs[k] = v
            elif k in ("stream_options", "tools", "tool_choice", "response_format"):
                call_kwargs[k] = v

        return call_kwargs

    def _normalize_chat_response(self, data: dict[str, Any], model: str) -> dict[str, Any]:
        """Normalize chat response, extracting reasoning_content."""
        data = _normalize_response(data)
        data.setdefault("id", "")
        data.setdefault("model", model)

        if data["choices"]:
            choice = data["choices"][0]
            message = choice.get("message", {})

            content = message.get("content") or ""
            reasoning_content = message.get("reasoning_content")

            # Fallback: use reasoning_content as content when content is empty
            if not content and reasoning_content:
                message["content"] = reasoning_content
                message["reasoning_content"] = None
            elif reasoning_content:
                message["reasoning_content"] = reasoning_content

        return data

    def _normalize_stream_response(self, data: dict[str, Any], model: str) -> dict[str, Any]:
        """Normalize stream chunk, extracting reasoning_content from delta.

        Unlike non-streaming, we do NOT apply the reasoning_content→content
        fallback here.  In streaming mode, reasoning chunks arrive separately
        from content chunks, and the caller needs ``reasoning_content`` to
        remain intact so it can distinguish reasoning from final content.
        """
        data.setdefault("id", "")
        data.setdefault("model", model)
        return data
