from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conversation_monitor import ConversationMonitor, key_identity


def run(coro):
    return asyncio.run(coro)


def test_policy_defaults_off_and_key_allowlist(tmp_path: Path):
    monitor = ConversationMonitor(tmp_path / "monitor.db")
    assert monitor.policy()["mode"] == "off"
    credential = "sk-unit-1234"
    key_hash, key_ref = key_identity(credential)
    assert monitor.decision(credential)["mode"] == "off"
    run(monitor.update_policy(mode="persist", keys=[key_hash], ttl_days=7, capacity=10_000))
    assert monitor.decision(credential) == {"mode": "persist", "key_hash": key_hash, "key_ref": key_ref}
    assert monitor.decision("sk-other-9999")["mode"] == "off"


def test_policy_update_is_visible_to_another_runtime_instance(tmp_path: Path):
    db = tmp_path / "shared.db"
    console_monitor = ConversationMonitor(db)
    compat_monitor = ConversationMonitor(db)
    key_hash, _ = key_identity("sk-unit-1234")
    assert compat_monitor.decision("sk-unit-1234")["mode"] == "off"
    run(console_monitor.update_policy(mode="stream", keys=[key_hash], ttl_days=14, capacity=50_000))
    assert compat_monitor.decision("sk-unit-1234")["mode"] == "stream"
    assert compat_monitor.policy()["mode"] == "stream"


def test_persist_detail_and_pagination_hides_payload_from_list(tmp_path: Path):
    monitor = ConversationMonitor(tmp_path / "monitor.db")
    key_hash, _ = key_identity("sk-unit-1234")
    run(monitor.update_policy(mode="persist", keys=[key_hash], ttl_days=14, capacity=50_000))
    for index in range(2):
        run(monitor.capture(request_id=f"req_{index}", request_raw=b'{"messages":[{"role":"user","content":"secret"}]}',
                            response_raw=b'{"choices":[],"usage":{"total_tokens":2}}',
                            headers={}, path="/v1/chat/completions", status_code=200,
                            model="model-alpha", protocol="openai-chat", started=0,
                            credential="sk-unit-1234", response_content_type="application/json"))
    result = run(monitor.list_records(limit=1))
    assert len(result["records"]) == 1
    assert "payload" not in result["records"][0]
    assert result["has_more"] is True
    detail = run(monitor.detail(result["records"][0]["request_id"]))
    assert detail["payload"]["request"]["messages"][0]["content"] == "secret"
    assert detail["payload"]["usage"]["total_tokens"] == 2
    assert detail["payload"]["retention_ttl_days"] == 14


def test_detail_can_resolve_upstream_response_id(tmp_path: Path):
    monitor = ConversationMonitor(tmp_path / "monitor.db")
    key_hash, _ = key_identity("sk-unit-1234")
    run(monitor.update_policy(mode="persist", keys=[key_hash], ttl_days=14, capacity=50_000))
    run(monitor.capture(request_id="req_internal", request_raw=b"{}", response_raw=b'{"id":"chatcmpl_unit","choices":[]}',
                        headers={}, path="/v1/chat/completions", status_code=200, model="m",
                        protocol="openai-chat", started=0, credential="sk-unit-1234"))
    detail = run(monitor.detail("chatcmpl_unit"))
    assert detail["request_id"] == "req_internal"
    assert detail["payload"]["upstream_request_id"] == "chatcmpl_unit"


def test_static_console_and_edge_contracts_wire_detail_and_sse():
    usage = (Path(__file__).parent / "console/static/usage.html").read_text()
    assert "conversation-monitor/records/" in usage
    assert "ug-detail-drawer" in usage
    assert "detailButton" in usage
    for name in ("private-llm.conf", "private-llm-offload.conf"):
        config = (Path(__file__).parent / "vps/nginx" / name).read_text()
        assert "location = /api/v1/conversation-monitor/stream" in config
        assert "proxy_buffering off;" in config
        assert "proxy_read_timeout 3600s;" in config


def test_sse_replays_from_last_event_id(tmp_path: Path):
    monitor = ConversationMonitor(tmp_path / "monitor.db")
    key_hash, _ = key_identity("sk-unit-1234")
    run(monitor.update_policy(mode="stream", keys=[key_hash], ttl_days=14, capacity=50_000))
    run(monitor.capture(request_id="req_1", request_raw=b"{}", response_raw=b"{}", headers={},
                        path="/v1/chat/completions", status_code=200, model="m", protocol="openai-chat",
                        started=0, credential="sk-unit-1234"))
    first = monitor._replay[-1]["id"]
    run(monitor.capture(request_id="req_2", request_raw=b"{}", response_raw=b"{}", headers={},
                        path="/v1/chat/completions", status_code=200, model="m", protocol="openai-chat",
                        started=0, credential="sk-unit-1234"))

    async def first_item():
        stream = monitor.sse(first)
        return await stream.__anext__()

    item = run(first_item()).decode()
    assert "conversation.capture" in item
    assert "req_2" in item


def test_capture_failure_does_not_raise(tmp_path: Path):
    monitor = ConversationMonitor(tmp_path / "monitor.db")
    monitor.db_path = tmp_path / "missing" / "nested" / "monitor.db"
    # malformed response and odd header values remain a best-effort capture.
    run(monitor.capture(request_id="req", request_raw=b"\xff", response_raw=b"\xff", headers=None,
                        path="/v1/chat/completions", status_code=502, model="m", protocol="openai-chat",
                        started=0, credential="sk-unit"))
    assert monitor.summary is not None
