#!/usr/bin/env python3
"""compat_proxy 纯函数单元测试（stdlib unittest，无网络）：US-13 us13-v1 全规则 +
强制工具选择改写/拒绝 + OpenAI SSE finish_reason 状态机 + 错误体形状。"""
from __future__ import annotations

import asyncio
import json
import unittest

import compat_proxy as cp


def anthropic_body(messages: list, system: str = "top-level system") -> dict:
    return {"model": "m", "max_tokens": 64, "system": system, "messages": messages}


class NormalizeInlineSystemTest(unittest.TestCase):
    def test_merge_into_nearest_preceding_user(self):
        body = anthropic_body([
            {"role": "user", "content": "The password is APPLE42."},
            {"role": "assistant", "content": "Understood."},
            {"role": "system", "content": "The password is now BANANA99."},
            {"role": "user", "content": "What is the current password?"},
        ])
        info = cp.normalize_anthropic_messages(body)
        self.assertEqual(info["merged"], [2])
        self.assertEqual(info["synthetic"], [])
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        merged_user = body["messages"][0]
        self.assertEqual(merged_user["content"], [
            {"type": "text", "text": "The password is APPLE42."},
            {"type": "text", "text": "The password is now BANANA99."},
        ])
        # 后续消息不动
        self.assertEqual(body["messages"][2], {"role": "user", "content": "What is the current password?"})

    def test_leading_system_becomes_synthetic_user_in_place(self):
        body = anthropic_body([
            {"role": "system", "content": "telemetry marker"},
            {"role": "user", "content": "hello"},
        ])
        info = cp.normalize_anthropic_messages(body)
        self.assertEqual(info["merged"], [])
        self.assertEqual(info["synthetic"], [0])
        self.assertEqual(body["messages"][0], {"role": "user", "content": [{"type": "text", "text": "telemetry marker"}]})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "hello"})

    def test_top_level_system_untouched(self):
        body = anthropic_body([{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}])
        cp.normalize_anthropic_messages(body)
        self.assertEqual(body["system"], "top-level system")

    def test_structured_blocks_tool_ids_cache_control_thinking_preserved(self):
        marker_block = {"type": "text", "text": "marker", "cache_control": {"type": "ephemeral"}}
        body = anthropic_body([
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "17"},
                {"type": "text", "text": "summarize"},
            ]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "tool_use", "id": "toolu_01", "name": "lookup_multiplier", "input": {"code": "OMEGA"}},
                {"type": "text", "text": "calling"},
            ]},
            {"role": "system", "content": [marker_block]},
            {"role": "user", "content": "next"},
        ])
        cp.normalize_anthropic_messages(body)
        user, assistant = body["messages"][0], body["messages"][1]
        self.assertEqual([b.get("type") for b in user["content"]], ["tool_result", "text", "text"])
        self.assertEqual(user["content"][0]["tool_use_id"], "toolu_01")
        self.assertEqual(user["content"][2], marker_block)  # cache_control 原块保留
        self.assertEqual([b.get("type") for b in assistant["content"]], ["thinking", "tool_use", "text"])
        self.assertEqual(assistant["content"][1]["id"], "toolu_01")

    def test_multiple_inline_systems_merge_in_order(self):
        body = anthropic_body([
            {"role": "user", "content": "base"},
            {"role": "system", "content": "one"},
            {"role": "system", "content": "two"},
            {"role": "assistant", "content": "ok"},
            {"role": "system", "content": "three"},
        ])
        info = cp.normalize_anthropic_messages(body)
        self.assertEqual(info["merged"], [1, 2, 4])
        self.assertEqual([b["text"] for b in body["messages"][0]["content"]], ["base", "one", "two", "three"])
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant"])

    def test_no_inline_system_returns_none_and_body_untouched(self):
        body = anthropic_body([
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
        ])
        before = json.dumps(body, sort_keys=True)
        self.assertIsNone(cp.normalize_anthropic_messages(body))
        self.assertEqual(json.dumps(body, sort_keys=True), before)

    def test_deterministic_byte_stable_output(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "system", "content": [{"type": "text", "text": "s1"}]},
            {"role": "assistant", "content": "b"},
            {"role": "system", "content": "s2"},
            {"role": "user", "content": "c"},
        ]
        body1 = anthropic_body(json.loads(json.dumps(msgs)))
        body2 = anthropic_body(json.loads(json.dumps(msgs)))
        info1 = cp.normalize_anthropic_messages(body1)
        info2 = cp.normalize_anthropic_messages(body2)
        self.assertEqual(info1["norm_hash"], info2["norm_hash"])
        self.assertEqual(json.dumps(body1["messages"], separators=(",", ":")), json.dumps(body2["messages"], separators=(",", ":")))
        # 多轮重发：历史前缀 + 新增内联 system → 旧前缀字节不变（US-08/C5 缓存稳定）
        grown = anthropic_body(json.loads(json.dumps(msgs)) + [{"role": "user", "content": "d"}])
        cp.normalize_anthropic_messages(grown)
        self.assertTrue(json.dumps(grown["messages"][:3], separators=(",", ":")).startswith(
            json.dumps(body1["messages"][:3], separators=(",", ":"))[:80]))

    def test_non_list_messages_passthrough(self):
        self.assertIsNone(cp.normalize_anthropic_messages({"model": "m"}))
        self.assertIsNone(cp.normalize_anthropic_messages({"model": "m", "messages": "bogus"}))


class ForcedToolChoiceTest(unittest.TestCase):
    def openai_tools(self, n: int) -> list:
        return [{"type": "function", "function": {"name": f"fn{i}", "parameters": {}}} for i in range(n)]

    def anthropic_tools(self, n: int) -> list:
        return [{"name": f"fn{i}", "description": "d", "input_schema": {}} for i in range(n)]

    def test_openai_single_tool_required_rewritten(self):
        body = {"tool_choice": "required", "tools": self.openai_tools(1)}
        info = cp.rewrite_forced_tool_choice(body, "openai")
        self.assertEqual(body["tool_choice"], {"type": "function", "function": {"name": "fn0"}})
        self.assertEqual(info, {"from": "required", "to": "fn0", "tools": 1})

    def test_anthropic_single_tool_any_rewritten(self):
        body = {"tool_choice": {"type": "any", "disable_parallel_tool_use": True}, "tools": self.anthropic_tools(1)}
        info = cp.rewrite_forced_tool_choice(body, "anthropic")
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "fn0", "disable_parallel_tool_use": True})
        self.assertEqual(info, {"from": "any", "to": "fn0", "tools": 1})

    def test_multi_tool_forced_rejected_stable_400(self):
        for proto, tc, tools in (
            ("openai", "required", self.openai_tools(2)),
            ("anthropic", {"type": "any"}, self.anthropic_tools(3)),
        ):
            body = {"tool_choice": tc, "tools": tools}
            with self.assertRaises(cp.CompatReject) as ctx:
                cp.rewrite_forced_tool_choice(body, proto)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "forced_tool_choice_unsupported")
            self.assertIn("multiple tools", ctx.exception.message)
            self.assertEqual(body["tool_choice"], tc)  # 拒绝时不改写

    def test_non_forced_passthrough(self):
        for tc in ("auto", "none", {"type": "function", "function": {"name": "fn0"}}):
            body = {"tool_choice": tc, "tools": self.openai_tools(2)}
            self.assertIsNone(cp.rewrite_forced_tool_choice(body, "openai"))
            self.assertEqual(body["tool_choice"], tc)
        for tc in ({"type": "auto"}, {"type": "tool", "name": "fn0"}, None):
            body = {"tool_choice": tc, "tools": self.anthropic_tools(2)}
            self.assertIsNone(cp.rewrite_forced_tool_choice(body, "anthropic"))
            self.assertEqual(body["tool_choice"], tc)

    def test_missing_or_malformed_tools_passthrough(self):
        self.assertIsNone(cp.rewrite_forced_tool_choice({"tool_choice": "required"}, "openai"))
        self.assertIsNone(cp.rewrite_forced_tool_choice({"tool_choice": "required", "tools": []}, "openai"))
        self.assertIsNone(cp.rewrite_forced_tool_choice(
            {"tool_choice": "required", "tools": [{"type": "function", "function": {}}]}, "openai"))


class OpenAIStreamFixerTest(unittest.TestCase):
    def test_fragments_then_stop_rewritten(self):
        fixer = cp.OpenAIStreamFixer()
        frag = b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"lookup_multiplier"}}]}}]}\n'
        self.assertEqual(fixer.process_line(frag), frag)  # fragment 行字节原样
        final = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n'
        out = fixer.process_line(final, endpoint="/v1/chat/completions")
        obj = json.loads(out.decode()[6:])
        self.assertEqual(obj["choices"][0]["finish_reason"], "tool_calls")
        self.assertTrue(out.endswith(b"\n"))
        self.assertEqual(fixer.fixed, [0])

    def test_no_fragments_stop_untouched(self):
        fixer = cp.OpenAIStreamFixer()
        final = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n'
        self.assertEqual(fixer.process_line(final), final)

    def test_done_and_non_data_lines_passthrough(self):
        fixer = cp.OpenAIStreamFixer()
        for line in (b": keep-alive\n", b"\n", b"data: [DONE]\n", b"event: ping\n"):
            self.assertEqual(fixer.process_line(line), line)
        frag_then_done = b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0}]}}]}\r\n'
        fixer.process_line(frag_then_done)
        stop = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\r\n'
        out = fixer.process_line(stop)
        self.assertTrue(out.endswith(b"\r\n"))  # 终结符保持
        self.assertEqual(json.loads(out.decode()[6:])["choices"][0]["finish_reason"], "tool_calls")

    def test_per_choice_index_tracking(self):
        fixer = cp.OpenAIStreamFixer()
        frag0 = b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0}]}},{"index":1,"delta":{"content":"hi"}}]}\n'
        fixer.process_line(frag0)
        final = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"},{"index":1,"delta":{},"finish_reason":"stop"}]}\n'
        obj = json.loads(fixer.process_line(final).decode()[6:])
        self.assertEqual(obj["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(obj["choices"][1]["finish_reason"], "stop")  # 无 fragments 的 choice 不改

    def test_invalid_json_and_empty_choices_passthrough(self):
        fixer = cp.OpenAIStreamFixer()
        bad = b"data: {not json}\n"
        self.assertEqual(fixer.process_line(bad), bad)
        usage = b'data: {"choices":[],"usage":{"total_tokens":9}}\n'
        self.assertEqual(fixer.process_line(usage), usage)

    def test_already_tool_calls_untouched(self):
        fixer = cp.OpenAIStreamFixer()
        line = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n'
        self.assertEqual(fixer.process_line(line), line)

    def test_async_stream_rewrites_across_chunk_boundaries(self):
        payload = (
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1"}]}}]}\n\n'
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        # 按奇数字节切块，验证跨 chunk 断行
        chunks = [payload[i:i + 7] for i in range(0, len(payload), 7)]

        async def collect() -> bytes:
            async def src():
                for c in chunks:
                    yield c
            out = b""
            async for piece in cp._openai_sse_rewritten(src(), "/v1/chat/completions"):
                out += piece
            return out

        result = asyncio.run(collect())
        self.assertEqual(result[:52], payload[:52])  # fragment 行原样
        obj = json.loads(result.decode().split("\n")[2][6:])
        self.assertEqual(obj["choices"][0]["finish_reason"], "tool_calls")
        self.assertTrue(result.endswith(b"data: [DONE]\n\n"))

    def test_async_passthrough_no_transform_stream(self):
        payload = b'data: {"choices":[{"index":0,"delta":{"content":"STREAM_OK"},"finish_reason":null}]}\n\ndata: [DONE]\n\n'
        chunks = [payload[i:i + 5] for i in range(0, len(payload), 5)]

        async def collect() -> bytes:
            async def src():
                for c in chunks:
                    yield c
            out = b""
            async for piece in cp._openai_sse_rewritten(src(), ""):
                out += piece
            return out

        self.assertEqual(asyncio.run(collect()), payload)


class DsmlArgumentsTest(unittest.TestCase):
    REAL_DSML = "\n\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name=\"lookup_multiplier\">\n<｜DSML｜parameter name=\"code\" string=\"true\">OMEGA</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜tool_calls>"

    def test_parse_real_dsml(self):
        self.assertEqual(cp.parse_dsml_arguments(self.REAL_DSML), [{"code": "OMEGA"}])

    def test_parse_multiple_params_and_non_string(self):
        text = '<｜DSML｜invoke name="f"><｜DSML｜parameter name="n" string="false">17</｜DSML｜parameter><｜DSML｜parameter name="s" string="true">x</｜DSML｜parameter></｜DSML｜invoke>'
        self.assertEqual(cp.parse_dsml_arguments(text), [{"n": 17, "s": "x"}])

    def test_non_dsml_returns_none(self):
        self.assertIsNone(cp.parse_dsml_arguments("plain text"))
        self.assertIsNone(cp.parse_dsml_arguments('{"code": "OMEGA"}'))

    def test_history_normalization(self):
        body = {"messages": [
            {"role": "user", "content": "call it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "lookup_multiplier", "arguments": self.REAL_DSML}},
                {"id": "c2", "type": "function", "function": {"name": "clean", "arguments": '{"a": 1}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "17"},
            {"role": "user", "content": "next"},
        ]}
        info = cp.normalize_dsml_history(body)
        self.assertEqual(info, {"calls": 1})
        self.assertEqual(json.loads(body["messages"][1]["tool_calls"][0]["function"]["arguments"]), {"code": "OMEGA"})
        self.assertEqual(body["messages"][1]["tool_calls"][1]["function"]["arguments"], '{"a": 1}')  # 合法 JSON 不动

    def test_history_no_change_returns_none(self):
        body = {"messages": [{"role": "assistant", "tool_calls": [
            {"function": {"name": "f", "arguments": '{"x": 1}'}}]}]}
        before = json.dumps(body, sort_keys=True)
        self.assertIsNone(cp.normalize_dsml_history(body))
        self.assertEqual(json.dumps(body, sort_keys=True), before)

    def test_response_normalization(self):
        value = {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "lookup_multiplier", "arguments": self.REAL_DSML}}]}}]}
        self.assertTrue(cp.normalize_dsml_response(value))
        self.assertEqual(json.loads(value["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]), {"code": "OMEGA"})
        clean = {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "f", "arguments": '{"x": 1}'}}]}}]}
        before = json.dumps(clean, sort_keys=True)
        self.assertFalse(cp.normalize_dsml_response(clean))
        self.assertEqual(json.dumps(clean, sort_keys=True), before)


class ErrorResponseTest(unittest.TestCase):
    def test_openai_shape(self):
        resp = cp.error_response(
            cp.CompatReject(400, "forced_tool_choice_unsupported", "boom"), "openai")
        self.assertEqual(resp.status_code, 400)
        body = json.loads(resp.body)
        self.assertEqual(body["error"]["code"], "forced_tool_choice_unsupported")
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["message"], "boom")

    def test_anthropic_shape(self):
        resp = cp.error_response(
            cp.CompatReject(400, "forced_tool_choice_unsupported", "boom"), "anthropic")
        body = json.loads(resp.body)
        self.assertEqual(body, {"type": "error", "error": {"type": "invalid_request_error", "message": "boom"}})

    def test_502_uses_api_error_type(self):
        resp = cp.error_response(cp.CompatReject(502, "upstream_unavailable", "x"), "openai")
        self.assertEqual(json.loads(resp.body)["error"]["type"], "api_error")


if __name__ == "__main__":
    unittest.main()
