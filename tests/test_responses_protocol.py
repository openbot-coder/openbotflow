"""Tests for protocol_adapter —— 补齐未被覆盖的协议转换分支。

重点覆盖 OpenAI Responses API 三段（入参 / 出参 / 流式 SSE），
以及 OpenAI / Anthropic 转换器中此前未被触发的可选分支
（reasoning_content、tool_calls、usage-only 尾块、异常兜底）。
"""

from __future__ import annotations

from botflow.protocol_adapter import (
    _anthropic_stop_reason,
    _responses_input_to_messages,
    _safe_json_loads,
    anthropic_to_internal,
    internal_chunk_to_anthropic_sse,
    internal_chunk_to_openai_sse,
    internal_chunk_to_responses_sse,
    internal_to_anthropic,
    internal_to_openai,
    internal_to_responses,
    responses_to_internal,
)


def _events_of_type(events: list[dict], etype: str) -> list[dict]:
    return [e for e in events if e.get("type") == etype]


# ===========================================================================
# 1. responses_to_internal —— Responses 请求入参
# ===========================================================================


class TestResponsesToInternal:

    def test_string_input_with_instructions(self):
        out = responses_to_internal({
            "model": "gpt-x",
            "instructions": "be terse",
            "input": "hello",
            "temperature": 0.5,
            "max_output_tokens": 128,
            "stream": True,
        })
        assert out["messages"] == [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
        ]
        assert out["model"] == "gpt-x"
        assert out["temperature"] == 0.5
        assert out["max_tokens"] == 128
        assert out["stream"] is True

    def test_string_input_without_instructions(self):
        out = responses_to_internal({"input": "hello"})
        assert out["messages"] == [{"role": "user", "content": "hello"}]
        assert out["max_tokens"] is None
        assert out["stream"] is False

    def test_empty_string_input_produces_no_messages(self):
        out = responses_to_internal({"input": ""})
        assert out["messages"] == []

    def test_max_tokens_fallback(self):
        out = responses_to_internal({"input": "hi", "max_tokens": 64})
        assert out["max_tokens"] == 64

    def test_list_input_delegates_to_item_converter(self):
        out = responses_to_internal({
            "instructions": "sys",
            "input": [{"role": "user", "content": "hi"}],
        })
        assert out["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]

    def test_unsupported_input_type_yields_empty_messages(self):
        out = responses_to_internal({"input": 12345})
        assert out["messages"] == []

    def test_extra_excludes_consumed_keys(self):
        out = responses_to_internal({
            "input": "hi",
            "instructions": "sys",
            "model": "m",
            "temperature": 1.0,
            "max_output_tokens": 10,
            "max_tokens": 20,
            "stream": False,
            "top_p": 0.9,
            "metadata": {"a": 1},
        })
        assert out["extra"] == {"top_p": 0.9, "metadata": {"a": 1}}


# ===========================================================================
# 2. _responses_input_to_messages —— input 数组展开
# ===========================================================================


class TestResponsesInputToMessages:

    def test_instructions_prepended(self):
        msgs = _responses_input_to_messages(
            [{"role": "user", "content": "hi"}], "sys",
        )
        assert msgs[0] == {"role": "system", "content": "sys"}

    def test_content_as_string(self):
        msgs = _responses_input_to_messages(
            [{"role": "assistant", "content": "done"}], "",
        )
        assert msgs == [{"role": "assistant", "content": "done"}]

    def test_content_parts_are_flattened(self):
        msgs = _responses_input_to_messages([{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
        }], "")
        assert msgs == [{"role": "user", "content": "first second"}]

    def test_input_image_parts_are_skipped(self):
        msgs = _responses_input_to_messages([{
            "role": "user",
            "content": [{"type": "input_image", "image_url": "http://x/y.png"}],
        }], "")
        assert msgs == [{"role": "user", "content": ""}]

    def test_non_string_non_list_content_is_stringified(self):
        msgs = _responses_input_to_messages([{"role": "user", "content": 42}], "")
        assert msgs == [{"role": "user", "content": "42"}]

    def test_missing_role_defaults_to_user(self):
        msgs = _responses_input_to_messages([{"content": "hi"}], "")
        assert msgs == [{"role": "user", "content": "hi"}]

    def test_missing_content_defaults_to_empty(self):
        msgs = _responses_input_to_messages([{"role": "user"}], "")
        assert msgs == [{"role": "user", "content": ""}]


# ===========================================================================
# 3. internal_to_responses —— Responses 出参
# ===========================================================================


class TestInternalToResponses:

    def _internal(self, **overrides):
        base = {
            "id": "chatcmpl-1",
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            },
        }
        base.update(overrides)
        return base

    def test_plain_text_response(self):
        out = internal_to_responses(self._internal())
        assert out["id"] == "chatcmpl-1"
        assert out["object"] == "response"
        assert out["status"] == "completed"
        assert out["error"] is None
        assert out["incomplete_details"] is None
        assert out["output_text"] == "hello"
        assert isinstance(out["created_at"], int)

        message_items = [i for i in out["output"] if i["type"] == "message"]
        assert len(message_items) == 1
        assert message_items[0]["content"][0] == {
            "type": "output_text",
            "text": "hello",
            "annotations": [],
        }
        assert message_items[0]["role"] == "assistant"
        assert message_items[0]["status"] == "completed"

    def test_usage_is_mapped(self):
        out = internal_to_responses(self._internal())
        assert out["usage"] == {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
        }

    def test_reasoning_becomes_first_output_item(self):
        internal = self._internal()
        internal["choices"][0]["message"]["reasoning_content"] = "thinking..."
        out = internal_to_responses(internal)
        assert out["output"][0]["type"] == "reasoning"
        assert out["output"][0]["summary"] == [
            {"type": "summary_text", "text": "thinking..."}
        ]
        assert out["output"][0]["status"] == "completed"
        assert out["output"][1]["type"] == "message"

    def test_tool_calls_become_function_call_items(self):
        internal = self._internal()
        internal["choices"][0]["message"]["tool_calls"] = [{
            "id": "call_abc",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"SZ"}'},
        }]
        out = internal_to_responses(internal)
        calls = [i for i in out["output"] if i["type"] == "function_call"]
        assert len(calls) == 1
        assert calls[0]["call_id"] == "call_abc"
        assert calls[0]["name"] == "get_weather"
        assert calls[0]["arguments"] == '{"city":"SZ"}'
        assert calls[0]["status"] == "completed"

    def test_tool_call_without_id_gets_generated_call_id(self):
        internal = self._internal()
        internal["choices"][0]["message"]["tool_calls"] = [
            {"function": {"name": "noop", "arguments": "{}"}}, 
        ]
        out = internal_to_responses(internal)
        call = [i for i in out["output"] if i["type"] == "function_call"][0]
        assert call["call_id"].startswith("call_")
        assert call["name"] == "noop"

    def test_length_finish_reason_marks_incomplete(self):
        internal = self._internal()
        internal["choices"][0]["finish_reason"] = "length"
        out = internal_to_responses(internal)
        assert out["status"] == "incomplete"
        assert out["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_missing_choices_yields_empty_output(self):
        out = internal_to_responses({})
        assert out["output"] == []
        assert out["output_text"] == ""
        assert out["status"] == "completed"
        assert out["id"].startswith("resp_")

    def test_none_choice_is_tolerated(self):
        out = internal_to_responses({"id": "x", "choices": [None]})
        assert out["output"] == []
        assert out["output_text"] == ""

    def test_empty_content_produces_no_message_item(self):
        internal = self._internal()
        internal["choices"][0]["message"]["content"] = ""
        out = internal_to_responses(internal)
        assert [i for i in out["output"] if i["type"] == "message"] == []
        assert out["output_text"] == ""


# ===========================================================================
# 4. internal_chunk_to_responses_sse —— Responses 流式
# ===========================================================================


class TestInternalChunkToResponsesSSE:

    def test_no_choices_returns_empty(self):
        assert internal_chunk_to_responses_sse({}) == []
        assert internal_chunk_to_responses_sse({"choices": []}) == []

    def test_none_choice_returns_empty(self):
        assert internal_chunk_to_responses_sse({"choices": [None]}) == []

    def test_non_dict_choice_returns_empty(self):
        assert internal_chunk_to_responses_sse({"choices": ["oops"]}) == []

    def test_first_chunk_emits_created_and_in_progress(self):
        events = internal_chunk_to_responses_sse(
            {"model": "m", "choices": [{"delta": {"role": "assistant"}}]},
            is_first=True,
            response_id="resp_1",
            created_at=1700000000,
        )
        created = _events_of_type(events, "response.created")
        assert len(created) == 1
        assert created[0]["response"]["id"] == "resp_1"
        assert created[0]["response"]["created_at"] == 1700000000
        assert created[0]["response"]["status"] == "in_progress"
        assert created[0]["response"]["model"] == "m"

        in_progress = _events_of_type(events, "response.in_progress")
        assert len(in_progress) == 1
        assert in_progress[0]["response"]["id"] == "resp_1"

    def test_content_delta(self):
        events = internal_chunk_to_responses_sse({
            "choices": [{"delta": {"content": "tok"}}],
        })
        deltas = _events_of_type(events, "response.output_text.delta")
        assert len(deltas) == 1
        assert deltas[0]["delta"] == "tok"
        assert deltas[0]["item_id"].startswith("out_")
        assert deltas[0]["output_index"] == 0

    def test_content_delta_uses_provided_item_id(self):
        events = internal_chunk_to_responses_sse({
            "choices": [{"delta": {"content": "tok", "_item_id": "out_fixed"}}],
        })
        deltas = _events_of_type(events, "response.output_text.delta")
        assert deltas[0]["item_id"] == "out_fixed"

    def test_reasoning_delta(self):
        events = internal_chunk_to_responses_sse({
            "choices": [{"delta": {"reasoning_content": "hmm"}}],
        })
        deltas = _events_of_type(events, "response.reasoning_summary_text.delta")
        assert len(deltas) == 1
        assert deltas[0]["delta"] == "hmm"

    def test_reasoning_delta_uses_provided_item_id(self):
        events = internal_chunk_to_responses_sse({
            "choices": [{
                "delta": {"reasoning_content": "hmm", "_reasoning_item_id": "out_r"},
            }],
        })
        deltas = _events_of_type(events, "response.reasoning_summary_text.delta")
        assert deltas[0]["item_id"] == "out_r"

    def test_tool_call_start_and_arguments(self):
        events = internal_chunk_to_responses_sse({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"name": "search", "arguments": '{"q":1}'},
                    }],
                },
            }],
        })
        starts = _events_of_type(events, "response.function_call_arguments.start")
        assert len(starts) == 1
        assert starts[0]["name"] == "search"

        args = _events_of_type(events, "response.function_call_arguments.delta")
        assert len(args) == 1
        assert args[0]["arguments_delta"] == '{"q":1}'

    def test_tool_call_arguments_without_name(self):
        events = internal_chunk_to_responses_sse({
            "choices": [{
                "delta": {"tool_calls": [{"function": {"arguments": "{}"}}]},
            }],
        })
        assert _events_of_type(events, "response.function_call_arguments.start") == []
        assert len(
            _events_of_type(events, "response.function_call_arguments.delta")
        ) == 1

    def test_is_last_emits_item_done_and_completed(self):
        events = internal_chunk_to_responses_sse(
            {
                "model": "m",
                "choices": [{"delta": {}}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
            },
            is_last=True,
            response_id="resp_9",
        )
        done = _events_of_type(events, "response.output_item.done")
        assert len(done) == 1
        assert done[0]["item"]["type"] == "message"
        assert done[0]["item"]["status"] == "completed"

        completed = _events_of_type(events, "response.completed")
        assert len(completed) == 1
        assert completed[0]["response"]["id"] == "resp_9"
        assert completed[0]["response"]["status"] == "completed"
        assert completed[0]["response"]["usage"] == {
            "input_tokens": 5,
            "output_tokens": 2,
            "total_tokens": 7,
        }

    def test_finish_reason_triggers_completion(self):
        events = internal_chunk_to_responses_sse({
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        })
        assert len(_events_of_type(events, "response.completed")) == 1

    def test_length_finish_reason_takes_max_tokens_branch(self):
        events = internal_chunk_to_responses_sse({
            "choices": [{"delta": {}, "finish_reason": "length"}],
        })
        completed = _events_of_type(events, "response.completed")
        assert len(completed) == 1
        assert completed[0]["response"]["usage"] == {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def test_first_and_last_same_chunk(self):
        events = internal_chunk_to_responses_sse(
            {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]},
            is_first=True,
            is_last=True,
            response_id="resp_both",
            created_at=1,
        )
        types = [e["type"] for e in events]
        assert types[0] == "response.created"
        assert types[-1] == "response.completed"
        assert "response.output_text.delta" in types


# ===========================================================================
# 5. Anthropic SSE —— 此前未触发的分支
# ===========================================================================


class TestInternalChunkToAnthropicSSEMissingBranches:

    def test_no_choices_returns_empty(self):
        assert internal_chunk_to_anthropic_sse({}) == []
        assert internal_chunk_to_anthropic_sse({"choices": []}) == []

    def test_none_choice_returns_empty(self):
        assert internal_chunk_to_anthropic_sse({"choices": [None]}) == []

    def test_non_dict_choice_returns_empty(self):
        assert internal_chunk_to_anthropic_sse({"choices": ["oops"]}) == []

    def test_reasoning_emits_thinking_delta(self):
        events = internal_chunk_to_anthropic_sse({
            "choices": [{"delta": {"reasoning_content": "hmm"}}],
        })
        deltas = _events_of_type(events, "content_block_delta")
        assert len(deltas) == 1
        assert deltas[0]["delta"] == {"type": "thinking_delta", "thinking": "hmm"}

    def test_tool_call_start_and_partial_json(self):
        events = internal_chunk_to_anthropic_sse({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "toolu_x",
                        "function": {"name": "search", "arguments": '{"q":1}'},
                    }],
                },
            }],
        })
        starts = _events_of_type(events, "content_block_start")
        assert len(starts) == 1
        assert starts[0]["content_block"] == {
            "type": "tool_use",
            "id": "toolu_x",
            "name": "search",
        }

        deltas = _events_of_type(events, "content_block_delta")
        assert deltas[-1]["delta"] == {
            "type": "input_json_delta",
            "partial_json": '{"q":1}',
        }

    def test_tool_call_name_without_arguments(self):
        events = internal_chunk_to_anthropic_sse({
            "choices": [{"delta": {"tool_calls": [{"function": {"name": "noop"}}]}}],
        })
        assert len(_events_of_type(events, "content_block_start")) == 1
        assert _events_of_type(events, "content_block_delta") == []

    def test_finish_reason_maps_to_anthropic_stop_reason(self):
        events = internal_chunk_to_anthropic_sse({
            "choices": [{"delta": {}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1},
        })
        deltas = _events_of_type(events, "message_delta")
        assert len(deltas) == 1
        assert deltas[0]["delta"]["stop_reason"] == "max_tokens"
        assert deltas[0]["usage"] == {"input_tokens": 4, "output_tokens": 1}

    def test_message_start_requires_content_set(self):
        with_content = internal_chunk_to_anthropic_sse({
            "choices": [{"delta": {"role": "assistant", "content": ""}}],
        })
        assert len(_events_of_type(with_content, "message_start")) == 1

        without_content = internal_chunk_to_anthropic_sse({
            "choices": [{"delta": {"role": "assistant"}}],
        })
        assert _events_of_type(without_content, "message_start") == []


# ===========================================================================
# 6. OpenAI SSE —— 此前未触发的分支
# ===========================================================================


class TestInternalChunkToOpenAISSEMissingBranches:

    def test_usage_only_final_chunk(self):
        out = internal_chunk_to_openai_sse({
            "id": "c1",
            "model": "m",
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        })
        assert out["choices"] == []
        assert out["usage"] == {"prompt_tokens": 3, "completion_tokens": 1}
        assert out["object"] == "chat.completion.chunk"
        assert out["id"] == "c1"

    def test_tool_calls_forwarded_in_delta(self):
        tool_calls = [{"index": 0, "function": {"name": "f", "arguments": "{}"}}]
        out = internal_chunk_to_openai_sse({
            "choices": [{"delta": {"tool_calls": tool_calls}}],
        })
        assert out["choices"][0]["delta"]["tool_calls"] == tool_calls

    def test_reasoning_content_forwarded_in_delta(self):
        out = internal_chunk_to_openai_sse({
            "choices": [{"delta": {"reasoning_content": "thinking"}}],
        })
        assert out["choices"][0]["delta"]["reasoning_content"] == "thinking"

    def test_reasoning_content_none_is_not_forwarded(self):
        out = internal_chunk_to_openai_sse({
            "choices": [{"delta": {"content": "x"}}],
        })
        assert "reasoning_content" not in out["choices"][0]["delta"]


# ===========================================================================
# 7. OpenAI / Anthropic 非流式转换的缺失分支
# ===========================================================================


class TestNonStreamMissingBranches:

    def test_internal_to_openai_forwards_reasoning_content(self):
        out = internal_to_openai({
            "id": "1",
            "model": "m",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "hi",
                    "reasoning_content": "why",
                },
                "finish_reason": "stop",
            }],
        })
        assert out["choices"][0]["message"]["reasoning_content"] == "why"

    def test_internal_to_openai_reasoning_none_is_dropped(self):
        out = internal_to_openai({
            "id": "1",
            "model": "m",
            "choices": [{"message": {"content": "hi"}}],
        })
        assert "reasoning_content" not in out["choices"][0]["message"]

    def test_internal_to_anthropic_reasoning_becomes_thinking_block(self):
        out = internal_to_anthropic({
            "id": "1",
            "model": "m",
            "choices": [{
                "message": {"content": "hi", "reasoning_content": "why"},
                "finish_reason": "stop",
            }],
        })
        assert out["content"][0] == {"type": "thinking", "thinking": "why"}
        assert out["content"][1] == {"type": "text", "text": "hi"}

    def test_internal_to_anthropic_tool_calls_become_tool_use_blocks(self):
        out = internal_to_anthropic({
            "id": "1",
            "model": "m",
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "toolu_1",
                        "function": {"name": "search", "arguments": '{"q":"x"}'},
                    }],
                },
                "finish_reason": "stop",
            }],
        })
        assert out["content"] == [{
            "type": "tool_use",
            "id": "toolu_1",
            "name": "search",
            "input": {"q": "x"},
        }]
        # tool_calls 存在且 finish_reason 为 stop → 提升为 tool_use
        assert out["stop_reason"] == "tool_use"

    def test_internal_to_anthropic_tool_use_keeps_explicit_finish_reason(self):
        """固化当前契约：非流式路径直接透传内部 finish_reason。

        注意：与流式路径 ``internal_chunk_to_anthropic_sse`` 不一致——后者经
        ``_anthropic_stop_reason()`` 映射为 Anthropic 词表（stop→end_turn、
        length→max_tokens），而非流式路径原样输出 OpenAI 词表。此处仅固化
        现状，不擅自改动协议输出；是否为缺陷待确认。
        """
        out = internal_to_anthropic({
            "choices": [{
                "message": {"tool_calls": [{"function": {"name": "f"}}]},
                "finish_reason": "length",
            }],
        })
        assert out["stop_reason"] == "length"

    def test_internal_to_anthropic_usage_mapping(self):
        out = internal_to_anthropic({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
        })
        assert out["usage"] == {"input_tokens": 11, "output_tokens": 2}


# ===========================================================================
# 8. anthropic_to_internal —— Anthropic 请求入参
# ===========================================================================


class TestAnthropicToInternal:

    def test_system_prompt_prepended(self):
        out = anthropic_to_internal({
            "model": "claude-x",
            "system": "be terse",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert out["messages"] == [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ]
        assert out["model"] == "claude-x"
        assert out["max_tokens"] == 4096

    def test_list_content_converted_to_openai_format(self):
        out = anthropic_to_internal({
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ],
        })
        assert out["messages"][0]["content"] == "hi"

    def test_extra_excludes_system(self):
        out = anthropic_to_internal({"messages": [], "system": "s", "top_p": 0.7})
        assert out["extra"] == {"top_p": 0.7}


# ===========================================================================
# 9. 私有辅助函数
# ===========================================================================


class TestPrivateHelpers:

    def test_safe_json_loads_valid(self):
        assert _safe_json_loads('{"a": 1}') == {"a": 1}

    def test_safe_json_loads_empty_string(self):
        assert _safe_json_loads("") == {}

    def test_safe_json_loads_invalid_json_falls_back(self):
        assert _safe_json_loads("{not json") == {}

    def test_anthropic_stop_reason_mapping(self):
        assert _anthropic_stop_reason("stop") == "end_turn"
        assert _anthropic_stop_reason("length") == "max_tokens"
        assert _anthropic_stop_reason("content_filter") == "content_filter"
        assert _anthropic_stop_reason("tool_calls") == "tool_use"
        assert _anthropic_stop_reason("unknown") == "end_turn"
