"""Google Gemini provider."""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import httpx

from botflow.common.exceptions import ProviderError
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider

logger = get_logger("providers.google")


class GoogleProvider(BaseProvider):
    """Provider for Google Gemini API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
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
                    "X-Goog-Api-Key": self.api_key,
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
        contents, system = self._convert_messages(messages)
        body: dict[str, Any] = {
            "contents": contents,
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        generation_config: dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if generation_config:
            body["generationConfig"] = generation_config
        body.update(kwargs)

        url = f"/models/{model}:generateContent"

        try:
            resp = await self.client.post(url, json=body)
            self._check_response(resp, "Google")
            data = resp.json()
            return self._to_unified(data, model)
        except httpx.TimeoutException:
            raise ProviderError(f"Google Gemini timed out for model {model}")
        except httpx.RequestError as e:
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
        body: dict[str, Any] = {
            "contents": contents,
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        generation_config: dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if generation_config:
            body["generationConfig"] = generation_config
        body.update(kwargs)

        url = f"/models/{model}:streamGenerateContent?alt=sse"

        try:
            async with self.client.stream("POST", url, json=body) as resp:
                self._check_response(resp, "Google")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        chunk = json.loads(line[6:])
                        yield self._chunk_to_unified(chunk, model)
        except httpx.TimeoutException:
            raise ProviderError(f"Google Gemini stream timed out for model {model}")
        except httpx.RequestError as e:
            raise ProviderError(f"Google Gemini stream failed: {e}")

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            resp = await self.client.get("/models")
            self._check_response(resp, "Google")
            data = resp.json()
            return data.get("models", [])
        except httpx.RequestError as e:
            logger.warning("Failed to list models from Google: {}", e)
            return []

    def _convert_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict], str]:
        """Convert OpenAI-style messages to Gemini format."""
        contents: list[dict] = []
        system = ""
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                system = content
                continue
            gemini_role = "model" if role in ("assistant", "model") else "user"
            contents.append({"role": gemini_role, "parts": [{"text": content}]})
        if not contents:
            contents.append({"role": "user", "parts": [{"text": ""}]})
        return contents, system

    def _to_unified(self, data: dict, model: str) -> dict[str, Any]:
        """Convert Google Gemini response to unified format."""
        candidates = data.get("candidates", []) or []
        candidate = candidates[0] if candidates else {}
        content_parts = candidate.get("content", {}).get("parts", []) or []
        text = "".join(p.get("text", "") for p in content_parts)
        finish_reason = candidate.get("finishReason", "STOP")
        usage = data.get("usageMetadata", {}) or {}

        return {
            "id": data.get("id", ""),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": text,
                    },
                    "finish_reason": finish_reason.lower() if finish_reason else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
                "cache_tokens": usage.get("cachedContentTokenCount", 0),
            },
            "provider": "google",
        }

    def _chunk_to_unified(self, data: dict, model: str) -> dict[str, Any]:
        """Convert Google Gemini stream chunk to unified format."""
        candidates = data.get("candidates", []) or []
        candidate = candidates[0] if candidates else {}
        content_parts = candidate.get("content", {}).get("parts", []) or []
        delta_content = "".join(p.get("text", "") for p in content_parts)
        finish_reason = candidate.get("finishReason")

        chunk: dict[str, Any] = {
            "id": data.get("id", ""),
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": delta_content},
                    "finish_reason": finish_reason.lower() if finish_reason else None,
                }
            ],
            "usage": None,
            "provider": "google",
        }

        usage = data.get("usageMetadata")
        if usage and finish_reason:
            chunk["usage"] = {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
                "cache_tokens": usage.get("cachedContentTokenCount", 0),
            }

        return chunk
