"""Internal tool registry with BM25-powered search.

Provides ToolRegistry for storing tool definitions and handlers,
and SimpleBM25 for ranking search results. Zero external dependencies.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


@dataclass
class ToolDef:
    """Internal tool definition."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[..., Awaitable[Any]]


class ToolRegistry:
    """Internal tool registry — stores tool definitions, provides search/describe/call."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._bm25 = SimpleBM25()

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Awaitable[Any]],
    ) -> None:
        """Register an internal tool."""
        # C2: handle duplicate registration — remove old BM25 entry first
        if name in self._tools:
            self._bm25.remove(name)
        td = ToolDef(name=name, description=description, parameters=parameters, handler=handler)
        self._tools[name] = td
        self._bm25.add(name, description)

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """Search tools by query using BM25 ranking.

        If query is "*", returns all tools without scoring.
        """
        # W1: consistent schema — wildcard results also include "score" (None)
        if query.strip() == "*":
            return [
                {"name": td.name, "description": td.description, "score": None}
                for td in self._tools.values()
            ]
        results = self._bm25.search(query, top_k=top_k)
        out: list[dict[str, Any]] = []
        for name, score in results:
            td = self._tools.get(name)
            if td is not None:
                out.append({
                    "name": td.name,
                    "description": td.description,
                    "score": round(score, 4),
                })
        return out

    def get(self, name: str) -> ToolDef | None:
        """Get a tool definition by name."""
        return self._tools.get(name)

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool by name with arguments."""
        td = self._tools.get(name)
        if td is None:
            available = ", ".join(sorted(self._tools.keys()))
            raise KeyError(
                f"Unknown tool '{name}'. Available tools: {available}"
            )
        return await td.handler(**arguments)

    def list_all(self) -> list[dict[str, Any]]:
        """List all registered tools."""
        return [
            {"name": td.name, "description": td.description}
            for td in self._tools.values()
        ]

    def names(self) -> list[str]:
        """Return all registered tool names (public API, avoids private attribute access)."""
        return list(self._tools.keys())


class SimpleBM25:
    """Lightweight BM25 implementation for small document sets (<1000).

    Zero external dependencies. Uses simple tokenization (split on
    non-alphanumeric/non-CJK characters).
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: list[dict[str, Any]] = []
        self._doc_lens: list[int] = []
        self._avgdl: float = 0.0
        self._df: Counter[str] = Counter()
        self._n: int = 0
        self._dirty: bool = False

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Lowercase + split on non-alphanumeric / non-CJK / underscore boundaries."""
        return [
            t
            for t in re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", text.lower())
            if t
        ]

    def add(self, doc_id: str, description: str) -> None:
        """Index a document (tool) for search."""
        tokens = self._tokenize(doc_id + " " + description)
        self._docs.append({"id": doc_id, "tokens": tokens})
        self._doc_lens.append(len(tokens))
        self._dirty = True

    def remove(self, doc_id: str) -> bool:
        """Remove a document by its ID. Returns True if found and removed."""
        for i, doc in enumerate(self._docs):
            if doc["id"] == doc_id:
                self._docs.pop(i)
                self._doc_lens.pop(i)
                self._dirty = True
                return True
        return False

    def _rebuild_index(self) -> None:
        self._n = len(self._docs)
        self._avgdl = sum(self._doc_lens) / self._n if self._n else 0.0
        self._df = Counter()
        for doc in self._docs:
            for term in set(doc["tokens"]):
                self._df[term] += 1
        self._dirty = False

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Return top_k (doc_id, score) pairs sorted by descending score."""
        if not self._docs:
            return []
        if self._dirty:
            self._rebuild_index()

        # C1: guard against zero division when index is empty after rebuild
        if self._n == 0 or self._avgdl == 0:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scores: list[tuple[str, float]] = []

        for doc in self._docs:
            score = 0.0
            tf_map = Counter(doc["tokens"])
            dl = len(doc["tokens"])

            for qt in query_tokens:
                if qt not in self._df:
                    continue
                df = self._df[qt]
                # W2: BM25+ IDF variant — adds +1 to prevent zero IDF for
                # universal terms in small collections (tool registry ~10-30 items).
                # Standard BM25 produces all-zero scores on small collections.
                idf = math.log((self._n - df + 0.5) / (df + 0.5) + 1)
                tf = tf_map.get(qt, 0)
                tf_norm = (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
                )
                score += idf * tf_norm

            if score > 0:
                scores.append((doc["id"], score))

        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]
