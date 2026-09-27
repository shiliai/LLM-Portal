from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from conversation_monitor import ConversationMonitor, key_identity
from testutil import install_litellm_stub, load_service


MASTER = "sk-master-monitor"
XRW = {"X-Requested-With": "XMLHttpRequest"}


def handler(method, path, bearer, body):
    if path == "/global/spend":
        return 200, {}
    if path == "/key/list":
        return 200, {"keys": []}
    return 404, {"error": "not found"}


def load_console(tmp_path: Path):
    return load_service(Path(__file__).parent / "console.py", {
        "CONSOLE_DATA": str(tmp_path / "console"),
        "CONVERSATION_MONITOR_DB": str(tmp_path / "monitor.db"),
        "LITELLM_MASTER_KEY": MASTER,
        "ONBOARD_ADMIN_TOKEN": "onboard",
        "LITELLM_BASE": "http://litellm.invalid",
        "MCP_VISION_CONF": str(tmp_path / "vision.json"),
        "MCP_VISION_MODEL": "",
        "SESSION_COOKIE_SECURE": "false",
    })


def test_admin_policy_records_and_detail_are_protected(tmp_path, monkeypatch):
    install_litellm_stub(monkeypatch, handler)
    mod = load_console(tmp_path)
    mod.MONITOR = ConversationMonitor(tmp_path / "monitor.db")
    key_hash, _ = key_identity("sk-unit-1234")
    with TestClient(mod.app) as client:
        assert client.get("/console/api/conversation-monitor/policy").status_code == 401
        assert client.post("/console/api/login", json={"key": MASTER}, headers=XRW).status_code == 200
        policy = client.put("/console/api/conversation-monitor/policy",
                            json={"mode": "persist", "keys": [key_hash], "ttl_days": 7, "capacity": 10_000},
                            headers=XRW)
        assert policy.status_code == 200
        import asyncio
        asyncio.run(mod.MONITOR.capture(request_id="req-api", request_raw=b"{}", response_raw=b'{"ok":true}',
                                        headers={}, path="/v1/chat/completions", status_code=200,
                                        model="m", protocol="openai-chat", started=0,
                                        credential="sk-unit-1234"))
        records = client.get("/console/api/conversation-monitor/records?limit=10")
        assert records.status_code == 200
        assert records.json()["records"][0]["request_id"] == "req-api"
        assert "payload" not in records.json()["records"][0]
        detail = client.get("/console/api/conversation-monitor/records/req-api")
        assert detail.status_code == 200
        assert detail.json()["payload"]["response"]["ok"] is True
