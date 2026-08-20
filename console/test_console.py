#!/usr/bin/env python3
"""console.py 核心单测（issue #10，无网络/docker/真实 LiteLLM）。

覆盖：登录成功后 sessions.db 只落 sha256 哈希（无完整 Key 明文）、管理员登录
不落 master key、旧 schema（`key` 列）import 时自动迁移为哈希、POST 缺
X-Requested-With → 403、未登录访问管理 API → 401、会话 cookie HMAC 签名被篡改
→ 401。LiteLLM 回环一律经 testutil.install_litellm_stub 桩替身。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from testutil import install_litellm_stub, load_service

CONSOLE_DIR = Path(__file__).parent
MASTER_KEY = "sk-master-unit-test-0001"
USER_KEY = "sk-user-abcdef-1234"          # 桩 LiteLLM 认可的用户虚拟 Key
ADMIN_EMAIL = "admin" + "@test.local"
ADMIN_PASSWORD = "test-pass-1"
XRW = {"X-Requested-With": "XMLHttpRequest"}


def _handler(method, path, bearer, json_body):
    """桩 LiteLLM：/key/info 认 USER_KEY，/global/spend 认 master，其余端点给空数据。"""
    if path == "/key/info":
        if bearer == USER_KEY:
            return 200, {"key_info": {"key_alias": "unit-user", "metadata": {"group": "default"},
                                      "models": [], "is_disabled": False}}
        return 401, {"error": "invalid key"}
    if path == "/global/spend":
        if bearer == MASTER_KEY:
            return 200, {}
        return 401, {"error": "not allowed"}
    if path == "/key/list":
        return 200, {"keys": [{"token": hashlib.sha256(USER_KEY.encode()).hexdigest(),
                               "key_alias": "unit-user", "metadata": {"group": "default"}}]}
    if path == "/spend/logs":
        return 200, {"data": []}
    if path in ("/model/info", "/v1/models"):
        return 200, {"data": []}
    return 404, {"error": f"unexpected stub path {path}"}


def _load(tmp_path: Path, extra_env: dict | None = None):
    env = {
        "CONSOLE_DATA": str(tmp_path / "cdata"),
        "LITELLM_MASTER_KEY": MASTER_KEY,
        "ONBOARD_ADMIN_TOKEN": "tok-onboard-unit",
        "LITELLM_BASE": "http://litellm-stub.invalid",   # 桩接管 httpx，地址仅占位
        "MCP_VISION_CONF": str(tmp_path / "vision-mcp.json"),
        "MCP_VISION_MODEL": "",
    }
    env.update(extra_env or {})
    return load_service(CONSOLE_DIR / "console.py", env)


def _cookie_of(resp) -> str:
    """从 Set-Cookie 头抠出 pll_session=sid.sig（secure cookie，不走客户端 jar 更稳）。"""
    for value in resp.headers.get_list("set-cookie"):
        if value.startswith("pll_session="):
            return value.split(";", 1)[0]
    return ""


@pytest.fixture
def console(tmp_path):
    """未配置 ADMIN_EMAIL/ADMIN_PASSWORD 的实例（master key 仍可登录的旧形态）。"""
    return _load(tmp_path)


@pytest.fixture
def console_admin(tmp_path):
    """配置了 ADMIN_EMAIL/ADMIN_PASSWORD 的实例（master key 不再作网页登录）。"""
    return _load(tmp_path, {"ADMIN_EMAIL": ADMIN_EMAIL, "ADMIN_PASSWORD": ADMIN_PASSWORD})


@pytest.fixture
def mcp_console(tmp_path):
    mod = _load(tmp_path, {
        "EXTERNAL_MCP_CONF": str(tmp_path / "external-mcp.json"),
        "MCP_RESTART_CMD": "true",
    })

    async def ready(_expected_sha256):
        return True

    mod.mcp_hub_ready = ready
    return mod


def _rows(mod) -> list[tuple]:
    db = sqlite3.connect(mod.SESSIONS_DB)
    rows = db.execute("SELECT sid, role, key_hash, key_last4, exp FROM sessions").fetchall()
    db.close()
    return rows


# ---------------------------------------------------------------- 用户登录只落哈希

def test_user_login_stores_hash_not_plaintext(console, monkeypatch):
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console.app) as client:
        resp = client.post("/console/api/login", json={"key": USER_KEY}, headers=XRW)
    assert resp.status_code == 200
    assert resp.json()["role"] == "user"

    rows = _rows(console)
    assert len(rows) == 1
    sid, role, key_hash, key_last4, exp = rows[0]
    assert role == "user"
    assert key_last4 == USER_KEY[-4:]
    # key_hash 是完整 Key 的 sha256（64 位 hex），不是明文
    assert key_hash == hashlib.sha256(USER_KEY.encode()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", key_hash)
    # 库文件字节级不含任何 sk- 前缀字符串 / 完整 Key
    raw = Path(console.SESSIONS_DB).read_bytes()
    assert USER_KEY.encode() not in raw
    assert b"sk-" not in raw


def test_master_key_login_keeps_admin_row_empty(console, monkeypatch):
    """未配置管理员账号时 master key 可登录（旧行为兜底），但 admin 会话不存任何密钥。"""
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console.app) as client:
        resp = client.post("/console/api/login", json={"key": MASTER_KEY}, headers=XRW)
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"
    _, role, key_hash, key_last4, _ = _rows(console)[0]
    assert (role, key_hash, key_last4) == ("admin", "", "")
    assert MASTER_KEY.encode() not in Path(console.SESSIONS_DB).read_bytes()


def test_admin_login_session_db_has_no_master_key(console_admin, monkeypatch):
    """ADMIN_EMAIL/ADMIN_PASSWORD 配置时经 /admin-login 登录，sessions.db 不含 master key。"""
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console_admin.app) as client:
        resp = client.post("/console/api/admin-login",
                           json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=XRW)
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"

    _, role, key_hash, key_last4, _ = _rows(console_admin)[0]
    assert (role, key_hash, key_last4) == ("admin", "", "")
    raw = Path(console_admin.SESSIONS_DB).read_bytes()
    assert MASTER_KEY.encode() not in raw          # master key 不落会话库
    assert ADMIN_PASSWORD.encode() not in raw      # 登录口令同样不落库


def test_admin_login_wrong_credentials(console_admin, monkeypatch):
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console_admin.app) as client:
        wrong_pw = client.post("/console/api/admin-login",
                               json={"email": ADMIN_EMAIL, "password": "nope"}, headers=XRW)
        wrong_mail = client.post("/console/api/admin-login",
                                 json={"email": "other@x.y", "password": ADMIN_PASSWORD}, headers=XRW)
    assert wrong_pw.status_code == 401
    assert wrong_mail.status_code == 401
    assert _rows(console_admin) == []              # 失败登录不产生会话


def test_master_key_not_web_login_when_admin_configured(console_admin, monkeypatch):
    """管理员账号配置后，master key 不再能从 /login 以用户身份进（/key/info 不认它）。"""
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console_admin.app) as client:
        resp = client.post("/console/api/login", json={"key": MASTER_KEY}, headers=XRW)
    assert resp.status_code == 401
    assert "invalid key" in resp.json()["error"]
    assert _rows(console_admin) == []


# ---------------------------------------------------------------- 旧 schema 迁移

def test_old_schema_migrated_to_hash_on_import(tmp_path):
    data = tmp_path / "cdata"
    data.mkdir()
    old_user_key = "sk-legacy-user-fullkey-9999"
    db = sqlite3.connect(data / "sessions.db")
    db.execute("CREATE TABLE sessions (sid TEXT PRIMARY KEY, role TEXT NOT NULL, "
               "key TEXT NOT NULL, exp REAL NOT NULL)")
    db.execute("INSERT INTO sessions VALUES ('sid-legacy-user', 'user', ?, ?)",
               (old_user_key, time.time() + 3600))
    db.execute("INSERT INTO sessions VALUES ('sid-legacy-admin', 'admin', ?, ?)",
               (MASTER_KEY, time.time() + 3600))
    db.commit()
    db.close()

    mod = _load(tmp_path)

    # 新 schema：key 列消失，key_hash/key_last4 就位
    db = sqlite3.connect(mod.SESSIONS_DB)
    cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    rows = {r[0]: r for r in db.execute("SELECT sid, role, key_hash, key_last4 FROM sessions")}
    db.close()
    assert "key" not in cols
    assert {"sid", "role", "key_hash", "key_last4", "exp"} <= cols
    # 用户会话 → sha256 哈希 + 尾 4 位；管理员会话 → 全空
    u = rows["sid-legacy-user"]
    assert u[1] == "user"
    assert u[2] == hashlib.sha256(old_user_key.encode()).hexdigest()
    assert u[3] == old_user_key[-4:]
    a = rows["sid-legacy-admin"]
    assert a[1:] == ("admin", "", "")
    # 完整密钥（用户 Key 与 master key）不再出现在库文件字节里
    raw = Path(mod.SESSIONS_DB).read_bytes()
    assert old_user_key.encode() not in raw
    assert MASTER_KEY.encode() not in raw


def test_fresh_state_dir_gets_new_schema(tmp_path):
    mod = _load(tmp_path)
    db = sqlite3.connect(mod.SESSIONS_DB)
    cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    db.close()
    assert cols == {"sid", "role", "key_hash", "key_last4", "exp"}


# ---------------------------------------------------------------- CSRF / 未登录门禁

def test_post_without_xrw_header_403(console_admin, monkeypatch):
    """登录类 POST 缺 X-Requested-With 头 → 403（CSRF 门禁先于凭据校验）。"""
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console_admin.app) as client:
        login = client.post("/console/api/login", json={"key": USER_KEY})
        admin_login = client.post("/console/api/admin-login",
                                  json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert login.status_code == 403
    assert "X-Requested-With" in login.json()["error"]
    assert admin_login.status_code == 403


def test_logged_in_post_without_xrw_403(console_admin, monkeypatch):
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console_admin.app) as client:
        ok = client.post("/console/api/admin-login",
                         json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=XRW)
        assert ok.status_code == 200
        cookie = _cookie_of(ok)
        assert cookie
        blocked = client.post("/console/api/keys/block", json={"key": "ab" * 32},
                              headers={"Cookie": cookie})     # 无 X-Requested-With
    assert blocked.status_code == 403


def test_admin_api_requires_session(console_admin, monkeypatch):
    """未登录访问管理 API → 401（GET 不需要 CSRF 头，直接打会话门禁）。"""
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console_admin.app) as client:
        for path in ("/console/api/keys", "/console/api/overview", "/console/api/me"):
            resp = client.get(path)
            assert resp.status_code == 401, path
        post = client.post("/console/api/keys/block", json={"key": "ab" * 32}, headers=XRW)
        assert post.status_code == 401


# ---------------------------------------------------------------- 会话 cookie 签名

def test_tampered_cookie_signature_rejected(console_admin, monkeypatch):
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console_admin.app) as client:
        ok = client.post("/console/api/admin-login",
                         json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers=XRW)
        cookie = _cookie_of(ok)                       # pll_session=<sid>.<sig>
        assert cookie
        # 正常 cookie 可用
        assert client.get("/console/api/me", headers={"Cookie": cookie}).status_code == 200

        sid, _, sig = cookie.split("=", 1)[1].partition(".")
        # 替换字符刻意取自字母表之外（sid 是 urlsafe、sig 是 hex），保证篡改后必不等于原值
        forged = [
            f"pll_session={sid[:-2]}~~.{sig}",        # 改 sid，沿用旧签名
            f"pll_session={sid}.{sig[:-2]}zz",        # 改签名
            f"pll_session={sid}",                     # 无签名段
            f"pll_session={sid}X.{sig}",              # 别的 sid 配旧签名
        ]
        for bad in forged:
            resp = client.get("/console/api/me", headers={"Cookie": bad})
            assert resp.status_code == 401, bad


def test_user_role_cannot_access_admin_api(console, monkeypatch):
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console.app) as client:
        ok = client.post("/console/api/login", json={"key": USER_KEY}, headers=XRW)
        assert ok.status_code == 200
        cookie = _cookie_of(ok)
        me = client.get("/console/api/me", headers={"Cookie": cookie})
        assert me.status_code == 200
        assert me.json()["role"] == "user"            # 用户会话可用（role=any 端点）
        admin_api = client.get("/console/api/keys", headers={"Cookie": cookie})
        assert admin_api.status_code == 403           # 管理端点按角色拒绝


# ---------------------------------------------------------------- MCP 分组分发（issue #51）

def _mcp_handler(method, path, bearer, json_body):
    if path == "/mcp/usage":
        return 401, {"error": "invalid or missing key"}
    if path == "/global/spend" and bearer == MASTER_KEY:
        return 200, {}
    if path == "/key/list":
        return 200, {"keys": [{"token": "hash-home", "metadata": {"group": "home"}}]}
    if path == "/model/info":
        return 200, {"data": []}
    if path == "/onboard/admin/list":
        return 200, {"sites": []}
    return 404, {"error": f"unexpected stub path {path}"}


def _mcp_only_handler(method, path, bearer, json_body):
    if path == "/key/list":
        return 200, {"keys": []}
    return _mcp_handler(method, path, bearer, json_body)


VISION_DEPLOYMENTS = [{
    "model_name": "qwen3.8-27b",
    "litellm_params": {"model": "openai/qwen3.8-27b-mtp2"},
    "model_info": {"id": "vision-dep"},
}, {
    "model_name": "deepseek-v4-flash",
    "litellm_params": {"model": "openai/deepseek-v4-flash-0731"},
    "model_info": {"id": "text-dep"},
}, {
    "model_name": "private-vlm",
    "litellm_params": {"model": "openai/private-vlm-build7"},
    "model_info": {"id": "private-dep"},
}]

MODELS_DEV_FIXTURE = {
    "alibaba/qwen3.8-27b": {
        "id": "alibaba/qwen3.8-27b", "name": "Qwen3.8 27B",
        "modalities": {"input": ["text", "image", "video"], "output": ["text"]},
    },
    "deepseek/deepseek-v4-flash": {
        "id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash",
        "modalities": {"input": ["text"], "output": ["text"]},
    },
}


def _vision_handler(method, path, bearer, json_body):
    if path == "/global/spend" and bearer == MASTER_KEY:
        return 200, {}
    if path == "/model/info":
        return 200, {"data": VISION_DEPLOYMENTS}
    if path == "/models.json":
        return 200, MODELS_DEV_FIXTURE
    if path == "/v1/chat/completions" and json_body and json_body.get("model") == "private-vlm":
        return 200, {"choices": [{"message": {"content": "red"}}]}
    return 404, {"error": f"unexpected stub path {path}"}


def test_mcp_vision_lists_registered_models_with_catalog_capabilities(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _vision_handler)
    client, hdr = _admin_client(mcp_console)
    response = client.get("/console/api/mcp/vision", headers=hdr)
    assert response.status_code == 200
    candidates = {item["model"]: item for item in response.json()["candidates"]}
    assert candidates["qwen3.8-27b"]["capability"] == "image"
    assert candidates["qwen3.8-27b"]["catalog_matches"][0]["id"] == "alibaba/qwen3.8-27b"
    assert candidates["deepseek-v4-flash"]["capability"] == "text_only"
    assert candidates["private-vlm"]["capability"] == "unknown"


def test_mcp_vision_saves_models_dev_verified_selection(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _vision_handler)
    client, hdr = _admin_client(mcp_console)
    response = client.post("/console/api/mcp/vision", headers=hdr, json={
        "model": "qwen3.8-27b", "catalog_id": "alibaba/qwen3.8-27b"})
    assert response.status_code == 200
    stored = json.loads(mcp_console.MCP_VISION_CONF.read_text())
    assert stored["model"] == "qwen3.8-27b"
    assert stored["catalog_id"] == "alibaba/qwen3.8-27b"
    assert stored["source"] == "models.dev"


def test_mcp_vision_rejects_unrelated_catalog_override(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _vision_handler)
    client, hdr = _admin_client(mcp_console)
    response = client.post("/console/api/mcp/vision", headers=hdr, json={
        "model": "deepseek-v4-flash", "catalog_id": "alibaba/qwen3.8-27b"})
    assert response.status_code == 400
    assert "does not match" in response.json()["error"]


def test_mcp_vision_rejects_cataloged_text_only_model(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _vision_handler)
    client, hdr = _admin_client(mcp_console)
    response = client.post("/console/api/mcp/vision", headers=hdr,
                           json={"model": "deepseek-v4-flash"})
    assert response.status_code == 400
    assert "without image input" in response.json()["error"]
    assert not mcp_console.MCP_VISION_CONF.exists()


def test_mcp_vision_probes_unknown_private_model_before_saving(mcp_console, monkeypatch):
    calls = install_litellm_stub(monkeypatch, _vision_handler)
    client, hdr = _admin_client(mcp_console)
    response = client.post("/console/api/mcp/vision", headers=hdr,
                           json={"model": "private-vlm"})
    assert response.status_code == 200
    assert response.json()["selected"]["source"] == "live_probe"
    probe = next(call for call in calls if call["path"] == "/v1/chat/completions")
    content = probe["json"]["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_mcp_vision_uses_stale_catalog_when_refresh_fails(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _vision_handler)
    client, hdr = _admin_client(mcp_console)
    assert client.get("/console/api/mcp/vision", headers=hdr).status_code == 200

    def unavailable_catalog(method, path, bearer, json_body):
        if path == "/models.json":
            return 503, {"error": "offline"}
        return _vision_handler(method, path, bearer, json_body)

    install_litellm_stub(monkeypatch, unavailable_catalog)
    response = client.get("/console/api/mcp/vision?refresh=1", headers=hdr)
    assert response.status_code == 200
    assert response.json()["catalog"]["status"] == "stale"
    candidates = {item["model"]: item for item in response.json()["candidates"]}
    assert candidates["qwen3.8-27b"]["capability"] == "image"


def test_mcp_register_and_update_groups(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _mcp_handler)
    client, hdr = _admin_client(mcp_console)
    created = client.post("/console/api/mcp/register", headers=hdr, json={
        "name": "svc", "url": "https://mcp.invalid/mcp", "api_key": "secret-1234",
        "prefix": "svc_", "groups": ["home"],
    })
    assert created.status_code == 200
    assert json.loads(mcp_console.EXTERNAL_MCP_CONF.read_text())[0]["groups"] == ["home"]

    listed = client.get("/console/api/mcp", headers=hdr)
    assert listed.status_code == 200
    assert listed.json()["external"][0]["groups"] == ["home"]
    assert listed.json()["groups"] == ["home"]

    updated = client.post("/console/api/mcp/groups", headers=hdr,
                          json={"name": "svc", "groups": []})
    assert updated.status_code == 200
    assert json.loads(mcp_console.EXTERNAL_MCP_CONF.read_text())[0]["groups"] == []


def test_mcp_register_rejects_unknown_group(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _mcp_handler)
    client, hdr = _admin_client(mcp_console)
    resp = client.post("/console/api/mcp/register", headers=hdr, json={
        "name": "svc", "url": "https://mcp.invalid/mcp", "prefix": "svc_",
        "groups": ["unknown"],
    })
    assert resp.status_code == 400
    assert "unknown groups" in resp.json()["error"]
    assert not mcp_console.EXTERNAL_MCP_CONF.exists()


@pytest.mark.parametrize("groups", [None, False, 0, "", {}, [""], [0]])
def test_mcp_register_rejects_present_malformed_groups(mcp_console, monkeypatch, groups):
    install_litellm_stub(monkeypatch, _mcp_handler)
    client, hdr = _admin_client(mcp_console)
    resp = client.post("/console/api/mcp/register", headers=hdr, json={
        "name": "svc", "url": "https://mcp.invalid/mcp", "prefix": "svc_",
        "groups": groups,
    })
    assert resp.status_code == 400
    assert not mcp_console.EXTERNAL_MCP_CONF.exists()


def test_mcp_config_restored_when_restart_fails(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _mcp_handler)
    original = b'[{"name":"old","url":"https://old.invalid","groups":[]}]\n'
    mcp_console.EXTERNAL_MCP_CONF.write_bytes(original)
    mcp_console.EXTERNAL_MCP_CONF.chmod(0o640)
    monkeypatch.setattr(mcp_console, "restart_mcp_hub", lambda: "exit 1")
    client, hdr = _admin_client(mcp_console)
    resp = client.post("/console/api/mcp/register", headers=hdr, json={
        "name": "svc", "url": "https://mcp.invalid/mcp", "prefix": "svc_", "groups": []})
    assert resp.status_code == 502
    assert mcp_console.EXTERNAL_MCP_CONF.read_bytes() == original
    assert mcp_console.EXTERNAL_MCP_CONF.stat().st_mode & 0o777 == 0o640


def test_mcp_config_restored_when_readiness_fails(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _mcp_handler)
    mcp_console.write_mcp_conf([])
    original = mcp_console.EXTERNAL_MCP_CONF.read_bytes()
    calls = []
    monkeypatch.setattr(mcp_console, "restart_mcp_hub", lambda: calls.append(1) or "ok")

    readiness = iter([False, True])

    async def not_ready_then_restored(_expected_sha256):
        return next(readiness)

    monkeypatch.setattr(mcp_console, "mcp_hub_ready", not_ready_then_restored)
    client, hdr = _admin_client(mcp_console)
    resp = client.post("/console/api/mcp/register", headers=hdr, json={
        "name": "svc", "url": "https://mcp.invalid/mcp", "prefix": "svc_", "groups": []})
    assert resp.status_code == 502
    assert mcp_console.EXTERNAL_MCP_CONF.read_bytes() == original
    assert len(calls) == 2


def test_restart_timeout_is_reported(mcp_console, monkeypatch):
    def timeout(*args, **kwargs):
        raise mcp_console.subprocess.TimeoutExpired(args[0], 60)

    monkeypatch.setattr(mcp_console.subprocess, "run", timeout)
    assert mcp_console.restart_mcp_hub() == "restart timed out"


def test_groups_snapshot_counts_mcp_and_blocks_delete(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _mcp_only_handler)
    mcp_console.write_mcp_conf([{
        "name": "svc", "url": "https://mcp.invalid/mcp", "api_key": "",
        "prefix": "svc_", "groups": ["home"],
    }])
    client, hdr = _admin_client(mcp_console)
    rows = client.get("/console/api/groups", headers=hdr).json()["groups"]
    assert next(row for row in rows if row["name"] == "home")["mcps"] == 1
    blocked = client.post("/console/api/groups/delete", headers=hdr, json={"name": "home"})
    assert blocked.status_code == 409
    assert "MCP" in blocked.json()["error"]


def test_group_rename_updates_mcp_bindings(mcp_console, monkeypatch):
    install_litellm_stub(monkeypatch, _mcp_only_handler)
    mcp_console.write_mcp_conf([{
        "name": "svc", "url": "https://mcp.invalid/mcp", "api_key": "",
        "prefix": "svc_", "groups": ["home"],
    }])
    client, hdr = _admin_client(mcp_console)
    resp = client.post("/console/api/groups/rename", headers=hdr,
                       json={"from": "home", "to": "family"})
    assert resp.status_code == 200
    assert resp.json()["mcps_rebound"] == 1
    assert json.loads(mcp_console.EXTERNAL_MCP_CONF.read_text())[0]["groups"] == ["family"]


def test_group_rename_rolls_back_keys_before_mcp_on_partial_failure(mcp_console, monkeypatch):
    keys = [
        {"token": "a" * 64, "metadata": {"group": "home"}},
        {"token": "b" * 64, "metadata": {"group": "home"}},
    ]
    updates = []

    def handler(method, path, bearer, body):
        if path == "/global/spend":
            return 200, {}
        if path == "/key/list":
            return 200, {"keys": keys}
        if path == "/model/info":
            return 200, {"data": []}
        if path == "/onboard/admin/list":
            return 200, {"sites": []}
        if path == "/key/update":
            updates.append(body)
            if body["key"] == "b" * 64:
                return 500, {"error": "failed"}
            return 200, {"ok": True}
        return 404, {"error": path}

    install_litellm_stub(monkeypatch, handler)
    original = [{"name": "svc", "url": "https://mcp.invalid/mcp", "groups": ["home"]}]
    mcp_console.write_mcp_conf(original)
    client, hdr = _admin_client(mcp_console)
    resp = client.post("/console/api/groups/rename", headers=hdr,
                       json={"from": "home", "to": "family"})
    assert resp.status_code == 502
    assert json.loads(mcp_console.EXTERNAL_MCP_CONF.read_text()) == original
    assert updates[-1] == {"key": "a" * 64, "metadata": {"group": "home"}}


def test_historic_key_import_requires_matching_live_key(console, monkeypatch):
    install_litellm_stub(monkeypatch, _handler)
    token = hashlib.sha256(USER_KEY.encode()).hexdigest()
    client, hdr = _admin_client(console)
    bad = client.post("/console/api/keys/import", headers=hdr,
                      json={"key": token, "plaintext": "sk-wrong"})
    assert bad.status_code == 400
    assert console.vault_get(token) == ""
    ok = client.post("/console/api/keys/import", headers=hdr,
                     json={"key": token, "plaintext": USER_KEY})
    assert ok.status_code == 200
    assert ok.json()["key"] == USER_KEY
    assert console.vault_get(token) == USER_KEY


def test_mcp_use_config_never_contains_placeholder_and_binds_reveal_generation():
    source = (CONSOLE_DIR / "static" / "mcp.html").read_text()
    assert "sk-（请选择用户 Key）" not in source
    assert "generation !== revealGeneration" in source
    assert "'/keys/import'" in source
    assert "id=\"vision-model\"" in source
    assert "'/mcp/vision'" in source
    assert "catalog_id: catalog.value" in source


def test_pi_and_dsh_use_config_contracts():
    source = (CONSOLE_DIR / "static" / "keys.html").read_text()

    assert "pi-settings-json" in source
    assert "api: 'openai-completions'" in source
    assert "supportsReasoningEffort: true" in source
    assert "requiresReasoningContentOnAssistantMessages: true" in source
    assert "defaultThinkingLevel: 'high'" in source
    for level in ("off", "minimal", "low", "medium", "xhigh"):
        assert f"{level}: null" in source
    assert "high: 'high'" in source
    assert "max: 'max'" in source

    assert "dsh-credentials-yaml" in source
    assert "dsh-settings-yaml" in source
    assert "apiKeyEnv: LLM_PORTAL_API_KEY" in source
    assert "contextWindow: 1048576" in source
    assert "maxTokens: 32768" in source
    assert "reasoningEfforts:" in source
    assert "agent-default-model:" in source
    assert "provider: private-llm" in source
    assert "reasoningEffort: high" in source
    assert "thinkingFormat:" not in source


def test_logout_removes_session(console, monkeypatch):
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console.app) as client:
        ok = client.post("/console/api/login", json={"key": USER_KEY}, headers=XRW)
        cookie = _cookie_of(ok)
        assert client.post("/console/api/logout", headers={"Cookie": cookie, **XRW}).status_code == 200
        assert client.get("/console/api/me", headers={"Cookie": cookie}).status_code == 401
    assert _rows(console) == []                       # 会话行已删


# ---------------------------------------------------------------- 思考强度提取

def test_row_effort_prefers_spend_logs_metadata(console):
    # 生产实际落库形态：LiteLLM 1.96.2 写库白名单只保留 spend_logs_metadata
    assert console.row_effort({"metadata": {"spend_logs_metadata": {"effort": "high"}}}) == "high"
    assert console.row_effort({"metadata": {"spend_logs_metadata": {"effort": "budget:8192"}}}) == "budget:8192"


def test_row_effort_fallback_shapes(console):
    assert console.row_effort({"metadata": {"requester_metadata": {"effort": "low"}}}) == "low"
    assert console.row_effort({"metadata": {"effort": "medium"}}) == "medium"   # 形态兜底
    assert console.row_effort({"metadata": {"usage_object": {}}}) == ""        # 历史行/未携带
    assert console.row_effort({}) == ""


# ---------------------------------------------------------------- issue #46 直通白名单

def test_rebuild_deployments_carry_allowed_openai_params(console, monkeypatch):
    """retag_site / 别名克隆都靠「/model/new 重建 + /model/delete 删旧」改 deployment——
    重建 payload 必须带 allowed_openai_params，否则一次分组改写就把 reasoning_effort
    直通（issue #46）洗掉。"""
    import asyncio

    dep = {"model_name": "m1",
           "litellm_params": {"model": "openai/m1", "api_base": "http://10.77.0.21:8890/v1",
                              "tags": ["home"], "connect_timeout": 5, "timeout": 600},
           "model_info": {"id": "dep-1"}}

    def handler(method, path, bearer, json_body):
        if path == "/model/info":
            return 200, {"data": [dep]}
        if path in ("/model/new", "/model/delete"):
            return 200, {"ok": True}
        if path == "/global/spend" and bearer == MASTER_KEY:   # master key 网页登录的探活
            return 200, {}
        return 404, {"error": f"unexpected stub path {path}"}

    calls = install_litellm_stub(monkeypatch, handler)
    assert asyncio.run(console.retag_site("10.77.0.21", ["lab"])) == []
    lp = [c for c in calls if c["path"] == "/model/new"][0]["json"]["litellm_params"]
    assert lp["allowed_openai_params"] == ["reasoning_effort"]
    assert lp["tags"] == ["lab"]                      # retag 既有语义不变

    calls.clear()
    with TestClient(console.app) as client:           # 别名克隆走正式路由（需登录会话）
        ok = client.post("/console/api/login", json={"key": MASTER_KEY}, headers=XRW)
        cookie = _cookie_of(ok)
        resp = client.post("/console/api/models/alias",
                           headers={"Cookie": cookie, **XRW},
                           json={"alias": "m1-copy", "target": "m1"})
    assert resp.status_code == 200
    lp = [c for c in calls if c["path"] == "/model/new"][0]["json"]["litellm_params"]
    assert lp["allowed_openai_params"] == ["reasoning_effort"]
    assert lp["model"] == "openai/m1"


# ---------------------------------------------------------------- 站点模型管理（探测/添加/刷新/删除）

SM_SITE = {"name": "workstation", "pubkey": "PUBKEY0", "wg_ip": "10.77.0.14",
           "models": "[]", "groups": '["home"]', "status": "active", "created_at": 1700000000}
UPSTREAM_IDS = ["qwen3.8-27b-mtp2"]                 # 站点上游 /v1/models 实际在服务的 id


def _sm_handler(deps=None, upstream_ids=None):
    """桩 LiteLLM + onboardd + 站点上游。站点上游探测的 /v1/models 不带
    Authorization 头，据此与网关自身 /v1/models（master key）区分。"""
    deps = [] if deps is None else deps
    upstream_ids = UPSTREAM_IDS if upstream_ids is None else upstream_ids

    def handler(method, path, bearer, json_body):
        if path == "/onboard/admin/list":
            return 200, {"sites": [dict(SM_SITE)]}
        if path == "/v1/models" and not bearer:
            return 200, {"data": [{"id": i, "owned_by": "llamacpp"} for i in upstream_ids]}
        if path == "/model/info":
            return 200, {"data": deps}
        if path in ("/model/new", "/model/delete", "/onboard/admin/models"):
            return 200, {"ok": True}
        if path == "/global/spend" and bearer == MASTER_KEY:
            return 200, {}
        return 404, {"error": f"unexpected stub path {path}"}

    return handler


def _admin_client(console):
    client = TestClient(console.app)
    ok = client.post("/console/api/login", json={"key": MASTER_KEY}, headers=XRW)
    return client, {"Cookie": _cookie_of(ok), **XRW}


def test_sites_probe_returns_upstream_ids(console, monkeypatch):
    install_litellm_stub(monkeypatch, _sm_handler(upstream_ids=["b", "a"]))
    client, hdr = _admin_client(console)
    resp = client.get("/console/api/sites/probe?site=workstation&port=8004", headers=hdr)
    assert resp.status_code == 200
    body = resp.json()
    assert [m["id"] for m in body["models"]] == ["b", "a"]   # 去重保序
    assert body["api_base"] == "http://10.77.0.14:8004/v1"
    # 未知站点 / 坏端口 / 用户角色
    assert client.get("/console/api/sites/probe?site=ghost&port=8004", headers=hdr).status_code == 404
    assert client.get("/console/api/sites/probe?site=workstation&port=abc", headers=hdr).status_code == 400


def test_sites_probe_rejects_user_role(console, monkeypatch):
    install_litellm_stub(monkeypatch, _handler)
    with TestClient(console.app) as client:
        ok = client.post("/console/api/login", json={"key": USER_KEY}, headers=XRW)
        resp = client.get("/console/api/sites/probe?site=x&port=1",
                          headers={"Cookie": _cookie_of(ok)})
    assert resp.status_code == 403


def test_sites_models_add(console, monkeypatch):
    dep = {"model_name": "deepseek-v4-flash-0731",
           "litellm_params": {"model": "openai/deepseek-v4-flash-0731",
                              "api_base": "http://10.77.0.14:8890/v1", "tags": ["home"]},
           "model_info": {"id": "dep-ds"}}
    calls = install_litellm_stub(monkeypatch, _sm_handler(deps=[dep]))
    client, hdr = _admin_client(console)
    resp = client.post("/console/api/sites/models", headers=hdr,
                       json={"site": "workstation", "name": "qwen3.8-27b",
                             "port": 8004, "upstream_model": "qwen3.8-27b-mtp2"})
    assert resp.status_code == 200
    assert resp.json()["upstream_model"] == "qwen3.8-27b-mtp2"
    new = [c for c in calls if c["path"] == "/model/new"][0]["json"]
    assert new["model_name"] == "qwen3.8-27b"                 # 对外名（带点号，MODEL_RE 放行）
    lp = new["litellm_params"]
    assert lp["model"] == "openai/qwen3.8-27b-mtp2"           # 上游真名
    assert lp["api_base"] == "http://10.77.0.14:8004/v1"
    assert lp["tags"] == ["home"]                             # 分组沿站点现状
    assert lp["allowed_openai_params"] == ["reasoning_effort"]
    sync = [c for c in calls if c["path"] == "/onboard/admin/models"][0]["json"]
    assert {"name": "qwen3.8-27b", "port": 8004,
            "upstream_model": "qwen3.8-27b-mtp2"} in sync["models"]


def test_sites_models_add_duplicate_409(console, monkeypatch):
    dep = {"model_name": "qwen3.8-27b",
           "litellm_params": {"model": "openai/qwen3.8-27b-mtp2",
                              "api_base": "http://10.77.0.14:8004/v1"},
           "model_info": {"id": "dep-1"}}
    install_litellm_stub(monkeypatch, _sm_handler(deps=[dep]))
    client, hdr = _admin_client(console)
    resp = client.post("/console/api/sites/models", headers=hdr,
                       json={"site": "workstation", "name": "qwen3.8-27b", "port": 8004})
    assert resp.status_code == 409


def test_sites_models_refresh_rebuilds_upstream(console, monkeypatch):
    """换模型场景：对外名/api_base/tags/限流保留，仅换上游 model id；先建新后删旧。"""
    dep = {"model_name": "qwen3.6-35b-fp8",
           "litellm_params": {"model": "openai/qwen3.6-35b-fp8",
                              "api_base": "http://10.77.0.14:8004/v1",
                              "tags": ["home"], "rpm": 7},
           "model_info": {"id": "dep-q"}}
    calls = install_litellm_stub(monkeypatch, _sm_handler(deps=[dep]))
    client, hdr = _admin_client(console)
    resp = client.post("/console/api/sites/models/refresh", headers=hdr,
                       json={"site": "workstation", "name": "qwen3.6-35b-fp8",
                             "port": 8004, "upstream_model": "qwen3.8-27b-mtp2"})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["previous"], body["upstream_model"]) == ("qwen3.6-35b-fp8", "qwen3.8-27b-mtp2")
    seq = [c["path"] for c in calls if c["path"] in ("/model/new", "/model/delete")]
    assert seq == ["/model/new", "/model/delete"]             # 不留零 deployment 窗口
    new = [c for c in calls if c["path"] == "/model/new"][0]["json"]
    assert new["model_name"] == "qwen3.6-35b-fp8"             # 对外名不变，订阅方无感
    lp = new["litellm_params"]
    assert lp["model"] == "openai/qwen3.8-27b-mtp2"
    assert lp["rpm"] == 7 and lp["tags"] == ["home"]
    assert [c for c in calls if c["path"] == "/model/delete"][0]["json"] == {"id": "dep-q"}
    sync = [c for c in calls if c["path"] == "/onboard/admin/models"][0]["json"]
    assert sync["models"] == [{"name": "qwen3.6-35b-fp8", "port": 8004,
                               "upstream_model": "qwen3.8-27b-mtp2"}]


def test_sites_models_refresh_unchanged_and_unknown(console, monkeypatch):
    dep = {"model_name": "m1",
           "litellm_params": {"model": "openai/qwen3.8-27b-mtp2",
                              "api_base": "http://10.77.0.14:8004/v1"},
           "model_info": {"id": "dep-1"}}
    install_litellm_stub(monkeypatch, _sm_handler(deps=[dep]))
    client, hdr = _admin_client(console)
    same = client.post("/console/api/sites/models/refresh", headers=hdr,
                       json={"site": "workstation", "name": "m1",
                             "port": 8004, "upstream_model": "qwen3.8-27b-mtp2"})
    assert same.status_code == 200 and same.json()["unchanged"] is True
    missing = client.post("/console/api/sites/models/refresh", headers=hdr,
                          json={"site": "workstation", "name": "nope",
                                "port": 8004, "upstream_model": "x"})
    assert missing.status_code == 404


def test_sites_models_delete(console, monkeypatch):
    dep = {"model_name": "qwen3.6-35b-fp8",
           "litellm_params": {"model": "openai/qwen3.6-35b-fp8",
                              "api_base": "http://10.77.0.14:8004/v1"},
           "model_info": {"id": "dep-q"}}
    calls = install_litellm_stub(monkeypatch, _sm_handler(deps=[dep]))
    client, hdr = _admin_client(console)
    resp = client.post("/console/api/sites/models/delete", headers=hdr,
                       json={"site": "workstation", "name": "qwen3.6-35b-fp8", "port": 8004})
    assert resp.status_code == 200
    assert [c for c in calls if c["path"] == "/model/delete"][0]["json"] == {"id": "dep-q"}
    sync = [c for c in calls if c["path"] == "/onboard/admin/models"][0]["json"]
    assert sync["models"] == []                               # 登记簿同步清空


def test_sites_lists_known_ports(console, monkeypatch):
    """/sites 返回 known_ports（deployment ∪ 登记簿端口）——前端「添加模型」的
    端口下拉数据源，杜绝手打端口拼错 api_base。"""
    dep = {"model_name": "qwen3.6-35b-fp8",
           "litellm_params": {"model": "openai/qwen3.6-35b-fp8",
                              "api_base": "http://10.77.0.14:8004/v1"},
           "model_info": {"id": "dep-1"}}

    def handler(method, path, bearer, json_body):
        if path == "/onboard/admin/list":
            # 登记簿里另有一个端口 9000（尚无 deployment）也应出现在已知端口里
            site = dict(SM_SITE, models='[{"name": "m2", "port": 9000}]')
            return 200, {"sites": [site]}
        if path == "/model/info":
            return 200, {"data": [dep]}
        if path == "/global/spend" and bearer == MASTER_KEY:
            return 200, {}
        return 404, {"error": f"unexpected stub path {path}"}

    install_litellm_stub(monkeypatch, handler)
    client, hdr = _admin_client(console)
    resp = client.get("/console/api/sites", headers=hdr)
    assert resp.status_code == 200
    row = resp.json()["sites"][0]
    assert row["known_ports"] == [8004, 9000]      # deployment 端口 ∪ 登记簿端口
    assert row["deps"][0]["port"] == 8004


def test_sites_token_accepts_dotted_model_names(console, monkeypatch):
    """注册表单允许 qwen3.8-27b 一类带点号模型名（此前 NAME_RE 误拒，MODEL_RE 放行）。"""
    seen = []

    def handler(method, path, bearer, json_body):
        if path == "/onboard/admin/tokens":
            seen.append(json_body)
            return 200, {"token": "t", "expires_in": 900, "install_command": "cmd"}
        if path == "/global/spend" and bearer == MASTER_KEY:
            return 200, {}
        return 404, {"error": f"unexpected stub path {path}"}

    install_litellm_stub(monkeypatch, handler)
    client, hdr = _admin_client(console)
    resp = client.post("/console/api/sites/token", headers=hdr,
                       json={"site": "s1", "models": [{"name": "qwen3.8-27b", "port": 8004}]})
    assert resp.status_code == 200
    assert seen[0]["models"][0]["name"] == "qwen3.8-27b"
