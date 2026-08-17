#!/usr/bin/env python3
"""group_routing effort 归一化单测（本地无 litellm 包：注入桩模块后加载被测文件）。"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace


def _hook():
    """注入假 litellm 后 import group_routing（容器内挂载路径 /app/proxy 之外的本仓形态）。"""
    litellm = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")


    class CustomLogger:
        pass


    custom_logger.CustomLogger = CustomLogger
    litellm.integrations = integrations
    sys.modules.setdefault("litellm", litellm)
    sys.modules.setdefault("litellm.integrations", integrations)
    sys.modules.setdefault("litellm.integrations.custom_logger", custom_logger)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "group_routing_under_test", Path(__file__).parent / "group_routing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- _effort 归一化

def test_effort_reasoning_effort_str():
    assert _hook()._effort({"reasoning_effort": "high"}) == "high"


def test_effort_reasoning_dict_effort():
    assert _hook()._effort({"reasoning": {"effort": "medium"}}) == "medium"


def test_effort_thinking_budget_tokens():
    assert _hook()._effort({"thinking": {"type": "enabled", "budget_tokens": 8192}}) == "budget:8192"


def test_effort_thinking_disabled():
    assert _hook()._effort({"thinking": {"type": "disabled"}}) == "off"


def test_effort_absent_or_malformed():
    m = _hook()
    assert m._effort({}) == ""
    assert m._effort({"reasoning_effort": ""}) == ""
    assert m._effort({"reasoning": {"effort": ""}}) == ""
    assert m._effort({"thinking": {}}) == ""
    assert m._effort({"reasoning_effort": None}) == ""


def test_effort_priority_reasoning_effort_first():
    m = _hook()
    assert m._effort({"reasoning_effort": "low", "thinking": {"budget_tokens": 1024}}) == "low"


def test_effort_object_form_data():
    """非 dict data（部分入口传对象）同样可提取。"""
    assert _hook()._effort(SimpleNamespace(reasoning_effort="high")) == "high"


# ---------------------------------------------------------------- _apply 落 metadata

def test_apply_injects_tags_and_effort():
    m = _hook()
    data = {"metadata": {"tags": ["forged"]}, "reasoning_effort": "high"}
    m._apply("lab", data)
    assert data["metadata"]["tags"] == ["lab"]
    assert data["metadata"]["spend_logs_metadata"] == {"effort": "high"}
    assert data["metadata"]["requester_metadata"] == {"effort": "high"}
    assert data["enable_tag_filtering"] is True


def test_apply_effort_merges_into_existing_slots():
    """客户端已带 spend_logs_metadata / requester_metadata 时只增不改。"""
    m = _hook()
    data = {"metadata": {"spend_logs_metadata": {"k": "v"}, "requester_metadata": {"hello": "world"}},
            "thinking": {"type": "enabled", "budget_tokens": 4096}}
    m._apply("", data)
    assert data["metadata"]["spend_logs_metadata"] == {"k": "v", "effort": "budget:4096"}
    assert data["metadata"]["requester_metadata"] == {"hello": "world", "effort": "budget:4096"}


def test_apply_mutates_metadata_in_place():
    """function_setup 在钩子前把 data["metadata"] 同引用存进 Logging——整体替换会丢改动。"""
    m = _hook()
    md = {"user_api_key_hash": "abc"}
    data = {"metadata": md, "reasoning_effort": "high"}
    m._apply("lab", data)
    assert data["metadata"] is md
    assert md["spend_logs_metadata"] == {"effort": "high"}


def test_apply_writes_litellm_metadata_channel_too():
    """/v1/messages 入口的代理 metadata 在 litellm_metadata，同通道需写入。"""
    m = _hook()
    data = {"metadata": None, "litellm_metadata": {"user_api_key_hash": "abc"},
            "reasoning_effort": "low"}
    m._apply("", data)
    assert data["litellm_metadata"]["spend_logs_metadata"] == {"effort": "low"}
    assert data["metadata"]["spend_logs_metadata"] == {"effort": "low"}  # 双通道都写


def test_apply_no_effort_leaves_key_out():
    m = _hook()
    data = {"metadata": {}, "messages": []}
    m._apply("", data)
    assert "spend_logs_metadata" not in data["metadata"]
    assert "requester_metadata" not in data["metadata"]
    assert "tags" not in data["metadata"]  # 未绑组清空 tags 的既有语义不变


def test_apply_object_form_sets_metadata_attr():
    m = _hook()
    data = SimpleNamespace(metadata=None, thinking={"type": "enabled", "budget_tokens": 1024})
    m._apply("home", data)
    assert data.metadata["tags"] == ["home"]
    assert data.metadata["spend_logs_metadata"] == {"effort": "budget:1024"}
