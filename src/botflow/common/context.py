"""Context window management for LLM requests."""

from __future__ import annotations

from typing import Any


def _extract_text(content: Any) -> str:
    """Extract plain text from content, handling both str and list formats.

    Multimodal content is a list of dicts like:
        [{"type": "text", "text": "..."}, {"type": "image_url", ...}]
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts)
    return str(content) if content else ""


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate token count for a list of messages.

    Uses a simple heuristic: ~4 characters per token for English text,
    ~2 characters per token for CJK text. This is intentionally rough;
    it's meant to prevent context-length explosions, not replace a real
    tokenizer.
    """
    total = 0
    for msg in messages:
        raw_content = msg.get("content", "") or ""
        text = _extract_text(raw_content)
        role = msg.get("role", "")
        # ~4 chars per token for English, ~2 for CJK
        char_count = len(role) + len(text)
        cjk_ratio = _cjk_ratio(text)
        chars_per_token = 4.0 - (cjk_ratio * 2.0)
        total += int(char_count / max(chars_per_token, 1.0)) + 1
    return total


def truncate_to_context_window(
    messages: list[dict[str, Any]],
    context_window: int,
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """Truncate messages to fit within the model's context window.

    Keeps the system message (if any) and the most recent messages.

    Args:
        messages: Full message list.
        context_window: Model's max context length in tokens.
        max_tokens: Reserved tokens for the completion.

    Returns:
        Truncated message list.
    """
    if context_window <= 0:
        return messages

    reserve = max_tokens or 1024
    limit = max(context_window - reserve, 1)

    # Fast path
    estimated = estimate_tokens(messages)
    if estimated <= limit:
        return messages

    # Keep system + last N messages
    system = [m for m in messages if m.get("role") == "system"]
    history = [m for m in messages if m.get("role") != "system"]

    # Binary search for how many recent messages we can keep
    low, high = 0, len(history)
    best = 0
    while low <= high:
        mid = (low + high) // 2
        candidate = system + history[len(history) - mid:]
        if estimate_tokens(candidate) <= limit:
            best = mid
            low = mid + 1
        else:
            high = mid - 1

    if best == 0 and system:
        return system
    if best == 0:
        return messages[-1:] if messages else messages

    return system + history[len(history) - best:]


def _cjk_ratio(text: str) -> float:
    """Return ratio of CJK characters in text."""
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\u3040" <= ch <= "\u309f" or "\u30a0" <= ch <= "\u30ff")
    return cjk / len(text)
