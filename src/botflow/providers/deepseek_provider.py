"""DeepSeek provider using an OpenAI-compatible client.

Provides ``DeepSeekProvider`` — a ``BaseProvider`` subclass that routes chat
completions through ``AsyncOpenAI`` pointed at DeepSeek's API (or any
OpenAI-compatible gateway that serves DeepSeek models).

DeepSeek's own documentation recommends the OpenAI SDK (the ``deepseek``
package on PyPI is a toy wrapper with a hardcoded endpoint and no async
support), so we use ``AsyncOpenAI`` with a configurable ``base_url``.

Features:
- Native ``reasoning_content`` handling (DeepSeek R1 / thinking mode)
- Tool-call forwarding (OpenAI-compatible format)
- Stream support with per-chunk reasoning extraction
- Robust fallback: if content is empty but reasoning_content exists,
  fall back to reasoning_content as content
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from openai import AsyncOpenAI

from botflow.common.exceptions import ProviderError
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider, _make_http_client

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

_DEFAULT_BASE_URL = "https://api.deepseek.com"


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
    """Provider for DeepSeek models via an OpenAI-compatible endpoint.

    Uses ``AsyncOpenAI`` (the SDK DeepSeek officially recommends) with a
    configurable ``base_url`` — either ``https://api.deepseek.com`` or any
    OpenAI-compatible gateway serving DeepSeek models.

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

        if not api_key:
            raise ValueError(
                "DeepSeekProvider requires a non-empty api_key. "
                "Set DEEPSEEK_API_KEY or pass api_key in provider config."
            )

        if not self.base_url:
            self.base_url = _DEFAULT_BASE_URL

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.extra_config.get("timeout", 120.0),
            http_client=_make_http_client(self.extra_config),
        )

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
        """Send a non-streaming chat completion.

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

            response = await self._client.chat.completions.create(**call_kwargs)
            return self._normalize_chat_response(response.model_dump(), model)

        except Exception as e:
            raise ProviderError(f"DeepSeek request failed: {e}") from e

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
        """Stream chat completion.

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

            stream = await self._client.chat.completions.create(**call_kwargs)

            async for chunk in stream:
                normalized = _normalize_stream_chunk(chunk.model_dump())
                if normalized is None:
                    continue

                yield self._normalize_stream_response(normalized, model)

        except Exception as e:
            raise ProviderError(f"DeepSeek stream failed: {e}") from e

    # ------------------------------------------------------------------
    # list_models() / health_check()
    # ------------------------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        """List available models from the configured endpoint."""
        try:
            response = await self._client.models.list()
            return [{"id": m.id, "object": "model"} for m in response.data]
        except Exception as e:
            logger.warning("Failed to list models from {}: {}", self.base_url, e)
            return []

    async def health_check(self) -> dict[str, Any]:
        """Health check — probe list_models as lightweight liveness test."""
        try:
            models = await self.list_models()
            return {"status": "ok", "models": len(models)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

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

    @staticmethod
    def _normalize_chat_response(data: dict[str, Any], model: str) -> dict[str, Any]:
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

    @staticmethod
    def _normalize_stream_response(data: dict[str, Any], model: str) -> dict[str, Any]:
        """Normalize stream chunk, extracting reasoning_content from delta.

        Unlike non-streaming, we do NOT apply the reasoning_content→content
        fallback here.  In streaming mode, reasoning chunks arrive separately
        from content chunks, and the caller needs ``reasoning_content`` to
        remain intact so it can distinguish reasoning from final content.
        """
        data.setdefault("id", "")
        data.setdefault("model", model)
        return data
