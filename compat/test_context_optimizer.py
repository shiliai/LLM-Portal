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


# ── 图片历史裁剪（issue #166，吸收 #98）────────────────────────────────────

def _image_part(marker: str = "img") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{marker}{'A' * 400}"}}


def _image_body(model: str, image_messages: int, per_message: int, last_user_images: int = 0) -> dict:
    messages: list = []
    for _ in range(image_messages):
        messages.append({"role": "user", "content": [{"type": "text", "text": "screenshot"}]
                         + [_image_part() for _ in range(per_message)]})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    messages.append({"role": "user", "content": [{"type": "text", "text": "latest question"}]
                     + [_image_part() for _ in range(last_user_images)]})
    return {"model": model, "messages": messages}


def _count_parts(body: dict, part_type: str) -> int:
    count = 0
    for message in body["messages"]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == part_type:
                    count += 1
                inner = part.get("content")
                if isinstance(inner, list):
                    count += sum(1 for sub in inner if isinstance(sub, dict) and sub.get("type") == part_type)
    return count


def test_image_guard_evicts_oldest_for_any_key_without_canary():
    body = _image_body("GLM-5.3-Flash-EXL3", image_messages=11, per_message=5, last_user_images=1)  # 56 张
    optimizer = ContextOptimizer(OptimizerConfig(image_limits={"GLM-5.3-Flash-EXL3": 50}))
    result, info = optimizer.transform(body, "sk-any-client")
    assert info["mode"] == "off"  # 未进白名单：文本 canary 关闭
    assert info["changed"] is True  # guard 档仍然生效
    evict = info["image_evict"]
    assert evict["active"] is True and evict["tier"] == "guard"
    assert evict["total"] == 56 and evict["pruned"] == 6 and evict["kept"] == 50
    assert evict["last_user_degraded"] is False
    assert _count_parts(result, "image_url") == 50
    placeholders = [p for m in result["messages"] for p in m["content"]
                    if isinstance(p, dict) and p.get("type") == "text"
                    and p["text"].startswith("[IMAGE_EVICTED: ")]
    assert len(placeholders) == 6
    assert placeholders[0]["text"] == "[IMAGE_EVICTED: 会话第 1 张截图已移除，仅保留最近 50 张]"
    # 最后一条 user 消息的图片原样保留
    assert _count_parts({"messages": [result["messages"][-1]]}, "image_url") == 1
    assert info["bytes_saved"] > 0


def test_image_canary_evicts_to_keep_last_for_allowlisted_key():
    body = _image_body("GLM-5.3-Flash-EXL3", image_messages=11, per_message=5, last_user_images=1)
    optimizer = ContextOptimizer(OptimizerConfig(mode="safe", key_hashes=frozenset({"*"}),
                                                 image_limits={"GLM-5.3-Flash-EXL3": 50},
                                                 image_keep_last=8))
    result, info = optimizer.transform(body, "sk-canary")
    evict = info["image_evict"]
    assert evict["tier"] == "canary" and evict["target"] == 8
    assert evict["pruned"] == 48 and evict["kept"] == 8
    assert _count_parts(result, "image_url") == 8
    # 淘汰的最旧 48 张，最后一条 user 的 1 张在保留集里
    assert _count_parts({"messages": [result["messages"][-1]]}, "image_url") == 1


def test_image_guard_off_and_no_canary_leaves_body_untouched():
    body = _image_body("GLM-5.3-Flash-EXL3", image_messages=11, per_message=5, last_user_images=1)
    optimizer = ContextOptimizer(OptimizerConfig(image_limits={"GLM-5.3-Flash-EXL3": 50}, image_guard=False))
    result, info = optimizer.transform(body, "sk-any")
    assert result is body
    assert info["image_evict"] == {"active": False}


def test_unconfigured_model_group_never_evicts():
    body = _image_body("some-text-model", image_messages=20, per_message=5, last_user_images=1)
    optimizer = ContextOptimizer(OptimizerConfig(image_limits={"GLM-5.3-Flash-EXL3": 50}))
    result, info = optimizer.transform(body, "sk-any")
    assert result is body
    assert info["image_evict"] == {"active": False}


def test_wildcard_limit_applies_to_unknown_group():
    body = _image_body("brand-new-vision", image_messages=3, per_message=3, last_user_images=0)  # 9 张
    optimizer = ContextOptimizer(OptimizerConfig(image_limits={"*": 5}))
    result, info = optimizer.transform(body, "sk-any")
    assert info["image_evict"]["pruned"] == 4
    assert _count_parts(result, "image_url") == 5


def test_last_user_message_images_survive_when_evictable_is_enough():
    # 7 张历史 + 最后一条 user 4 张 = 11 > 10；canary 目标 8：裁 3 张历史的，最后 4 张不动
    body = _image_body("m", image_messages=7, per_message=1, last_user_images=4)
    optimizer = ContextOptimizer(OptimizerConfig(mode="safe", key_hashes=frozenset({"*"}),
                                                 image_limits={"m": 10}, image_keep_last=8))
    result, info = optimizer.transform(body, "sk-canary")
    assert info["image_evict"]["last_user_degraded"] is False
    assert _count_parts({"messages": [result["messages"][-1]]}, "image_url") == 4
    assert _count_parts(result, "image_url") == 8


def test_last_user_alone_exceeds_limit_degrades_oldest_inside_it():
    # 历史 2 张 + 最后一条 user 12 张 = 14 > 10：守卫必须动最后一条 user，最旧优先
    body = _image_body("m", image_messages=2, per_message=1, last_user_images=12)
    optimizer = ContextOptimizer(OptimizerConfig(image_limits={"m": 10}))
    result, info = optimizer.transform(body, "sk-any")
    evict = info["image_evict"]
    assert evict["last_user_degraded"] is True
    assert _count_parts(result, "image_url") == 10
    last_parts = result["messages"][-1]["content"]
    kept_in_last = [p for p in last_parts if p.get("type") == "image_url"]
    assert len(kept_in_last) == 10  # 12 - 2
    assert sum(1 for p in last_parts if p.get("type") == "text" and p["text"].startswith("[IMAGE_EVICTED")) == 2


def test_placeholder_bytes_stable_across_rounds():
    # 客户端每轮重发全量历史：占位符跨轮字节一致、淘汰边界单调前移
    config = OptimizerConfig(image_limits={"g": 50})
    optimizer = ContextOptimizer(config)
    round1 = _image_body("g", image_messages=10, per_message=5, last_user_images=1)  # 51 张
    r1, i1 = optimizer.transform(round1, "sk-any")
    assert i1["image_evict"]["pruned"] == 1
    round2 = _image_body("g", image_messages=10, per_message=5, last_user_images=1)
    round2["messages"] = round2["messages"] + [
        {"role": "assistant", "content": [{"type": "text", "text": "edited"}]},
        {"role": "user", "content": [_image_part("new")]},
    ]  # 52 张
    r2, i2 = optimizer.transform(round2, "sk-any")
    assert i2["image_evict"]["pruned"] == 2
    p1 = [p["text"] for m in r1["messages"] for p in m["content"]
          if isinstance(p, dict) and p.get("type") == "text" and p["text"].startswith("[IMAGE_EVICTED")]
    p2 = [p["text"] for m in r2["messages"] for p in m["content"]
          if isinstance(p, dict) and p.get("type") == "text" and p["text"].startswith("[IMAGE_EVICTED")]
    assert p2[0] == p1[0]  # 同一张截图的占位符逐字节一致 → prefix cache 每轮只前移一格


def test_anthropic_nested_tool_result_images_counted_and_cache_control_kept():
    body = {
        "model": "glm-v",
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "browser state"},
                    {"type": "image", "source": {"type": "base64", "data": "A" * 400},
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "image", "source": {"type": "base64", "data": "B" * 400}},
                ]},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            {"role": "user", "content": [{"type": "text", "text": "continue"}, {"type": "image", "source": {"type": "base64", "data": "C" * 400}}]},
        ],
    }
    optimizer = ContextOptimizer(OptimizerConfig(image_limits={"glm-v": 2}))
    result, info = optimizer.transform(body, "sk-any")
    assert info["image_evict"]["total"] == 3 and info["image_evict"]["pruned"] == 1
    inner = result["messages"][0]["content"][0]["content"]
    assert inner[1]["type"] == "text" and inner[1]["text"].startswith("[IMAGE_EVICTED: 会话第 1 张")
    assert inner[1].get("cache_control") == {"type": "ephemeral"}  # 断点随占位符保留
    assert inner[2]["type"] == "image"  # 第二张保留
    assert result["messages"][2]["content"][1]["type"] == "image"  # 最后一条 user 的图不动


def test_openai_tool_message_images_counted():
    body = {
        "model": "dspark",
        "messages": [
            {"role": "user", "content": "look"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function"}]},
            {"role": "tool", "tool_call_id": "c1", "content": [
                {"type": "text", "text": "shot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
            {"role": "user", "content": [_image_part("q")]},
        ],
    }
    optimizer = ContextOptimizer(OptimizerConfig(image_limits={"dspark": 1}))
    result, info = optimizer.transform(body, "sk-any")
    assert info["image_evict"]["total"] == 2 and info["image_evict"]["pruned"] == 1
    tool_parts = result["messages"][2]["content"]
    assert tool_parts[1]["type"] == "text" and tool_parts[1]["text"].startswith("[IMAGE_EVICTED")
    assert result["messages"][3]["content"][0]["type"] == "image_url"


def test_short_url_images_rescue_survives_text_size_fallback():
    # PR #167 评审 P2-1：短 URL 图 part 比占位符小 → 候选总字节变大 → 文本
    # never-worse 回退，但图片限额救援必须保留，指标与实发请求一致。
    messages = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"https://example.com/{i}.png"}}
            for i in range(51)]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    body = {"model": "g", "messages": messages}
    optimizer = ContextOptimizer(OptimizerConfig(mode="safe", key_hashes=frozenset({"*"}),
                                                 image_limits={"g": 50}, image_keep_last=8))
    result, info = optimizer.transform(body, "sk-canary")
    assert info["fallback_never_worse"] is True  # 文本无节省，回退
    assert info["changed"] is True               # 但图片救援生效
    assert info["image_evict"]["pruned"] == 43
    assert _count_parts(result, "image_url") == 8  # 实发请求确实只剩 8 张
    placeholders = [p for m in result["messages"] for p in m["content"]
                    if isinstance(p, dict) and p.get("type") == "text"
                    and p["text"].startswith("[IMAGE_EVICTED: ")]
    assert len(placeholders) == 43


def test_placeholder_stable_when_kept_varies_across_rounds():
    # PR #167 评审 P2-2：canary 保护规则使 kept 跨轮浮动（9→8），占位符不写
    # 本轮 kept（改用配置目标），历史占位符逐字节稳定，prefix cache 不塌。
    optimizer = ContextOptimizer(OptimizerConfig(mode="safe", key_hashes=frozenset({"*"}),
                                                 image_limits={"g": 50}, image_keep_last=8))

    def history(images: int) -> list:
        msgs = []
        for _ in range(images):
            msgs.append({"role": "user", "content": [_image_part()]})
            msgs.append({"role": "assistant", "content": "ok"})
        return msgs

    def placeholders(result: dict) -> list:
        return [p["text"] for m in result["messages"] for p in m["content"]
                if isinstance(p, dict) and p.get("type") == "text"
                and p["text"].startswith("[IMAGE_EVICTED: ")]

    round1 = {"model": "g", "messages": history(43) + [
        {"role": "user", "content": [_image_part() for _ in range(9)]}]}
    r1, i1 = optimizer.transform(round1, "k")
    assert i1["image_evict"]["kept"] == 9  # 最后 user 的 9 张受保护，裁不到 8
    assert "仅保留最近 8 张" in placeholders(r1)[0]  # 占位符用配置目标而非 kept

    round2 = {"model": "g", "messages": history(43) + [
        {"role": "user", "content": [_image_part() for _ in range(9)]},   # 上一轮的最后 user，已成历史
        {"role": "assistant", "content": "edited"},
        {"role": "user", "content": [_image_part()]},                     # 本轮新截图
    ]}
    r2, i2 = optimizer.transform(round2, "k")
    assert i2["image_evict"]["kept"] == 8
    p1, p2 = placeholders(r1), placeholders(r2)
    assert len(p1) == 43 and len(p2) == 45
    assert p2[:43] == p1  # 同一批历史截图的占位符跨轮逐字节一致
