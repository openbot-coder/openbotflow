"""Context window management for LLM requests."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import tiktoken

# 上游 DeepSeek 未公开 tiktoken 词表；o200k_base 是实测误差最小的代理
# （真实 agent 载荷 −17.5%，旧启发式为 −51% ~ +34%）。可宣称"更准"不可宣称"精确"。
_ENCODING_NAME = "o200k_base"

# 每条消息的固定开销（role 与内容之间的分隔）。
_MESSAGE_OVERHEAD = 1


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


@lru_cache(maxsize=1)
def _encoding() -> "tiktoken.Encoding":
    """返回共享的 BPE 编码器，词表在首次调用时才加载。

    用 lru_cache 惰性单例（标准库），避免手写全局变量；首次调用会联网下载
    词表并缓存于 TIKTOKEN_CACHE_DIR，离线环境需预置。
    """
    return tiktoken.get_encoding(_ENCODING_NAME)


def _token_upper_bound(messages: list[dict[str, Any]]) -> int:
    """`estimate_tokens()` 的严格上界。

    每个 token 至少覆盖 1 字节 ⇒ BPE 结果 ≤ UTF-8 字节数；再加上每条消息的
    固定开销。**两条都要算**：只算字节数时，若 role 与 content 同时为空
    （客户端漏发 role），字节数为 0 而估算值仍为 1/条，上界就不成立了。

    用作快路径短路：上界 ≤ limit ⇒ 真实 token 数必不超限，可原样返回。
    """
    total = 0
    for msg in messages:
        role = msg.get("role", "")
        raw_content = msg.get("content", "") or ""
        text = _extract_text(raw_content)
        total += len(role.encode("utf-8")) + len(text.encode("utf-8")) + _MESSAGE_OVERHEAD
    return total


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """用真正的 BPE 分词器统计 token 数。

    用 encode_ordinary 而非 encode：后者遇到 <|endoftext|> 这类特殊 token
    字面量会抛 ValueError；encode_ordinary 把它们当普通文本处理，正文安全。
    """
    enc = _encoding()
    total = 0
    for msg in messages:
        role = msg.get("role", "")
        raw_content = msg.get("content", "") or ""
        text = _extract_text(raw_content)
        total += (
            len(enc.encode_ordinary(role))
            + len(enc.encode_ordinary(text))
            + _MESSAGE_OVERHEAD
        )
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

    # 上界短路：token 数 ≤ 字节数 + 每条固定开销，上界达标即必不超限。
    # 边界：上界 == limit 走短路，上界 == limit + 1 才进编码路径。
    if _token_upper_bound(messages) <= limit:
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
