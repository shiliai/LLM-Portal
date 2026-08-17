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
