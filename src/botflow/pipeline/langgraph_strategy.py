"""LangGraph 策略 — 基于有向图的多步 LLM 工作流。

group.params 格式::

    {
        "nodes": {
            "analyze": {"prompt": "分析以下内容: {messages}", "group_id": null},
            "respond": {"prompt": "基于分析结果回复: {state}", "group_id": null}
        },
        "edges": [["analyze", "respond"]],
        "entry": "analyze",
        "final": "respond"
    }

- ``group_id: null`` 表示使用当前 group 的 endpoints；也可指定其他 group_id。
- ``edges`` 支持条件边：``["from", "to", "condition"]``，condition 为 LLM 输出中的子串匹配。
- ``final`` 指定哪个节点的输出作为最终结果（默认最后一个无出边的节点）。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from botflow.common.exceptions import (
    ConfigurationError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.common.logger import get_logger
from botflow.pipeline.base import BaseStrategy, StrategyError, register_strategy

log = get_logger("pipeline.langgraph")


class LangGraphStrategy(BaseStrategy):
    """基于有向图的多步 LLM 工作流策略。"""

    async def select_endpoints(self, *args, **kwargs):  # noqa: ARG002
        raise StrategyError("LangGraphStrategy overrides execute(); select_endpoints is not used")

    async def execute(
        self,
        messages: list[dict],
        db,  # Database
        cooldown,  # CooldownManager
        group_id: int,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> dict:
        from botflow.pipeline._shared import (
            call_llm,
            filter_available,
            load_endpoints,
            truncate_messages,
        )

        params = self.params or {}
        nodes_cfg: dict[str, dict] = params.get("nodes", {})
        edges_cfg: list = params.get("edges", [])
        entry: str = params.get("entry", "")
        final_node: str | None = params.get("final")

        # --- 校验 ---
        if not nodes_cfg:
            raise ConfigurationError("LangGraph strategy requires 'nodes' in params")
        if not entry or entry not in nodes_cfg:
            raise ConfigurationError(
                f"Invalid entry node '{entry}'. Available: {list(nodes_cfg)}"
            )

        # --- 构建邻接表（含条件边）---
        # adjacency[node] = [(target, condition_str | None), ...]
        adjacency: dict[str, list[tuple[str, str | None]]] = {n: [] for n in nodes_cfg}
        for edge in edges_cfg:
            if not isinstance(edge, (list, tuple)) or len(edge) < 2:
                raise ConfigurationError(f"Invalid edge format: {edge}")
            src, dst = edge[0], edge[1]
            condition = edge[2] if len(edge) > 2 else None
            if src not in nodes_cfg:
                raise ConfigurationError(f"Edge source '{src}' not in nodes")
            if dst not in nodes_cfg and dst not in ("__end__", "END"):
                raise ConfigurationError(f"Edge target '{dst}' not in nodes")
            adjacency.setdefault(src, []).append((dst, condition))

        # --- 预加载各 group 的 endpoints 缓存 ---
        endpoint_cache: dict[int, list] = {}

        async def _get_endpoints(gid: int):
            if gid not in endpoint_cache:
                eps = await load_endpoints(gid, db)
                if not eps:
                    raise NoAvailableModelError(f"Group {gid} has no enabled models")
                endpoint_cache[gid] = eps
            return endpoint_cache[gid]

        # --- 执行图 ---
        state: dict[str, Any] = {}
        current: str = entry
        visited: int = 0
        MAX_STEPS = 50  # 防无限循环
        last_resp: dict | None = None

        while current and current not in ("__end__", "END"):
            if visited >= MAX_STEPS:
                raise StrategyError(f"LangGraph exceeded max steps ({MAX_STEPS}), possible cycle")
            visited += 1

            node_cfg = nodes_cfg.get(current)
            if node_cfg is None:  # UNCOVERED: current 只可能来自已校验的 entry 或 edge 目标，此分支不可达
                raise ConfigurationError(f"Node '{current}' not found in nodes config")

            # 解析 group_id
            node_gid = node_cfg.get("group_id")
            if node_gid is None:
                node_gid = group_id

            endpoints = await _get_endpoints(node_gid)
            available = filter_available(endpoints, cooldown, node_gid)
            if not available:
                raise NoAvailableModelError(
                    f"All models on cooldown for node '{current}' (group {node_gid})"
                )

            # 构造 prompt
            prompt_template: str = node_cfg.get("prompt", "")
            try:
                prompt = prompt_template.format(
                    messages=_format_messages(messages),
                    state=json.dumps(state, ensure_ascii=False),
                )
            except KeyError as e:
                raise ConfigurationError(
                    f"Node '{current}' prompt has unknown placeholder: {e}"
                ) from e

            # LLM 调用
            node_messages = [{"role": "user", "content": prompt}]
            truncated = truncate_messages(node_messages, available, max_tokens)

            ep = available[0]
            resp = await call_llm(
                ep,
                truncated,
                node_gid,
                cooldown,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
            if resp is None:
                raise NoAvailableModelError(f"LLM call failed for node '{current}'")

            last_resp = resp

            # 提取 content
            content = _extract_content(resp)
            state[current] = content

            log.debug("LangGraph node '{}' completed, content length={}", current, len(content))

            # 选择下一个节点
            next_node: str | None = None
            edges_from = adjacency.get(current, [])
            if len(edges_from) == 0:
                # 无出边 → 结束
                current = None
            elif len(edges_from) == 1:
                current = edges_from[0][0]
            else:
                # 多条出边 → 条件匹配
                for target, condition in edges_from:
                    if condition is None or condition in content:
                        current = target
                        break
                else:
                    # 无匹配条件 → 取第一条（默认）
                    current = edges_from[0][0]

        # 确定最终返回
        if final_node and final_node in state:
            return _build_response(state[final_node], last_resp)
        if final_node and final_node not in state:
            log.warning(
                "final_node '{}' was not visited; returning last available response",
                final_node,
            )
        if last_resp is not None:
            return last_resp
        raise ProviderError("LangGraph produced no response")


def _format_messages(messages: list[dict]) -> str:
    """将 messages 格式化为可读字符串用于 prompt template。"""
    parts = []
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]: {content}")
        else:
            parts.append(f"[{role}]: {json.dumps(content, ensure_ascii=False)}")
    return "\n".join(parts)


def _extract_content(resp: dict) -> str:
    """从 LLM 响应中提取文本内容。"""
    choices = resp.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        return msg.get("content", "")
    return ""


def _build_response(final_content: str, base_resp: dict | None) -> dict:
    """用指定内容构建标准 LLM 响应 dict。"""
    if base_resp is None:
        return {
            "id": "langgraph",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": final_content}, "finish_reason": "stop"}
            ],
        }
    # 复制 base_resp 并替换 content
    resp = copy.deepcopy(base_resp)
    if resp.get("choices"):
        resp["choices"][0]["message"]["content"] = final_content
    return resp


# --- 注册策略 ---
register_strategy("langgraph", LangGraphStrategy)
