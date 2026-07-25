"""Google Gemini provider.

Uses the official google-genai SDK for proper streaming,
tool use, and multimodal support.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from google import genai
from google.genai.types import (
    Blob,
    Content,
    GenerateContentConfig,
    GenerateContentResponse,
    Part,
)

from botflow.common.exceptions import ProviderError
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider

logger = get_logger("providers.google")


class GoogleProvider(BaseProvider):
    """Provider for Google Gemini API.

    Uses the official google-genai SDK for proper connection pooling,
    retry logic, and protocol compliance.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "",  # Not used with official SDK
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(api_key, base_url, extra_config)
        self._client: genai.Client | None = None

    @property
    def client(self) -> genai.Client:
        """Get or create the Google GenAI client (lazy initialization)."""
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
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
        contents, system = self._convert_messages(messages)
        config = self._build_config(temperature, max_tokens, system=system, **kwargs)

        try:
            response = await self.client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return self._to_unified(response, model)
        except Exception as e:
            raise ProviderError(f"Google Gemini request failed: {e}")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        contents, system = self._convert_messages(messages)
        config = self._build_config(temperature, max_tokens, system=system, **kwargs)

        try:
            async for chunk in await self.client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            ):
                yield self._chunk_to_unified(chunk, model)
        except Exception as e:
            raise ProviderError(f"Google Gemini stream failed: {e}")

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            models = []
            async for m in self.client.aio.models.list():
                models.append({"id": m.name, "object": "model"})
            return models
        except Exception as e:
            logger.warning("Failed to list models from Google: {}", e)
            return []

    def _build_config(
        self,
        temperature: float | None,
        max_tokens: int | None,
        system: str = "",
        **kwargs: Any,
    ) -> GenerateContentConfig:
        """Build GenerateContentConfig from parameters."""
        config_params: dict[str, Any] = {}
        if system:
            config_params["system_instruction"] = system
        if temperature is not None:
            config_params["temperature"] = temperature
        if max_tokens is not None:
            config_params["max_output_tokens"] = max_tokens
        return GenerateContentConfig(**config_params)

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> tuple[list[Content], str]:
        """Convert OpenAI-style messages to Gemini format.

        Handles both string and list-type content (multimodal).
        List content with text/image_url blocks is converted to Gemini Part objects.
        """
        contents: list[Content] = []
        system = ""
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                system = _extract_system_text(content)
                continue
            gemini_role = "model" if role in ("assistant", "model") else "user"

            if isinstance(content, str):
                contents.append(Content(role=gemini_role, parts=[Part(text=content)]))
            elif isinstance(content, list):
                parts = GoogleProvider._convert_content_parts(content)
                if parts:
                    contents.append(Content(role=gemini_role, parts=parts))
            else:
                contents.append(Content(role=gemini_role, parts=[Part(text=str(content))]))

        if not contents:
            contents.append(Content(role="user", parts=[Part(text="")]))
        return contents, system

    @staticmethod
    def _convert_content_parts(content_blocks: list[dict[str, Any]]) -> list[Part]:
        """Convert OpenAI-format content blocks to Gemini Part objects.

        Handles:
        - {"type": "text", "text": "..."} → Part(text=...)
        - {"type": "image_url", "image_url": {"url": "data:mime;base64,..."}} → Part(inline_data=Blob(...))
        """
        parts: list[Part] = []
        for block in content_blocks:
            block_type = block.get("type", "")
            if block_type == "text":
                parts.append(Part(text=block.get("text", "")))
            elif block_type == "image_url":
                image_url = block.get("image_url", {})
                url = image_url.get("url", "")
                if url.startswith("data:"):
                    mime_type, b64_data = _parse_data_url(url)
                    if mime_type and b64_data:
                        parts.append(Part(inline_data=Blob(mime_type=mime_type, data=b64_data)))
                else:
                    parts.append(Part(file_data={"file_uri": url}))
        return parts

    def _to_unified(self, response: GenerateContentResponse, model: str) -> dict[str, Any]:
        """Convert Google Gemini response to unified format."""
        text = response.text or ""
        usage = response.usage_metadata

        return {
            "id": "",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(usage, "candidates_token_count", 0) or 0,
                "total_tokens": getattr(usage, "total_token_count", 0) or 0,
                "cache_tokens": getattr(usage, "cached_content_token_count", 0) or 0,
            },
            "provider": "google",
        }

    def _chunk_to_unified(self, chunk: Any, model: str) -> dict[str, Any]:
        """Convert Google Gemini stream chunk to unified format."""
        text = chunk.text or ""
        usage = chunk.usage_metadata

        result: dict[str, Any] = {
            "id": "",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None,
                }
            ],
            "usage": None,
            "provider": "google",
        }

        # Include usage on final chunk
        if usage and getattr(usage, "total_token_count", None):
            result["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(usage, "candidates_token_count", 0) or 0,
                "total_tokens": getattr(usage, "total_token_count", 0) or 0,
                "cache_tokens": getattr(usage, "cached_content_token_count", 0) or 0,
            }

        return result


def _extract_system_text(content: Any) -> str:
    """Extract plain text from system content, handling both str and list formats.

    Gemini system_instruction only accepts str, so list content must be reduced.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts) if parts else ""
    return str(content) if content else ""


def _parse_data_url(url: str) -> tuple[str, str] | None:
    """Parse a data URI into (mime_type, base64_data).

    Example: "data:image/png;base64,iVBOR..." → ("image/png", "iVBOR...")
    Returns None if not a valid data URI.
    """
    if not url.startswith("data:"):
        return None
    header, _, b64 = url.partition(",")
    # header = "data:image/png;base64"
    mime_part = header.split(":", 1)[1] if ":" in header else "text/plain"
    mime_type = mime_part.split(";")[0]
    return mime_type, b64
