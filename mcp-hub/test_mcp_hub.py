#!/usr/bin/env python3
"""mcp_hub.py 鉴权单测（issue #10，无网络/真实 LiteLLM）。

覆盖：Bearer 虚拟 Key 鉴权路径——/mcp/usage、/mcp/upload 无/错 Bearer → 401；
正确 Key 经桩 LiteLLM /key/info 返回 200（且确实打到该端点）才放行；被禁用
Key（is_disabled）拒绝；LiteLLMTokenVerifier.verify_token 的放行/拒绝与
client_id（Key 哈希前 16 位）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from testutil import install_litellm_stub, load_service

MCP_HUB_DIR = Path(__file__).parent
VALID_KEY = "testkey-valid-0001"
DISABLED_KEY = "testkey-disabled-02"
BAD_KEY = "testkey-bad-0003"


def _handler(method, path, bearer, json_body):
    """桩 LiteLLM /key/info：VALID_KEY 正常、DISABLED_KEY 被禁、其余 401。"""
    if path != "/key/info":
        return 404, {"error": f"unexpected stub path {path}"}
    if bearer == VALID_KEY:
        return 200, {"key_info": {"key_alias": "unit-key",
                                  "metadata": {"group": "default"}, "is_disabled": False}}
    if bearer == DISABLED_KEY:
        return 200, {"key_info": {"key_alias": "disabled-key", "is_disabled": True}}
    return 401, {"error": "invalid key"}


@pytest.fixture
def hub(monkeypatch, tmp_path):
    install_litellm_stub(monkeypatch, _handler)
    return load_service(MCP_HUB_DIR / "mcp_hub.py", {
        "MCP_HUB_DATA": str(tmp_path / "mdata"),
        "LITELLM_BASE": "http://litellm-stub.invalid",
        "EXTERNAL_MCP_CONF": str(tmp_path / "external-mcp.json"),   # 不存在 → 不挂外部工具
    })


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------- Bearer 门禁

def test_usage_without_bearer_401(hub):
    with TestClient(hub.app) as client:
        resp = client.get("/mcp/usage")
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid or missing key"


def test_usage_with_wrong_bearer_401(hub):
    with TestClient(hub.app) as client:
        resp = client.get("/mcp/usage", headers=_bearer(BAD_KEY))
    assert resp.status_code == 401


def test_usage_with_valid_bearer_200(hub):
    with TestClient(hub.app) as client:
        resp = client.get("/mcp/usage", headers=_bearer(VALID_KEY))
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == f"sk-...{VALID_KEY[-4:]}"   # 回显只露尾 4 位
    assert body["tools"] == {}
    assert body["total"] == 0


def test_valid_bearer_actually_hits_key_info(hub, monkeypatch):
    """放行依据是桩 LiteLLM /key/info 200（回环校验），不是本地白名单。"""
    calls = install_litellm_stub(monkeypatch, _handler)   # 重装以拿本次调用记录
    with TestClient(hub.app) as client:
        assert client.get("/mcp/usage", headers=_bearer(VALID_KEY)).status_code == 200
    assert calls and {c["path"] for c in calls} == {"/key/info"}
    assert {c["method"] for c in calls} == {"GET"}
    assert {c["bearer"] for c in calls} == {VALID_KEY}


def test_disabled_key_rejected(hub):
    """/key/info 明确 is_disabled → 视同无效。"""
    with TestClient(hub.app) as client:
        resp = client.get("/mcp/usage", headers=_bearer(DISABLED_KEY))
    assert resp.status_code == 401


def test_upload_requires_bearer_key(hub):
    with TestClient(hub.app) as client:
        no_auth = client.post("/mcp/upload")
        bad = client.post("/mcp/upload", headers=_bearer(BAD_KEY))
        ok = client.post("/mcp/upload", headers=_bearer(VALID_KEY))  # 缺 multipart 字段 → 400（已过鉴权）
    assert no_auth.status_code == 401
    assert bad.status_code == 401
    assert ok.status_code == 400
    assert "multipart field" in ok.json()["error"]


def test_fetch_uploaded_image_returns_mime_type(hub):
    image = b"fake-png-content"
    (hub.UPLOAD_DIR / "test-token.png").write_bytes(image)

    data, mime = asyncio.run(hub.fetch_image_data("/mcp/files/test-token.png"))

    assert data == image
    assert mime == "image/png"


# ---------------------------------------------------------------- MCP 传输层 TokenVerifier

def test_token_verifier_accepts_valid_key(hub):
    token = asyncio.run(hub.LiteLLMTokenVerifier().verify_token(VALID_KEY))
    assert token is not None
    assert token.token == VALID_KEY
    assert token.client_id == hashlib.sha256(VALID_KEY.encode()).hexdigest()[:16]
    assert token.scopes == ["default"]


def test_token_verifier_rejects_bad_and_empty(hub):
    verifier = hub.LiteLLMTokenVerifier()
    assert asyncio.run(verifier.verify_token(BAD_KEY)) is None
    assert asyncio.run(verifier.verify_token("")) is None
    assert asyncio.run(verifier.verify_token(DISABLED_KEY)) is None


def test_key_group_reassignment_is_not_cached(hub, monkeypatch):
    current = {"group": "home"}
    calls = install_litellm_stub(monkeypatch, lambda method, path, bearer, body: (
        200, {"key_info": {"metadata": {"group": current["group"]}, "is_disabled": False}}))
    verifier = hub.LiteLLMTokenVerifier()
    first = asyncio.run(verifier.verify_token(VALID_KEY))
    current["group"] = "work"
    second = asyncio.run(verifier.verify_token(VALID_KEY))
    assert first.scopes == ["home"]
    assert second.scopes == ["work"]
    assert len(calls) == 2
    assert hub.group_access(_auth_ctx(hub, ["home"], second.scopes)) is False


def test_usage_counts_recorded_per_key_hash(hub):
    hub.record_usage(VALID_KEY, "analyze_image")
    hub.record_usage(VALID_KEY, "upload")
    with TestClient(hub.app) as client:
        resp = client.get("/mcp/usage", headers=_bearer(VALID_KEY))
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"] == {"analyze_image": 1, "upload": 1}
    assert body["total"] == 2


# ---------------------------------------------------------------- Group 工具授权（issue #51）

def _auth_ctx(hub, tags, scopes=("default",)):
    token = hub.AccessToken(token="unit", client_id="unit", scopes=list(scopes))
    return hub.AuthContext(token=token, component=SimpleNamespace(tags=set(tags)))


def test_group_access_matrix(hub):
    assert hub.group_access(_auth_ctx(hub, [])) is True          # 无 tags = 全局
    assert hub.group_access(_auth_ctx(hub, ["home"], ["home"])) is True
    assert hub.group_access(_auth_ctx(hub, ["home", "lab"], ["lab"])) is True
    assert hub.group_access(_auth_ctx(hub, ["home"], ["work"])) is False
    assert hub.group_access(hub.AuthContext(
        token=None, component=SimpleNamespace(tags={"home"}))) is False


def test_auth_middleware_is_installed(hub):
    assert any(isinstance(item, hub.AuthMiddleware) for item in hub.mcp.middleware)


def test_internal_config_state_binds_loaded_registry(hub):
    expected = hashlib.sha256(b"").hexdigest()
    with TestClient(hub.app) as client:
        resp = client.get("/internal/config-state")
    assert resp.status_code == 200
    assert resp.json() == {"sha256": expected}


def test_external_tools_inherit_configured_groups(hub, monkeypatch):
    hub.EXTERNAL_MCP_CONF.write_text(json.dumps([{
        "name": "svc", "url": "https://mcp.invalid/mcp", "api_key": "secret",
        "prefix": "svc_", "groups": ["home", "lab"],
    }]))

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def list_tools(self):
            return [SimpleNamespace(name="ping", description="Ping", inputSchema=None)]

    monkeypatch.setattr(hub, "Client", FakeClient)
    asyncio.run(hub.register_external_tools())
    tool = asyncio.run(hub.mcp.get_tool("svc_ping"))
    assert tool is not None
    assert tool.tags == {"home", "lab"}


def test_external_tool_with_malformed_groups_is_not_registered(hub, monkeypatch):
    hub.EXTERNAL_MCP_CONF.write_text(json.dumps([{
        "name": "svc", "url": "https://mcp.invalid/mcp", "api_key": "secret",
        "prefix": "svc_", "groups": "home",
    }]))

    class UnexpectedClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("malformed authorization config must fail before connecting")

    monkeypatch.setattr(hub, "Client", UnexpectedClient)
    asyncio.run(hub.register_external_tools())
    assert asyncio.run(hub.mcp.get_tool("svc_ping")) is None


@pytest.mark.parametrize("groups", [None, False, 0, "", {}, [""], [0]])
def test_external_tool_with_any_present_malformed_groups_is_not_registered(
        hub, monkeypatch, groups):
    hub.EXTERNAL_MCP_CONF.write_text(json.dumps([{
        "name": "svc", "url": "https://mcp.invalid/mcp", "api_key": "secret",
        "prefix": "svc_", "groups": groups,
    }]))

    class UnexpectedClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("malformed authorization config must fail before connecting")

    monkeypatch.setattr(hub, "Client", UnexpectedClient)
    asyncio.run(hub.register_external_tools())
    assert asyncio.run(hub.mcp.get_tool("svc_ping")) is None
