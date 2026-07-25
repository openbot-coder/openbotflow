"""Cross-provider multimodal content format converters.

Converts between OpenAI and Anthropic content block formats:
- OpenAI: [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:..."}}]
- Anthropic: [{"type": "text", "text": "..."}, {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}]

Also handles Google Gemini Part format conversion.
"""

from __future__ import annotations

import base64
from typing import Any


# ---------------------------------------------------------------------------
# OpenAI → Anthropic
# ---------------------------------------------------------------------------


def openai_to_anthropic_content(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI content format to Anthropic content blocks.

    OpenAI format:
        str → [{"type": "text", "text": str}]
        list → each block converted individually

    Anthropic format:
        [{"type": "text", "text": str}, {"type": "image", "source": {...}}, ...]
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    blocks: list[dict[str, Any]] = []
    for block in content:
        block_type = block.get("type", "")

        if block_type == "text":
            blocks.append({"type": "text", "text": block.get("text", "")})

        elif block_type == "image_url":
            image_url = block.get("image_url", {})
            url = image_url.get("url", "")
            source = _parse_data_uri_to_anthropic_source(url)
            if source:
                blocks.append({"type": "image", "source": source})

        elif block_type == "image":
            # Already Anthropic-like format, pass through
            blocks.append(block)

        elif block_type == "tool_result":
            blocks.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content": block.get("content", ""),
            })

        elif block_type == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })

        else:
            # Unknown type: skip silently
            continue

    return blocks


# ---------------------------------------------------------------------------
# Anthropic → OpenAI
# ---------------------------------------------------------------------------


def anthropic_to_openai_content(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Convert Anthropic content blocks to OpenAI content format.

    Anthropic format:
        [{"type": "text", "text": "..."}, {"type": "image", "source": {...}}, ...]

    OpenAI format:
        [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:..."}}, ...]

    If only text blocks exist, returns a plain string for efficiency.
    """
    if isinstance(content, str):
        return content

    blocks: list[dict[str, Any]] = []
    has_non_text = False

    for block in content:
        block_type = block.get("type", "")

        if block_type == "text":
            blocks.append({"type": "text", "text": block.get("text", "")})

        elif block_type == "image":
            source = block.get("source", {})
            data_uri = _anthropic_source_to_data_uri(source)
            if data_uri:
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                })
                has_non_text = True

        elif block_type == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
            has_non_text = True

        elif block_type == "tool_result":
            blocks.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content": block.get("content", ""),
            })
            has_non_text = True

    if not blocks:
        return ""

    # If only text blocks, return plain string
    if not has_non_text and all(b["type"] == "text" for b in blocks):
        return "\n".join(b["text"] for b in blocks)

    return blocks


# ---------------------------------------------------------------------------
# OpenAI → Google Gemini
# ---------------------------------------------------------------------------


def openai_to_google_parts(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI content to Google Gemini Part dicts.

    Google Gemini Part format:
        [{"text": "..."}, {"inline_data": {"mime_type": "...", "data": "..."}}]
    """
    if isinstance(content, str):
        return [{"text": content}]

    parts: list[dict[str, Any]] = []
    for block in content:
        block_type = block.get("type", "")

        if block_type == "text":
            parts.append({"text": block.get("text", "")})

        elif block_type == "image_url":
            image_url = block.get("image_url", {})
            url = image_url.get("url", "")
            parsed = _parse_data_uri(url)
            if parsed:
                mime_type, b64_data = parsed
                parts.append({
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": b64_data,
                    }
                })
            else:
                # HTTP/HTTPS URL → file_data
                parts.append({"file_data": {"file_uri": url}})

    return parts if parts else [{"text": ""}]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_data_uri(url: str) -> tuple[str, str] | None:
    """Parse a data URI into (mime_type, base64_data).

    Returns None if not a data URI.
    """
    if not url.startswith("data:"):
        return None

    # "data:image/png;base64,iVBOR..."
    header, _, b64 = url.partition(",")
    # header = "data:image/png;base64"
    mime_part = header.split(":", 1)[1] if ":" in header else "text/plain"
    mime_type = mime_part.split(";")[0]

    return mime_type, b64


def _parse_data_uri_to_anthropic_source(url: str) -> dict[str, Any] | None:
    """Convert a data URI to Anthropic image source dict.

    Returns {"type": "base64", "media_type": "...", "data": "..."} or None.
    """
    parsed = _parse_data_uri(url)
    if not parsed:
        return None

    mime_type, b64_data = parsed
    # Only handle image MIME types
    if not mime_type.startswith("image/"):
        return None

    return {
        "type": "base64",
        "media_type": mime_type,
        "data": b64_data,
    }


def _anthropic_source_to_data_uri(source: dict[str, Any]) -> str | None:
    """Convert an Anthropic image source to a data URI string.

    source = {"type": "base64", "media_type": "image/png", "data": "iVBOR..."}
    Returns "data:image/png;base64,iVBOR..." or None.
    """
    if source.get("type") != "base64":
        return None

    media_type = source.get("media_type", "image/png")
    data = source.get("data", "")
    if not data:
        return None

    return f"data:{media_type};base64,{data}"
