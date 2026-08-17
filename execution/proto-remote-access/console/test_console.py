#!/usr/bin/env python3
"""console.py 核心单测（issue #10，无网络/docker/真实 LiteLLM）。

覆盖：登录成功后 sessions.db 只落 sha256 哈希（无完整 Key 明文）、管理员登录
不落 master key、旧 schema（`key` 列）import 时自动迁移为哈希、POST 缺
X-Requested-With → 403、未登录访问管理 API → 401、会话 cookie HMAC 签名被篡改
→ 401。LiteLLM 回环一律经 testutil.install_litellm_stub 桩替身。
"""
from __future__ import annotations

import hashlib
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
ADMIN_EMAIL = "admin@test.local"
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
