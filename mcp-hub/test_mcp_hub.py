#!/usr/bin/env python3
"""mcp_hub.py 单测（issue #10 鉴权 + issue #71 内嵌图片，无网络/真实 LiteLLM）。

鉴权部分覆盖：Bearer 虚拟 Key 鉴权路径——/mcp/usage、/mcp/upload 无/错 Bearer → 401；
正确 Key 经桩 LiteLLM /key/info 返回 200（且确实打到该端点）才放行；被禁用
Key（is_disabled）拒绝；LiteLLMTokenVerifier.verify_token 的放行/拒绝与
client_id（Key 哈希前 16 位）。

issue #71 部分覆盖：analyze_image 的 image_base64/mime_type 内嵌输入与 upload_image
工具——输入选择、严格 Base64 解码、MIME 白名单与字节签名一致性、10MB 上限、
临时文件（随机 token + 后缀 + 30min TTL 语义）、向 LiteLLM 转发的请求体
（字节与客户端输入一致、Key 归属、模型名），以及 tools/list 可发现性与
image_url 旧用法兼容。
"""
from __future__ import annotations

import asyncio
import base64
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


def _vision_handler(method, path, bearer, json_body):
    """在 _handler 基础上补 /v1/chat/completions：返回固定识别结果。"""
    if path == "/v1/chat/completions":
        return 200, {"choices": [{"message": {"content": "stub-vision-answer"}}]}
    return _handler(method, path, bearer, json_body)


@pytest.fixture
def hub(monkeypatch, tmp_path):
    install_litellm_stub(monkeypatch, _handler)
    return load_service(MCP_HUB_DIR / "mcp_hub.py", {
        "MCP_HUB_DATA": str(tmp_path / "mdata"),
        "LITELLM_BASE": "http://litellm-stub.invalid",
        "MCP_VISION_CONF": str(tmp_path / "vision-mcp.json"),
        "MCP_VISION_MODEL": "fallback-vision-model",
        "EXTERNAL_MCP_CONF": str(tmp_path / "external-mcp.json"),   # 不存在 → 不挂外部工具
    })


@pytest.fixture
def vhub(monkeypatch, tmp_path):
    """同 hub，但桩 LiteLLM 额外应答 /v1/chat/completions（issue #71 视觉链路测试用）。"""
    install_litellm_stub(monkeypatch, _vision_handler)
    return load_service(MCP_HUB_DIR / "mcp_hub.py", {
        "MCP_HUB_DATA": str(tmp_path / "mdata"),
        "LITELLM_BASE": "http://litellm-stub.invalid",
        "MCP_VISION_CONF": str(tmp_path / "vision-mcp.json"),
        "MCP_VISION_MODEL": "fallback-vision-model",
        "EXTERNAL_MCP_CONF": str(tmp_path / "external-mcp.json"),
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


def test_vision_model_prefers_persisted_selection(hub):
    hub.VISION_CONF.write_text(json.dumps({"model": "selected-vision-model"}))
    assert hub.vision_model() == "selected-vision-model"


def test_vision_model_uses_environment_migration_fallback(hub):
    assert hub.vision_model() == "fallback-vision-model"
    hub.VISION_CONF.write_text("not-json")
    assert hub.vision_model() == "fallback-vision-model"


def test_vision_model_requires_configuration(hub):
    hub.VISION_MODEL_FALLBACK = ""
    with pytest.raises(hub.ToolError, match="not configured"):
        hub.vision_model()


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
    assert resp.json() == {"sha256": expected, "ok": True,
                           "tools": {"analyze_image": "builtin", "upload_image": "builtin"}, "entries": {}}


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


def test_config_state_attests_actual_restarted_surface_and_owner(hub, monkeypatch):
    hub.EXTERNAL_MCP_CONF.write_text(json.dumps([{
        "name": "svc", "url": "https://mcp.invalid/mcp", "api_key": "",
        "prefix": "svc_", "groups": [],
    }]))

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def list_tools(self):
            return [SimpleNamespace(name="ping", description="Ping", inputSchema={})]

    monkeypatch.setattr(hub, "Client", FakeClient)
    with TestClient(hub.app) as client:
        state = client.get("/internal/config-state").json()
        tool = asyncio.run(hub.mcp.get_tool("svc_ping"))
    assert state["ok"] is True
    assert state["tools"] == {"analyze_image": "builtin", "upload_image": "builtin", "svc_ping": "svc"}
    assert state["entries"]["svc"] == {"ok": True, "reason": "", "tools": ["svc_ping"]}
    assert tool is not None and tool.name == "svc_ping"


def test_config_state_rejects_builtin_and_inter_entry_collisions(hub, monkeypatch):
    hub.EXTERNAL_MCP_CONF.write_text(json.dumps([
        {"name": "builtin", "url": "https://a.invalid/mcp", "api_key": "", "prefix": "analyze_", "groups": []},
        {"name": "first", "url": "https://b.invalid/mcp", "api_key": "", "prefix": "svc_", "groups": []},
        {"name": "second", "url": "https://c.invalid/mcp", "api_key": "", "prefix": "svc_", "groups": []},
    ]))

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.url = args[0]
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def list_tools(self):
            return [SimpleNamespace(name="image" if self.url.endswith("a.invalid/mcp") else "ping",
                                    description="", inputSchema={})]

    monkeypatch.setattr(hub, "Client", FakeClient)
    with TestClient(hub.app) as client:
        state = client.get("/internal/config-state").json()
    assert state["ok"] is False
    assert state["entries"]["builtin"]["reason"] == "tool_collision"
    assert state["entries"]["second"]["reason"] == "tool_collision"
    assert state["tools"] == {"analyze_image": "builtin", "upload_image": "builtin", "svc_ping": "first"}


def test_config_state_marks_partial_surface_ownership_mismatch(hub, monkeypatch):
    hub.EXTERNAL_MCP_CONF.write_text(json.dumps([{
        "name": "svc", "url": "https://mcp.invalid/mcp", "api_key": "", "prefix": "svc_", "groups": [],
    }]))

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def list_tools(self):
            return [SimpleNamespace(name="ping", description="", inputSchema={})]

    original_get_tool = hub.mcp.get_tool
    async def missing_one(name):
        if name == "svc_ping":
            return None
        return await original_get_tool(name)

    monkeypatch.setattr(hub, "Client", FakeClient)
    monkeypatch.setattr(hub.mcp, "get_tool", missing_one)
    with TestClient(hub.app) as client:
        state = client.get("/internal/config-state").json()
    assert state["ok"] is False
    assert state["entries"]["svc"] == {"ok": False, "reason": "surface_mismatch", "tools": []}


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


# ---------------------------------------------------------------- 内嵌图片（issue #71）


def _png() -> bytes:
    """程序化生成 1x1 红色 PNG（真实签名与结构，不依赖外部资源）。"""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


_GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00" \
       b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00" + b"\x00" * 40 + b"\xff\xd9"
_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00" + b"\x00" * 24


def _mcp_headers(session: str | None = None) -> dict:
    headers = {**_bearer(VALID_KEY), "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    return headers


def _rpc_text(resp):
    """从 JSON 或 SSE 响应体取 JSON-RPC payload。"""
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    lines = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
    assert lines, f"no rpc payload in response: {resp.status_code} {resp.text[:200]}"
    return json.loads(lines[-1])


def _mcp_session(client) -> str:
    """Streamable HTTP 握手：initialize → notifications/initialized，返回 session id。"""
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                     "params": {"protocolVersion": "2025-03-26",
                                                "capabilities": {},
                                                "clientInfo": {"name": "pytest", "version": "0"}}},
                       headers=_mcp_headers())
    assert resp.status_code == 200, resp.text
    session = resp.headers.get("mcp-session-id")
    assert session, resp.text
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=_mcp_headers(session))
    return session


def _tools_call(client, session, name, arguments):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}},
                       headers=_mcp_headers(session))
    payload = _rpc_text(resp)
    result = payload.get("result", {})
    text = "".join(c.get("text", "") for c in result.get("content", []))
    return result, text


def _tool_error(client, session, name, arguments) -> str:
    result, text = _tools_call(client, session, name, arguments)
    assert result.get("isError") is True, f"expected tool error, got: {text[:200]}"
    return text


def _as_caller(vhub, monkeypatch, key=VALID_KEY):
    """直调工具函数时伪造 HTTP 请求上下文（current_key 经 contextvar 取 Bearer）。"""
    monkeypatch.setattr(vhub, "get_http_request",
                        lambda: SimpleNamespace(headers={"authorization": f"Bearer {key}"}))


def test_tools_list_discovers_inline_image_capability(vhub):
    """验收：tools/list 能发现本地上传能力（upload_image + analyze_image 可选输入）。"""
    with TestClient(vhub.app) as client:
        session = _mcp_session(client)
        payload = _rpc_text(client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                                      "method": "tools/list"},
                                        headers=_mcp_headers(session)))
    schema = {t["name"]: t["inputSchema"] for t in payload["result"]["tools"]}
    assert {"analyze_image", "upload_image"} <= set(schema)
    assert "question" in schema["analyze_image"].get("required", [])
    assert not {"image_url", "image_base64", "mime_type"} & set(schema["analyze_image"].get("required", []))
    assert set(schema["analyze_image"]["properties"]) >= {"question", "image_url", "image_base64", "mime_type"}
    assert set(schema["upload_image"]["properties"]) >= {"image_base64", "mime_type"}


def test_analyze_image_inline_base64_roundtrip(vhub, monkeypatch):
    """验收：Base64 调用 analyze_image，模型收到的字节与客户端输入一致，Key 记调用者账上。"""
    calls = install_litellm_stub(monkeypatch, _vision_handler)   # 重装以拿本次调用记录
    with TestClient(vhub.app) as client:
        session = _mcp_session(client)
        result, text = _tools_call(client, session, "analyze_image", {
            "question": "图里是什么颜色",
            "image_base64": base64.b64encode(_png()).decode(),
            "mime_type": "image/png"})
        usage = client.get("/mcp/usage", headers=_bearer(VALID_KEY)).json()
    assert result.get("isError") is not True
    assert text == "stub-vision-answer"
    vision = [c for c in calls if c["path"] == "/v1/chat/completions"]
    assert len(vision) == 1
    assert vision[0]["method"] == "POST" and vision[0]["bearer"] == VALID_KEY
    body = vision[0]["json"]
    assert body["model"] == "fallback-vision-model"
    content = body["messages"][0]["content"]
    assert content[1] == {"type": "text", "text": "图里是什么颜色"}
    assert content[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(_png()).decode()
    assert usage["tools"] == {"analyze_image": 1}
    assert list(vhub.UPLOAD_DIR.iterdir()), "内嵌路径应与 upload_image 共用落盘逻辑"


def test_analyze_image_wrapped_base64_sniffs_mime(vhub, monkeypatch):
    """缺省 mime_type 时按签名识别；容忍 CLI 折行复制的 Base64。"""
    calls = install_litellm_stub(monkeypatch, _vision_handler)
    _as_caller(vhub, monkeypatch)
    b64 = base64.b64encode(_GIF).decode()
    wrapped = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    answer = asyncio.run(vhub.analyze_image(question="q", image_base64=wrapped))
    assert answer == "stub-vision-answer"
    sent = [c for c in calls if c["path"] == "/v1/chat/completions"][-1]["json"]
    assert sent["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/gif;base64,")


def test_analyze_image_input_selection(vhub, monkeypatch):
    _as_caller(vhub, monkeypatch)
    b64 = base64.b64encode(_png()).decode()
    with pytest.raises(vhub.ToolError, match="not both"):
        asyncio.run(vhub.analyze_image(question="q", image_url="https://x/a.png", image_base64=b64))
    with pytest.raises(vhub.ToolError, match="image_url or image_base64 is required"):
        asyncio.run(vhub.analyze_image(question="q"))


def test_analyze_image_image_url_via_temp_file(vhub, monkeypatch):
    """兼容：临时文件相对路径走本地分支，转发体携带真实 MIME 与原始字节。"""
    calls = install_litellm_stub(monkeypatch, _vision_handler)
    url = vhub.store_image(_png(), "image/png")
    _as_caller(vhub, monkeypatch)
    answer = asyncio.run(vhub.analyze_image(question="颜色", image_url="/mcp/files/" + url.rsplit("/mcp/files/", 1)[-1]))
    assert answer == "stub-vision-answer"
    sent = [c for c in calls if c["path"] == "/v1/chat/completions"][0]["json"]
    data_url = sent["messages"][0]["content"][0]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == _png()


def test_upload_image_returns_temp_url_and_serves_file(vhub):
    """验收：upload_image 返回 30 分钟临时 URL，文件随机命名、原样落盘可回读。"""
    with TestClient(vhub.app) as client:
        session = _mcp_session(client)
        result, text = _tools_call(client, session, "upload_image", {
            "image_base64": base64.b64encode(_png()).decode(), "mime_type": "image/png"})
        assert result.get("isError") is not True
        body = json.loads(text)
        assert body["url"].startswith(vhub.PUBLIC_BASE + "/mcp/files/")
        assert body["expires_in"] == vhub.UPLOAD_TTL == 1800
        name = body["url"].rsplit("/mcp/files/", 1)[-1]
        assert name.endswith(".png") and len(name.split(".")[0]) >= 18   # 随机不可猜 token
        assert (vhub.UPLOAD_DIR / name).read_bytes() == _png()
        served = client.get(f"/mcp/files/{name}")
        assert served.status_code == 200
        assert served.content == _png()
        assert served.headers["content-type"].startswith("image/png")
        usage = client.get("/mcp/usage", headers=_bearer(VALID_KEY)).json()
        assert usage["tools"] == {"upload_image": 1}


def test_upload_image_rejects_bad_input_without_leaking(vhub):
    bad_b64 = "%%%not-base64%%%"
    with TestClient(vhub.app) as client:
        session = _mcp_session(client)
        assert "not valid base64" in _tool_error(client, session, "upload_image", {"image_base64": bad_b64})
        assert "non-empty" in _tool_error(client, session, "upload_image", {"image_base64": "  \n "})
        err = _tool_error(client, session, "upload_image", {
            "image_base64": base64.b64encode(_png()).decode(), "mime_type": "image/gif"})
        assert "does not match image signature" in err
        assert "unsupported mime" in _tool_error(client, session, "upload_image", {
            "image_base64": base64.b64encode(_png()).decode(), "mime_type": "image/bmp"})
    assert not list(vhub.UPLOAD_DIR.iterdir()), "被拒输入不得留下临时文件"


def test_decode_inline_image_whitelist_signatures(vhub):
    sample = {"image/png": _png(), "image/jpeg": _JPEG, "image/webp": _WEBP, "image/gif": _GIF}
    for mime, raw in sample.items():
        data, got = vhub.decode_inline_image(base64.b64encode(raw).decode(), mime)
        assert data == raw and got == mime


def test_decode_inline_image_size_limits(vhub):
    oversized = _png() + b"\x00" * (10 * 1024 * 1024)      # 签名合法但解码后 >10MB
    with pytest.raises(vhub.ToolError, match="too large"):
        vhub.decode_inline_image(base64.b64encode(oversized).decode(), "image/png")
    with pytest.raises(vhub.ToolError, match="too large"):   # 超长 Base64 文本解码前先拒
        vhub.decode_inline_image("A" * (2 * 10 * 1024 * 1024 + 1), None)


def test_error_messages_do_not_echo_payload(vhub):
    """边界：错误不回显 Base64 正文或图片内容。"""
    unknown = base64.b64encode(b"totally not an image").decode()
    with pytest.raises(vhub.ToolError) as exc:
        vhub.decode_inline_image(unknown, None)
    assert unknown not in str(exc.value)
    forged = base64.b64encode(_png()).decode()
    with pytest.raises(vhub.ToolError) as exc:
        vhub.decode_inline_image(forged, "image/jpeg")
    assert forged not in str(exc.value)


def test_builtin_tools_attested_in_config_state(vhub):
    """upload_image 与 analyze_image 一样进入内建 attestation，防外部前缀工具遮蔽。"""
    asyncio.run(vhub.register_external_tools())
    assert vhub.loaded_tool_owners["upload_image"] == "builtin"
    assert vhub.loaded_tool_owners["analyze_image"] == "builtin"


def test_http_upload_validates_signature(vhub):
    """/mcp/upload 与内嵌路径共用校验：声明 MIME 与签名不一致 → 400。"""
    with TestClient(vhub.app) as client:
        ok = client.post("/mcp/upload", headers=_bearer(VALID_KEY),
                         files={"file": ("a.png", _png(), "image/png")})
        assert ok.status_code == 200
        assert ok.json()["expires_in"] == vhub.UPLOAD_TTL
        forged = client.post("/mcp/upload", headers=_bearer(VALID_KEY),
                             files={"file": ("b.png", _png(), "image/jpeg")})
        assert forged.status_code == 400
        assert "does not match image signature" in forged.json()["error"]
