#!/usr/bin/env python3
"""onboardd.py 核心单测（issue #10，无网络/docker/wireguard）。

覆盖：本机管理 API 的 x-admin-token 门禁（无/错 token → 403，源码
require_admin 对全部 admin 端点统一返回 403）、一次性 install token 的签发与
基本校验（未知/已用/过期 token → 403、坏站名 → 400、活跃站点重签 → 409、
坏公钥 → 400）。LiteLLM/wg 均不触达（只测不产生外部调用的分支）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from testutil import install_litellm_stub, load_service

ONBOARDD_DIR = Path(__file__).parent
ADMIN_TOKEN = "tok-admin-unit"
WRONG_TOKEN = "tok-wrong"
GOOD_PUBKEY = "Q" * 43 + "="            # wg base64 公钥形状（43 位 + padding）


def _insert_site(mod, name: str, status: str = "active", wg_ip: str = "10.77.0.99") -> None:
    with sqlite3.connect(mod.DB_PATH) as conn:
        conn.execute("INSERT INTO sites VALUES (?,?,?,?,?,?,?)",
                     (name, GOOD_PUBKEY, wg_ip, "[]", '["default"]', status, int(time.time())))


@pytest.fixture
def onboardd(tmp_path):
    data = tmp_path / "odata"
    pub = tmp_path / "wireguard-public.key"
    pub.write_text("VPSWGPUBLICKEYBASE64PLACEHOLDER==\n")
    return load_service(ONBOARDD_DIR / "onboardd.py", {
        "ONBOARDD_DATA": str(data),
        "LITELLM_MASTER_KEY": "sk-master-unit",
        "ONBOARD_ADMIN_TOKEN": ADMIN_TOKEN,
        "VPS_PUBLIC_KEY_PATH": str(pub),
        "WG_EXEC": "wg-absent",          # 相关分支不触 wg；误触即显式失败
    })


def _admin_headers(token: str = ADMIN_TOKEN) -> dict:
    return {"x-admin-token": token}


def _issue(client, site="site-a", models=None):
    return client.post("/onboard/admin/tokens",
                       headers=_admin_headers(),
                       json={"site": site, "models": models or [{"name": "m1", "port": 8000}]})


# ---------------------------------------------------------------- admin token 门禁

def test_admin_endpoints_reject_missing_or_wrong_token(onboardd):
    """源码行为：require_admin 仅比对 x-admin-token 头，不符一律 403（非 401）。"""
    with TestClient(onboardd.app) as client:
        for headers in ({}, {"x-admin-token": WRONG_TOKEN}):
            lst = client.get("/onboard/admin/list", headers=headers)
            tok = client.post("/onboard/admin/tokens", headers=headers,
                              json={"site": "s", "models": [{"name": "m", "port": 1}]})
            rvk = client.post("/onboard/admin/revoke", headers=headers, json={"site": "s"})
            grp = client.post("/onboard/admin/groups", headers=headers,
                              json={"site": "s", "groups": []})
            for resp in (lst, tok, rvk, grp):
                assert resp.status_code == 403, (headers, resp.status_code)
                assert resp.json()["error"] == "forbidden"
    # 门禁拒掉时也不该签出任何 token
    with sqlite3.connect(onboardd.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0


def test_admin_list_with_token_ok(onboardd):
    with TestClient(onboardd.app) as client:
        resp = client.get("/onboard/admin/list", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"sites": []}


# ---------------------------------------------------------------- install token 签发

def test_admin_token_issue_shape(onboardd):
    with TestClient(onboardd.app) as client:
        resp = _issue(client, site="alpha", models=[{"name": "m1", "port": 8000},
                                                    {"name": "m2", "port": 8001,
                                                     "upstream_model": "up-m2"}])
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == onboardd.TOKEN_TTL == 900
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", body["token"])   # 随机 urlsafe token
    assert body["install_command"] == \
        f'curl -fsSL "https://{onboardd.DOMAIN}/onboard/install?token={body["token"]}" | sudo bash'
    # 落库：未用、15 分钟后过期、带站点/模型/分组与预分配 wg_ip
    with sqlite3.connect(onboardd.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tokens WHERE token=?", (body["token"],)).fetchone()
    assert row is not None
    assert row["used"] == 0
    assert row["site"] == "alpha"
    # 源码归一化：upstream_model 未传时显式存 None（confirm 时回退模型名）
    assert json.loads(row["models"]) == [{"name": "m1", "port": 8000, "upstream_model": None},
                                         {"name": "m2", "port": 8001, "upstream_model": "up-m2"}]
    assert row["wg_ip"].startswith(f"{onboardd.WG_SUBNET_PREFIX}.")
    assert row["expires_at"] > time.time() + 800


def test_issue_rejects_bad_site_name(onboardd):
    with TestClient(onboardd.app) as client:
        for bad in ("bad site!", "", "x" * 33, "站点一"):
            resp = _issue(client, site=bad)
            assert resp.status_code == 400, bad
            assert resp.json()["error"] == "bad site name"


def test_issue_rejects_active_site_until_revoked(onboardd):
    _insert_site(onboardd, "beta")
    with TestClient(onboardd.app) as client:
        resp = _issue(client, site="beta")
    assert resp.status_code == 409
    assert "revoke first" in resp.json()["error"]


# ---------------------------------------------------------------- install token 校验

def test_install_unknown_token_403(onboardd):
    with TestClient(onboardd.app) as client:
        resp = client.get("/onboard/install", params={"token": "no-such-token"})
    assert resp.status_code == 403
    assert "invalid, expired or used" in resp.text


def _insert_token(mod, *, used=0, expires_at=None) -> str:
    import secrets
    token = secrets.token_urlsafe(24)
    with sqlite3.connect(mod.DB_PATH) as conn:
        conn.execute("INSERT INTO tokens (token, site, models, groups, wg_ip, expires_at, used) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (token, "gamma", json.dumps([{"name": "m1", "port": 8000}]), '["default"]',
                      "10.77.0.50", expires_at or int(time.time()) + 600, used))
    return token


def test_install_valid_token_serves_script(onboardd):
    token = _insert_token(onboardd)
    with TestClient(onboardd.app) as client:
        resp = client.get("/onboard/install", params={"token": token})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/x-shellscript")
    script = resp.text
    assert f'TOKEN="{token}"' in script                       # 回填 token
    assert onboardd.vps_public_key() in script                # 注入 VPS 公钥
    assert f"{onboardd.DOMAIN}:{onboardd.WG_PORT}" in script  # wg endpoint
    assert "8000" in script                                   # 模型端口列表


def test_install_used_or_expired_token_403(onboardd):
    used_tok = _insert_token(onboardd, used=1)
    expired_tok = _insert_token(onboardd, expires_at=int(time.time()) - 1)
    with TestClient(onboardd.app) as client:
        used = client.get("/onboard/install", params={"token": used_tok})
        expired = client.get("/onboard/install", params={"token": expired_tok})
    assert used.status_code == 403
    assert expired.status_code == 403


def test_register_rejects_bad_pubkey_before_any_side_effect(onboardd):
    token = _insert_token(onboardd)
    with TestClient(onboardd.app) as client:
        for bad in ("not-base64!", "Q" * 42 + "==", ""):
            resp = client.post("/onboard/register", json={"token": token, "pubkey": bad})
            assert resp.status_code == 400, bad
            assert "invalid pubkey" in resp.json()["error"]
    # 坏公钥在 wg/写库前被拒：token 仍未标记使用
    with sqlite3.connect(onboardd.DB_PATH) as conn:
        assert conn.execute("SELECT used FROM tokens WHERE token=?", (token,)).fetchone()[0] == 0


def test_register_unknown_token_403(onboardd):
    with TestClient(onboardd.app) as client:
        resp = client.post("/onboard/register",
                           json={"token": "nope", "pubkey": GOOD_PUBKEY})
    assert resp.status_code == 403


def test_admin_groups_updates_site_groups(onboardd):
    _insert_site(onboardd, "delta", wg_ip="10.77.0.60")
    with TestClient(onboardd.app) as client:
        ok = client.post("/onboard/admin/groups", headers=_admin_headers(),
                         json={"site": "delta", "groups": ["lab", "default"]})
        unknown = client.post("/onboard/admin/groups", headers=_admin_headers(),
                              json={"site": "ghost", "groups": []})
        bad = client.post("/onboard/admin/groups", headers=_admin_headers(),
                          json={"site": "delta", "groups": ["bad group!"]})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "site": "delta", "groups": ["lab", "default"]}
    assert unknown.status_code == 404
    assert bad.status_code == 400


def test_admin_revoke_unknown_site_404(onboardd):
    with TestClient(onboardd.app) as client:
        resp = client.post("/onboard/admin/revoke", headers=_admin_headers(),
                           json={"site": "ghost"})
    assert resp.status_code == 404
    assert "unknown site" in resp.json()["error"]


# --------------------------------------- offload 地址解耦（PUBLIC_BASE/WG_ENDPOINT_HOST/WG_SUBNET_PREFIX）

@pytest.fixture
def onboardd_offload(tmp_path):
    """offload 模式 env 组合：高位端口 PUBLIC_BASE（带尾斜杠，验证归一化）、
    wg Endpoint 指向网关内网 IP、独立网段前缀。"""
    data = tmp_path / "odata-offload"
    pub = tmp_path / "wireguard-public.key"
    pub.write_text("VPSWGPUBLICKEYBASE64PLACEHOLDER==\n")
    return load_service(ONBOARDD_DIR / "onboardd.py", {
        "ONBOARDD_DATA": str(data),
        "LITELLM_MASTER_KEY": "sk-master-unit",
        "ONBOARD_ADMIN_TOKEN": ADMIN_TOKEN,
        "VPS_PUBLIC_KEY_PATH": str(pub),
        "WG_EXEC": "wg-absent",
        "PUBLIC_BASE": "https://llm.example.com:8443/",    # 尾斜杠应被 rstrip
        "WG_ENDPOINT_HOST": "192.168.88.22",
        "WG_SUBNET_PREFIX": "10.78.0",
        "WG_VPS_IP": "10.78.0.1",
    })


def test_offload_env_shapes_install_command_and_script(onboardd_offload):
    """env 解耦逐项落地：install_command 用 PUBLIC_BASE（归一化后无 //），
    预分配 wg_ip 用新前缀，下发脚本的回调地址/wg Endpoint/AllowedIPs/自检 ping 全随 env。"""
    mod = onboardd_offload
    with TestClient(mod.app) as client:
        resp = _issue(client, site="lan-site")
    assert resp.status_code == 200
    body = resp.json()
    assert body["install_command"] == \
        f'curl -fsSL "https://llm.example.com:8443/onboard/install?token={body["token"]}" | sudo bash'
    with sqlite3.connect(mod.DB_PATH) as conn:
        row = conn.execute("SELECT wg_ip FROM tokens WHERE token=?",
                           (body["token"],)).fetchone()
    assert row[0].startswith("10.78.0.")
    with TestClient(mod.app) as client:
        script = client.get("/onboard/install", params={"token": body["token"]}).text
    assert 'ENDPOINT="https://llm.example.com:8443"' in script
    assert "s|__ENDPOINT_WG__|192.168.88.22:51820|" in script
    assert "AllowedIPs = 10.78.0.0/24" in script
    assert "ping -c 2 -W 3 10.78.0.1 " in script


def test_default_env_keeps_legacy_addresses(onboardd):
    """不设新 env 时行为与旧版逐字节一致（standalone/external 兼容性锚点）。"""
    assert onboardd.PUBLIC_BASE == f"https://{onboardd.DOMAIN}"
    assert onboardd.WG_ENDPOINT_HOST == onboardd.DOMAIN
    assert onboardd.WG_ALLOWED == "10.77.0.0/24"
    assert onboardd.GW_WG_IP == "10.77.0.1"


# ---------------------------------------------------------------- confirm → /model/new 注册

def test_confirm_registers_with_param_passthrough(onboardd, monkeypatch):
    """issue #46：drop_params=true 下通用 openai/ deployment 会静默丢 reasoning_effort
    （supported 列表不含、vLLM 实际支持）——注册 payload 必须带 allowed_openai_params。"""
    calls = install_litellm_stub(monkeypatch, lambda m, p, b, j: (200, {"ok": True}))
    with sqlite3.connect(onboardd.DB_PATH) as conn:
        conn.execute("INSERT INTO sites VALUES (?,?,?,?,?,?,?)",
                     ("site-x", GOOD_PUBKEY, "10.77.0.21",
                      '[{"name": "m1", "port": 8890}]', '["home"]', "registered", int(time.time())))
        conn.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?)",
                     ("tok-confirm-1", "site-x", '[{"name": "m1", "port": 8890}]', '["home"]',
                      "10.77.0.21", int(time.time()) + 600, 0))
    with TestClient(onboardd.app) as client:
        resp = client.post("/onboard/confirm",
                           json={"token": "tok-confirm-1", "ok": True, "results": {}})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    news = [c for c in calls if c["path"] == "/model/new"]
    assert len(news) == 1
    lp = news[0]["json"]["litellm_params"]
    assert lp["allowed_openai_params"] == ["reasoning_effort"]   # 直通白名单
    assert lp["model"] == "openai/m1" and lp["api_base"] == "http://10.77.0.21:8890/v1"
    assert lp["tags"] == ["home"]
    with sqlite3.connect(onboardd.DB_PATH) as conn:
        assert conn.execute("SELECT status FROM sites WHERE name='site-x'").fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0  # 一次性 token 已消耗
