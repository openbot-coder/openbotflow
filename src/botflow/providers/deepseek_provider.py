"""DeepSeek provider — handles reasoning_content and list content blocks."""

from __future__ import annotations

import copy
from typing import Any, AsyncGenerator

from loguru import logger

from botflow.providers.openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    """OpenAI-compatible provider with DeepSeek-specific quirks.

    DeepSeek r1 thinking mode quirks:
    1. Returns ``reasoning_content`` (non-standard field) — preserved in messages
    2. Returns ``content`` as list of blocks ``[{"type":"text","text":"..."}]``
       instead of a plain string
    3. Session state pollution — must deepcopy messages before mutation
    """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text_content(content: Any) -> str:
        """Extract plain text from list content blocks."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(content) if content else ""

    # ------------------------------------------------------------------
    # Non-streaming chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Deepseek: deepcopy messages to avoid session state pollution
        messages = copy.deepcopy(messages)

        # Build request body — reuse parent but ensure DeepSeek-specific params
        body = self._build_body(messages, model, temperature, max_tokens, **kwargs)
        # DeepSeek may need stream_options to get usage in streaming mode
        body.setdefault("stream", False)

        response = await self._send_request(body)

        if "error" in response:
            raise Exception(f"DeepSeek API error: {response['error']}")

        # --- Reasoning content handling ---
        choice = response.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content", "")
        reasoning_content = msg.get("reasoning_content")

        # Handle list content blocks (DeepSeek r1 returns this format)
        if isinstance(content, list):
            content = self._extract_text_content(content)
            msg["content"] = content

        # Fallback: if content empty but reasoning_content exists, use it
        if not content and reasoning_content:
            content = reasoning_content
            reasoning_content = None
            msg["content"] = content
            msg["reasoning_content"] = None

        # Forward reasoning_content so protocol_adapter can emit it
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content

        # Log with thinking info
        has_thinking = bool(reasoning_content)
        logger.debug(
            "DeepSeek response: content_len={}, thinking_len={}, model={}",
            len(content) if content else 0,
            len(reasoning_content) if reasoning_content else 0,
            model,
        )

        # Attach usage info if present
        usage = response.get("usage")
        if usage:
            logger.debug(
                "DeepSeek usage: prompt_tokens={}, completion_tokens={}, total_tokens={}",
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )

        return response

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        # Deepseek: deepcopy messages to avoid session state pollution
        messages = copy.deepcopy(messages)

        body = self._build_body(messages, model, temperature, max_tokens, **kwargs)
        body["stream"] = True
        # Request usage in stream via stream_options
        body.setdefault("stream_options", {"include_usage": True})

        full_content = ""
        full_reasoning = ""

        async for chunk_data in self._stream_request(body):
            if "error" in chunk_data:
                raise Exception(f"DeepSeek stream error: {chunk_data['error']}")

            # Handle chunk format
            choices = chunk_data.get("choices", [])
            if not choices:
                # Usage-only chunk (stream_options)
                usage = chunk_data.get("usage")
                if usage:
                    logger.debug(
                        "DeepSeek stream usage: prompt_tokens={}, completion_tokens={}",
                        usage.get("prompt_tokens", 0),
                        usage.get("completion_tokens", 0),
                    )
                continue

            choice = choices[0]
            delta = choice.get("delta", {})

            # --- Reasoning content from delta ---
            delta_reasoning = delta.get("reasoning_content")
            content = delta.get("content", "")

            # Handle list content blocks in delta
            if isinstance(content, list):
                content = self._extract_text_content(content)

            # Fallback: if content empty but reasoning_content exists
            if not content and delta_reasoning:
                content = delta_reasoning
                delta_reasoning = None
                delta["content"] = content
                delta["reasoning_content"] = None

            if delta_reasoning:
                full_reasoning += delta_reasoning
                delta["reasoning_content"] = delta_reasoning

            if content:
                full_content += content

            yield chunk_data

        logger.debug(
            "DeepSeek stream complete: content_len={}, thinking_len={}, model={}",
            len(full_content),
            len(full_reasoning),
            model,
        )
