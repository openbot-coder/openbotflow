"""Tests for streaming fallback (SG-1)：驱动 core._drive / engine.run 的组级降级 + 传输层。

- T1.2：主组全冷却 → 驱动降级到 backup 组（流式与非流式对照，缺陷 1 修复）
- T7.2 / T7.3：_stream_common 不再持有端点重试环；fallback_attempted 全仓清零
- 端点级流式回退 / 选端点语义已迁到 test_langgraph_engine.py（T4.x）
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from botflow import core
from botflow.common.exceptions import AllModelsCooldownError, ProviderError
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.models import GroupModelWithDetails, ModelGroup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubProvider:
    """chat_stream yields behavior items; Exception items are raised."""

    def __init__(self, behavior: list) -> None:
        self.behavior = behavior
        self.calls = 0

    async def chat_stream(self, *args, **kwargs):
        self.calls += 1
        result = self.behavior[min(self.calls - 1, len(self.behavior) - 1)]
        if isinstance(result, Exception):
            raise result
        for item in result:
            if isinstance(item, Exception):
                raise item
            yield item


def _make_endpoint(model_id: int, provider: StubProvider, max_retries: int = 3) -> ModelEndpoint:
    detail = GroupModelWithDetails(
        id=model_id,
        group_id=1,
        model_id=model_id,
        weight=1.0,
        is_enabled=True,
        model_name=f"model-{model_id}",
        display_name=f"model-{model_id}",
        provider_id=1,
        provider_name="p",
        provider_type="openai",
        max_retries=max_retries,
        cooldown_seconds=60,
        cooldown_failure_threshold=3,
    )
    return ModelEndpoint(detail, provider)


def _make_group(group_id=1, fallback_group_id=None):
    return ModelGroup(
        id=group_id, name=f"group-{group_id}", description="", is_enabled=True,
        type="random_weights", params={}, fallback_group_id=fallback_group_id,
        created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
    )


# NOTE (SG-1 §3.3): 旧的 StubEngine（打桩 route_stream 返回端点列表）已由下方
# SG-1 版 StubEngine（按 group.id 返回 ("chunk", c)/("state", s) 事件序列）取代，
# 此处不再保留旧桩。

def _serialize(chunk: dict) -> tuple[list[str], dict | None]:
    return [f"data: {chunk['content']}\n\n"], None


async def _collect(agen) -> list[str]:
    return [line async for line in agen]


class _LogCapture(list):
    """Captures both ``_log_call`` (as list items) and ``_log_attempts``
    (via the ``.attempts`` attribute), so existing ``env`` usages that
    iterate the log list keep working while SG-0 attempt-logging tests can
    read ``env.attempts``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[dict] = []


@pytest.fixture
def env(monkeypatch):
    cap = _LogCapture()

    async def fake_log_call(**kwargs):
        cap.append(kwargs)

    async def fake_log_attempts(rows):
        if rows:
            cap.attempts.extend(rows if isinstance(rows, list) else [rows])

    monkeypatch.setattr(core, "_log_call", fake_log_call)
    # SG-0: also capture the new attempt-logging sink so F6/F7 can assert on it.
    monkeypatch.setattr(core, "_log_attempts", fake_log_attempts)
    return cap


def _setup(monkeypatch, engine: StubEngine) -> None:
    monkeypatch.setattr(
        core,
        "_get_extra_route_params",
        AsyncMock(return_value=(1, engine, _make_group(), {})),
    )


# ---------------------------------------------------------------------------
# NOTE (SG-1 §3.3 / §3.2): 以下 14 条旧 ``_stream_common`` 端点级回退用例已被删除。
# SG-1 把端点级重试/回退迁入图（try_stream 节点，见 test_langgraph_engine.py T4.x），
# 组级降级迁入驱动 core._drive（见本文件 T1.2 与 test_core_runtime.py T1.x）。
# ``_stream_common`` 自身不再持有重试环 / fallback_attempted（T7.2 / T7.3）。
# 旧用例意图已被 T4.x（端点级流式）+ T1.2（流式组级降级）+ T5.1（传输层）覆盖。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# StubEngine（SG-1 §3.3 重写）：由打桩 route_stream() 改为按 group.id 返回
# 「图事件序列」。core._drive / engine.run 是新的调用目标：
#   - run(mode="chat") 返回最终 result dict（含 _attempts）
#   - run(mode="stream") / stream_events 返回 async gen，产出 ("chunk", c) / ("state", s)
# ---------------------------------------------------------------------------


class StubEngine:
    """SG-1 §3.3：按 group.id 返回图事件序列的引擎桩。"""

    def __init__(self, cooldown=None):
        self.cooldown = cooldown or CooldownManager()
        self._groups: dict = {}  # gid -> (events, error)
        self.run_calls: list = []  # 记录 ("gid", mode)

    def register_group(self, group_id, events=None, error=None):
        self._groups[group_id] = (events or [], error)

    async def run(self, strategy, group, mode, **kw):
        gid = getattr(group, "id", group)
        self.run_calls.append((gid, mode))
        events, error = self._groups.get(gid, (None, None))
        if error is not None:
            raise error
        if mode == "chat":
            return events  # 最终 result dict
        # 流式：事件 async iterator
        async def _gen():
            for ev in (events or []):
                yield ev
        return _gen()

    async def stream_events(self, strategy, group, **kw):
        gid = getattr(group, "id", group)
        events, error = self._groups.get(gid, (None, None))
        if error is not None:
            raise error

        async def _gen():
            for ev in (events or []):
                yield ev
        return _gen()


# ---------------------------------------------------------------------------
# F1 / T1.2：流式组级降级（缺陷 1 修复）—— 主组全冷却 → 驱动降级到 backup 组
# ---------------------------------------------------------------------------


async def test_driver_group_fallback_works_for_stream(monkeypatch, env):
    """T1.2（正例，缺陷 1 核心）：主组全冷却（run 抛 AllModelsCooldownError）
    → 备份组可用 → **流式同样降级成功**，SSE 正常收尾 [DONE]。改动前此场景返回 502。"""
    engine = StubEngine()
    engine.register_group(1, error=AllModelsCooldownError("Group 1: all models on cooldown"))
    engine.register_group(
        2,
        events=[
            ("chunk", {"choices": [{"delta": {"content": "fallback-ok"}}]}),
            ("state", {"recoverable": True, "used_model_id": 2, "provider_id": 1}),
        ],
    )

    primary = _make_group(group_id=1, fallback_group_id=2)
    backup = _make_group(group_id=2)

    monkeypatch.setattr(
        core, "_get_extra_route_params",
        AsyncMock(return_value=(1, engine, primary, {})),
    )
    monkeypatch.setattr(core, "_load_group", AsyncMock(return_value=backup))

    out = [
        line async for line in core._drive(
            {"model": "x", "messages": []}, mode="stream",
        )
    ]
    assert out[-1] == "data: [DONE]\n\n"
    assert any("fallback-ok" in line for line in out)
    # 驱动对主组与备份组各调用一次 run（流式）
    assert engine.run_calls == [(1, "stream"), (2, "stream")]
    assert len([c for c in engine.run_calls if c[1] == "stream"]) == 2


async def test_driver_group_fallback_works_for_stream_non_stream_counterpart(monkeypatch, env):
    """T1.2 对照：同一 stub，仅 mode="chat" 也应降级成功（保证流式与
    非流式共用驱动骨架）。"""
    engine = StubEngine()
    engine.register_group(1, error=AllModelsCooldownError("Group 1: all models on cooldown"))
    engine.register_group(2, events={"choices": [{"message": {"content": "fallback-ok"}}], "_routing": {"model_id": 2}})

    primary = _make_group(group_id=1, fallback_group_id=2)
    backup = _make_group(group_id=2)

    monkeypatch.setattr(
        core, "_get_extra_route_params",
        AsyncMock(return_value=(1, engine, primary, {})),
    )
    monkeypatch.setattr(core, "_load_group", AsyncMock(return_value=backup))

    result = await core._drive({"model": "x", "messages": []}, mode="chat")
    assert result["choices"][0]["message"]["content"] == "fallback-ok"
    assert len([c for c in engine.run_calls if c[1] == "chat"]) == 2


# ---------------------------------------------------------------------------
# F7 / T7.2：_stream_common 不再持有端点重试环；T7.3：fallback_attempted 全仓清零
# ---------------------------------------------------------------------------


async def test_stream_common_has_no_own_retry_loop(monkeypatch):
    """T7.2：SG-1 后 core._stream_common 体内不含 `for attempt in range(`（端点重试环
    已迁入图），行数从 ~180 降到 ~60 量级（断言上限 < 90 即可，别写死精确值）。"""
    import inspect

    from botflow import core as _core

    source = inspect.getsource(_core._stream_common)
    assert "for attempt in range(" not in source, (
        "_stream_common must not contain an endpoint retry loop after SG-1"
    )
    assert len(source.splitlines()) < 90, (
        f"_stream_common is too long ({len(source.splitlines())} lines); expected < 90"
    )


async def test_fallback_attempted_symbol_fully_removed(monkeypatch):
    """T7.3：全仓 src/ grep `fallback_attempted` → 0 命中（参照 test_context 写法）。"""
    from pathlib import Path

    from botflow import core as _core

    src_root = Path(_core.__file__).resolve().parent
    hits = [
        str(p)
        for p in src_root.rglob("*.py")
        if "fallback_attempted" in p.read_text(encoding="utf-8")
    ]
    assert hits == [], f"`fallback_attempted` still present in: {hits}"
