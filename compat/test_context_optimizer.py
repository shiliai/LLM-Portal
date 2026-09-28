from __future__ import annotations

import hashlib

from context_optimizer import ContextOptimizer, OptimizerConfig


def _key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _body(tool_text: str) -> dict:
    return {
        "model": "fixture",
        "system": "system stays exact",
        "messages": [
            {"role": "user", "content": "user stays exact"},
            {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": tool_text},
        ],
    }


def test_default_off_is_a_noop():
    body = _body("x\n" * 10)
    result, info = ContextOptimizer(OptimizerConfig()).transform(body, "sk-test")
    assert result is body
    assert info["mode"] == "off"
    assert info["enabled"] is False


def test_key_allowlist_applies_safe_only_to_tool_result():
    body = _body("same\n" * 10)
    optimizer = ContextOptimizer(OptimizerConfig(mode="safe", key_hashes=frozenset({_key("sk-test")})))
    result, info = optimizer.transform(body, "sk-test")
    assert info["enabled"] is True
    assert info["bytes_saved"] > 0
    assert result["system"] == body["system"]
    assert result["messages"][0]["content"] == body["messages"][0]["content"]
    assert result["messages"][1]["tool_calls"] == body["messages"][1]["tool_calls"]
    assert "repeated x10" in result["messages"][2]["content"]


def test_bounded_preserves_anthropic_tool_result_structure():
    body = {
        "model": "fixture",
        "messages": [{"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "toolu-1",
            "content": [{"type": "text", "text": "z" * 20000}],
        }]}],
    }
    optimizer = ContextOptimizer(OptimizerConfig(mode="bounded", key_hashes=frozenset({"*"})))
    result, info = optimizer.transform(body, "sk-test")
    block = result["messages"][0]["content"][0]
    assert info["bytes_saved"] > 0
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu-1"
    assert block["content"][0]["type"] == "text"
    assert "tool output truncated" in block["content"][0]["text"]


def test_candidate_that_does_not_shrink_falls_back_to_original():
    body = _body("short")
    optimizer = ContextOptimizer(OptimizerConfig(mode="bounded", key_hashes=frozenset({"*"})))
    result, info = optimizer.transform(body, "sk-test")
    assert result is body
    assert info["fallback_never_worse"] is True
    assert info["bytes_saved"] == 0
