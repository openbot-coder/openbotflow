"""OpenAI-compatible provider (OpenAI, Azure, vLLM, Ollama).

Uses the official openai SDK which supports:
- Direct OpenAI API
- Azure OpenAI
- Any OpenAI-compatible endpoint (vLLM, Ollama, etc.)

``extra_config["headers"]`` adds custom request headers.  Some upstreams require
this — e.g. OpenCode Go rejects requests with a generic SDK user agent and needs
a stable per-conversation ``x-opencode-session``; use the ``{conversation_id}``
placeholder to derive one from the conversation itself.
"""

from __future__ import annotations

import hashlib

from typing import Any, AsyncGenerator

from openai import AsyncOpenAI, AsyncAzureOpenAI
from openai.types.chat import ChatCompletionChunk

from botflow.common.exceptions import ProviderError
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider, _make_http_client

logger = get_logger("providers.openai")


def _extract_text_from_content(content: Any) -> str:
    """Extract plain text from content, handling both str and list formats.

    LLM providers (DeepSeek, OpenAI, etc.) may return multimodal content as::

        [{"type": "text", "text": "..."}, {"type": "image_url", ...}]

    Downstream consumers (protocol_adapter, history) expect ``content`` to be
    a plain string.  This helper extracts only text blocks and joins them.

    Also used in ``common.context.estimate_tokens()`` for token estimation.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts)
    return str(content) if content else ""


def _conversation_id(messages: list[dict[str, Any]]) -> str:
    """Stable digest of a conversation, for upstreams that key routing on it.

    Seeds on the system prompt plus the *first* user message.  OpenAI-style
    histories are append-only, so those two never move — every turn of the same
    conversation hashes the same, while different conversations differ.
    """
    system = next((m for m in messages if m.get("role") == "system"), None)
    first_user = next((m for m in messages if m.get("role") == "user"), None)
    seed = "{}\n{}".format(
        _extract_text_from_content(system.get("content")) if system else "",
        _extract_text_from_content(first_user.get("content")) if first_user else "",
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _apply_headers(
    extra_config: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, str] | None:
    """Resolve ``extra_config["headers"]`` into request headers.

    Values pass through as-is, except the ``{conversation_id}`` placeholder,
    which is replaced with :func:`_conversation_id`.  Returns ``None`` when
    nothing is configured, so the SDK keeps its own defaults.
    """
    raw = extra_config.get("headers")
    if not isinstance(raw, dict) or not raw:
        return None
    headers = {str(k): str(v) for k, v in raw.items()}
    if not any("{conversation_id}" in v for v in headers.values()):
        return headers
    cid = _conversation_id(messages)
    return {k: v.replace("{conversation_id}", cid) for k, v in headers.items()}


class OpenAICompatProvider(BaseProvider):
    """Provider for OpenAI-compatible APIs.

    Uses the official openai SDK for proper connection pooling, retry logic,
    and protocol compliance.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(api_key, base_url, extra_config)
        self._client: AsyncOpenAI | None = None
        self._azure_client: AsyncAzureOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI | AsyncAzureOpenAI:
        """Get or create the OpenAI client (lazy initialization)."""
        if "azure" in self.extra_config.get("mode", ""):
            if self._azure_client is None:
                self._azure_client = AsyncAzureOpenAI(
                    api_key=self.api_key,
                    azure_endpoint=self.base_url,
                    api_version=self.extra_config.get("api_version", "2024-02-01"),
                    timeout=self.extra_config.get("timeout", 120.0),
                    http_client=_make_http_client(self.extra_config),
                )
            return self._azure_client
        
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.api_key or "dummy",
                base_url=self.base_url,
                timeout=self.extra_config.get("timeout", 120.0),
                http_client=_make_http_client(self.extra_config),
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
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                extra_headers=_apply_headers(self.extra_config, messages),
                **kwargs,
            )
            return self._to_unified(response.model_dump(), model)
        except Exception as e:
            raise ProviderError(f"OpenAICompat request failed: {e}")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        try:
            stream_options = kwargs.pop("stream_options", {"include_usage": True})
            if isinstance(stream_options, dict):
                stream_options.setdefault("include_usage", True)
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options=stream_options,
                extra_headers=_apply_headers(self.extra_config, messages),
                **kwargs,
            )
            async for chunk in stream:
                yield self._chunk_to_unified(chunk.model_dump(), model)
        except Exception as e:
            raise ProviderError(f"OpenAICompat stream failed: {e}")

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            response = await self.client.models.list()
            return [m.model_dump() for m in response.data]
        except Exception as e:
            logger.warning("Failed to list models from {}: {}", self.base_url, e)
            return []

    def _to_unified(self, data: dict, model: str) -> dict[str, Any]:
        """Convert OpenAI response to unified format."""
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        usage = data.get("usage", {}) or {}
        prompt_details = usage.get("prompt_tokens_details", {}) or {}

        content = _extract_text_from_content(msg.get("content") or "")
        reasoning_content = msg.get("reasoning_content")

        message: dict[str, Any] = {
            "role": msg.get("role", "assistant"),
            "content": content,
        }
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        if msg.get("tool_calls"):
            message["tool_calls"] = msg["tool_calls"]
        if msg.get("function_call"):
            message["function_call"] = msg["function_call"]

        return {
            "id": data.get("id", ""),
            "model": model,
            "choices": [
                {
                    "index": choice.get("index", 0),
                    "message": message,
                    "finish_reason": choice.get("finish_reason", "stop"),
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cache_tokens": prompt_details.get("cached_tokens", 0),
            },
            "provider": "openai",
        }

    def _chunk_to_unified(self, data: dict, model: str) -> dict[str, Any]:
        """Convert OpenAI stream chunk to unified format."""
        raw_choices = data.get("choices") or []
        choice = raw_choices[0] if raw_choices and raw_choices[0] is not None else {}
        delta = choice.get("delta") or {}

        delta_content = _extract_text_from_content(delta.get("content"))
        delta_reasoning = delta.get("reasoning_content")

        delta_out: dict[str, Any] = {
            "role": delta.get("role") or "assistant",
            "content": delta_content,
        }
        if delta_reasoning:
            delta_out["reasoning_content"] = delta_reasoning
        if delta.get("tool_calls"):
            delta_out["tool_calls"] = delta["tool_calls"]
        if delta.get("function_call"):
            delta_out["function_call"] = delta["function_call"]

        chunk: dict[str, Any] = {
            "id": data.get("id", ""),
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": choice.get("index", 0),
                    "delta": delta_out,
                    "finish_reason": choice.get("finish_reason"),
                }
            ],
            "usage": None,
            "provider": "openai",
        }

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
