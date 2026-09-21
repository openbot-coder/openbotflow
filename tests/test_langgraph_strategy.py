"""Tests for LangGraphStrategy — 基于有向图的多步 LLM 工作流策略。

覆盖三类场景：
- 配置校验（nodes / entry / edges 的非法与合法输入）
- 图执行（单节点、线性边、条件边三分支、group 兜底与 endpoints 缓存、防环、final 解析）
- 辅助函数（_format_messages / _extract_content / _build_response）与注册表
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from botflow.common.exceptions import (
    ConfigurationError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.pipeline.base import STRATEGY_REGISTRY, StrategyError
from botflow.pipeline.langgraph_strategy import (
    LangGraphStrategy,
    _build_response,
    _extract_content,
    _format_messages,
)
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.models import GroupModelWithDetails


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_endpoint(
    model_id: int = 1,
    group_id: int = 1,
    context_window: int = 8192,
    max_retries: int = 3,
    cooldown_failure_threshold: int = 3,
    cooldown_seconds: int = 60,
    provider_id: int = 1,
    model_name: str = "test-model",
) -> ModelEndpoint:
    detail = GroupModelWithDetails(
        id=model_id,
        group_id=group_id,
        model_id=model_id,
        weight=1.0,
        is_enabled=True,
        model_name=model_name,
        display_name=model_name,
        provider_id=provider_id,
        provider_name="test-provider",
        provider_type="openai",
        max_retries=max_retries,
        cooldown_seconds=cooldown_seconds,
        cooldown_failure_threshold=cooldown_failure_threshold,
        context_window=context_window,
    )
    return ModelEndpoint(detail, MagicMock())


def llm_resp(content: str) -> dict:
    return {
        "id": f"resp-{content}",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


# langgraph_strategy 在 execute() 内部按需 import，故 patch 打在 _shared 命名空间
_PATCH_LOAD = "botflow.pipeline._shared.load_endpoints"
_PATCH_FILTER = "botflow.pipeline._shared.filter_available"
_PATCH_TRUNCATE = "botflow.pipeline._shared.truncate_messages"
_PATCH_CALL_LLM = "botflow.pipeline._shared.call_llm"

MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.fixture
def cooldown():
    return CooldownManager()


# ===========================================================================
# 1. 配置校验
# ===========================================================================


class TestValidation:

    async def test_select_endpoints_is_not_supported(self, cooldown):
        strategy = LangGraphStrategy(params={})
        with pytest.raises(StrategyError, match="select_endpoints is not used"):
            await strategy.select_endpoints(MESSAGES, None, cooldown, 1)

    async def test_missing_nodes_raises(self, cooldown):
        strategy = LangGraphStrategy(params={"entry": "a"})
        with pytest.raises(ConfigurationError, match="requires 'nodes'"):
            await strategy.execute(MESSAGES, None, cooldown, 1)

    async def test_empty_entry_raises(self, cooldown):
        strategy = LangGraphStrategy(params={"nodes": {"a": {"prompt": "x"}}})
        with pytest.raises(ConfigurationError, match="Invalid entry node"):
            await strategy.execute(MESSAGES, None, cooldown, 1)

    async def test_entry_not_in_nodes_raises(self, cooldown):
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "x"}}, "entry": "zzz"}
        )
        with pytest.raises(ConfigurationError, match="Invalid entry node"):
            await strategy.execute(MESSAGES, None, cooldown, 1)

    async def test_entry_is_terminator_raises_no_response(self, cooldown):
        """entry 本身是终止符时 while 循环零次执行，落到「produced no response」。

        `entry in nodes_cfg` 的校验不排除 __end__/END，故这是一条**可达**路径，
        不是不可覆盖的死分支。
        """
        strategy = LangGraphStrategy(
            params={"nodes": {"__end__": {"prompt": "x"}}, "entry": "__end__"}
        )
        with pytest.raises(ProviderError, match="produced no response"):
            await strategy.execute(MESSAGES, None, cooldown, 1)

    async def test_edge_not_sequence_raises(self, cooldown):
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "x"}}, "entry": "a", "edges": ["a"]}
        )
        with pytest.raises(ConfigurationError, match="Invalid edge format"):
            await strategy.execute(MESSAGES, None, cooldown, 1)

    async def test_edge_too_short_raises(self, cooldown):
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "x"}}, "entry": "a", "edges": [["a"]]}
        )
        with pytest.raises(ConfigurationError, match="Invalid edge format"):
            await strategy.execute(MESSAGES, None, cooldown, 1)

    async def test_edge_source_not_in_nodes_raises(self, cooldown):
        strategy = LangGraphStrategy(
            params={
                "nodes": {"a": {"prompt": "x"}},
                "entry": "a",
                "edges": [["ghost", "a"]],
            }
        )
        with pytest.raises(ConfigurationError, match="Edge source 'ghost' not in nodes"):
            await strategy.execute(MESSAGES, None, cooldown, 1)

    async def test_edge_target_not_in_nodes_raises(self, cooldown):
        strategy = LangGraphStrategy(
            params={
                "nodes": {"a": {"prompt": "x"}},
                "entry": "a",
                "edges": [["a", "ghost"]],
            }
        )
        with pytest.raises(ConfigurationError, match="Edge target 'ghost' not in nodes"):
            await strategy.execute(MESSAGES, None, cooldown, 1)

    async def test_prompt_unknown_placeholder_raises(self, cooldown):
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "hi {nope}"}}, "entry": "a"}
        )
        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[make_endpoint()]),
            patch(_PATCH_FILTER, return_value=[make_endpoint()]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
        ):
            with pytest.raises(ConfigurationError, match="unknown placeholder"):
                await strategy.execute(MESSAGES, None, cooldown, 1)


# ===========================================================================
# 2. 图执行
# ===========================================================================


class TestExecution:

    async def test_single_node_returns_last_response(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={"nodes": {"only": {"prompt": "Say hi"}}, "entry": "only"}
        )
        resp = llm_resp("answer")

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(resp, None)),
        ):
            result = await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        # 无 final → 直接返回最后一次 LLM 原始响应
        assert result is resp

    async def test_final_node_output_is_returned(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {"a": {"prompt": "first"}, "b": {"prompt": "second {state}"}},
                "edges": [["a", "b"]],
                "entry": "a",
                "final": "b",
            }
        )
        responses = [(llm_resp("content-a"), None), (llm_resp("content-b"), None)]

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, side_effect=responses),
        ):
            result = await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        assert result["choices"][0]["message"]["content"] == "content-b"
        # base_resp 的其他字段被保留
        assert result["id"] == "resp-content-b"

    async def test_final_node_unvisited_falls_back(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {"a": {"prompt": "first"}},
                "entry": "a",
                "final": "ghost",
            }
        )
        resp = llm_resp("content-a")

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(resp, None)),
        ):
            result = await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        assert result is resp

    async def test_state_is_passed_to_next_node_prompt(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {"a": {"prompt": "first"}, "b": {"prompt": "prev={state}"}},
                "edges": [["a", "b"]],
                "entry": "a",
                "final": "b",
            }
        )
        call_mock = AsyncMock(
            side_effect=[(llm_resp("content-a"), None), (llm_resp("content-b"), None)]
        )

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, call_mock),
        ):
            await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        second_prompt = call_mock.call_args_list[1].args[1][0]["content"]
        assert '"a": "content-a"' in second_prompt

    async def test_messages_placeholder_rendered(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "user said {messages}"}}, "entry": "a"}
        )
        call_mock = AsyncMock(return_value=(llm_resp("ok"), None))

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, call_mock),
        ):
            await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        prompt = call_mock.call_args.args[1][0]["content"]
        assert prompt == "user said [user]: hello"

    # -- endpoints 解析 --------------------------------------------------

    async def test_node_group_id_overrides_default(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "x", "group_id": 7}}, "entry": "a"}
        )
        load_mock = AsyncMock(return_value=[ep])

        with (
            patch(_PATCH_LOAD, load_mock),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(llm_resp("ok"), None)),
        ):
            await strategy.execute(MESSAGES, MagicMock(), cooldown, group_id=1)

        assert load_mock.call_args.args[0] == 7

    async def test_endpoints_are_cached_per_group(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {"a": {"prompt": "first"}, "b": {"prompt": "second"}},
                "edges": [["a", "b"]],
                "entry": "a",
            }
        )
        load_mock = AsyncMock(return_value=[ep])

        with (
            patch(_PATCH_LOAD, load_mock),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(
                _PATCH_CALL_LLM,
                new_callable=AsyncMock,
                side_effect=[(llm_resp("a"), None), (llm_resp("b"), None)],
            ),
        ):
            await strategy.execute(MESSAGES, MagicMock(), cooldown, group_id=1)

        # 两个节点同属 group 1 → 只加载一次
        assert load_mock.call_count == 1

    async def test_group_without_enabled_models_raises(self, cooldown):
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "x"}}, "entry": "a"}
        )
        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[]):
            with pytest.raises(NoAvailableModelError, match="has no enabled models"):
                await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

    async def test_all_models_on_cooldown_raises(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "x"}}, "entry": "a"}
        )
        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[]),
        ):
            with pytest.raises(NoAvailableModelError, match="All models on cooldown"):
                await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

    async def test_llm_failure_raises(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={"nodes": {"a": {"prompt": "x"}}, "entry": "a"}
        )
        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(None, ProviderError("call_llm failed"))),
        ):
            with pytest.raises(NoAvailableModelError, match="LLM call failed"):
                await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

    # -- 边选择 -----------------------------------------------------------

    async def test_condition_match_selects_matching_branch(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {
                    "a": {"prompt": "first"},
                    "b": {"prompt": "b"},
                    "c": {"prompt": "c"},
                },
                "edges": [["a", "b", "NEED_B"], ["a", "c", "NEED_C"]],
                "entry": "a",
                "final": "c",
            }
        )
        responses = [(llm_resp("go NEED_C now"), None), (llm_resp("content-c"), None)]

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, side_effect=responses),
        ):
            result = await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        assert result["choices"][0]["message"]["content"] == "content-c"

    async def test_condition_no_match_defaults_to_first_edge(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {
                    "a": {"prompt": "first"},
                    "b": {"prompt": "b"},
                    "c": {"prompt": "c"},
                },
                "edges": [["a", "b", "NEED_B"], ["a", "c", "NEED_C"]],
                "entry": "a",
                "final": "b",
            }
        )
        responses = [(llm_resp("nothing matches"), None), (llm_resp("content-b"), None)]

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, side_effect=responses),
        ):
            result = await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        assert result["choices"][0]["message"]["content"] == "content-b"

    async def test_unconditional_edge_wins_over_conditional(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {
                    "a": {"prompt": "first"},
                    "b": {"prompt": "b"},
                    "c": {"prompt": "c"},
                },
                "edges": [["a", "b"], ["a", "c", "NEED_C"]],
                "entry": "a",
                "final": "b",
            }
        )
        # content 含 NEED_C，但首条无条件边先匹配 → 走 b
        responses = [(llm_resp("NEED_C"), None), (llm_resp("content-b"), None)]

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, side_effect=responses),
        ):
            result = await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        assert result["choices"][0]["message"]["content"] == "content-b"

    async def test_edge_to_end_marker_terminates(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {"a": {"prompt": "first"}, "b": {"prompt": "b"}},
                "edges": [["a", "b"], ["b", "__end__"]],
                "entry": "a",
            }
        )
        responses = [(llm_resp("a"), None), (llm_resp("b"), None)]

        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, side_effect=responses) as call_mock,
        ):
            result = await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)

        assert call_mock.call_count == 2
        assert result["choices"][0]["message"]["content"] == "b"

    async def test_cycle_hits_max_steps(self, cooldown):
        ep = make_endpoint()
        strategy = LangGraphStrategy(
            params={
                "nodes": {"a": {"prompt": "a"}, "b": {"prompt": "b"}},
                "edges": [["a", "b"], ["b", "a"]],
                "entry": "a",
            }
        )
        with (
            patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep]),
            patch(_PATCH_FILTER, return_value=[ep]),
            patch(_PATCH_TRUNCATE, side_effect=lambda m, e, t: m),
            patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(llm_resp("x"), None)),
        ):
            with pytest.raises(StrategyError, match="exceeded max steps"):
                await strategy.execute(MESSAGES, MagicMock(), cooldown, 1)


# ===========================================================================
# 3. 辅助函数与注册表
# ===========================================================================


class TestHelpers:

    def test_format_messages_string_content(self):
        out = _format_messages([{"role": "user", "content": "hi"}])
        assert out == "[user]: hi"

    def test_format_messages_structured_content(self):
        out = _format_messages(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        )
        assert out == '[user]: [{"type": "text", "text": "hi"}]'

    def test_format_messages_defaults_missing_role(self):
        out = _format_messages([{"content": "hi"}])
        assert out == "[unknown]: hi"

    def test_extract_content_with_choices(self):
        assert _extract_content(llm_resp("hello")) == "hello"

    def test_extract_content_without_choices(self):
        assert _extract_content({"id": "x"}) == ""

    def test_build_response_without_base(self):
        resp = _build_response("final text", None)
        assert resp["id"] == "langgraph"
        assert resp["object"] == "chat.completion"
        assert resp["choices"][0]["message"]["content"] == "final text"
        assert resp["choices"][0]["finish_reason"] == "stop"

    def test_build_response_replaces_content(self):
        base = llm_resp("old")
        resp = _build_response("new", base)
        assert resp["choices"][0]["message"]["content"] == "new"
        # 深拷贝：原对象不被修改
        assert base["choices"][0]["message"]["content"] == "old"

    def test_build_response_without_choices_key(self):
        resp = _build_response("new", {"id": "x", "object": "chat.completion"})
        assert resp == {"id": "x", "object": "chat.completion"}

    def test_strategy_is_registered(self):
        assert STRATEGY_REGISTRY["langgraph"] is LangGraphStrategy
