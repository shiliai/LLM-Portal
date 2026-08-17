"""consoled：网关管理控制台后端（设计 §3.6，US-P9 修订/US-P14）。

静态页面挂 /console/（高保真原型接线落地），API 挂 /console/api/*：
  POST /console/api/login {key}        用户登录：有效虚拟 Key→用户（仅 /my）
  POST /console/api/admin-login        管理员登录 {email, password, totp}（console.env
                                       的 ADMIN_EMAIL/ADMIN_PASSWORD[/_TOTP_SECRET]）；
                                       配置后 master key 不再作为网页登录方式
  GET  /console/api/2fa                2FA 状态（是否启用 + 来源 state/env）
  POST /console/api/2fa/setup          生成新密钥（pending）+ otpauth URI + 二维码
  POST /console/api/2fa/confirm        {code} 校验 pending 密钥并启用（页面生成优先于 env 预置）
  POST /console/api/2fa/disable        {password, code} 停用页面启用的 2FA
  POST /console/api/logout             注销
  GET  /console/api/me                 会话信息（角色 + 导航裁剪）
  GET  /console/api/overview           仪表盘：今日聚合 + 站点隧道 + deployment 健康 + 近期错误
  GET  /console/api/sites              站点清单（onboardd + wg 握手 + deployment 数/明细）
  POST /console/api/sites/token        签发一次性安装命令（转 onboardd）
  POST /console/api/sites/revoke       吊销站点（转 onboardd）
  POST /console/api/sites/groups       调整站点分组（deployment retag + onboardd 同步）
  GET  /console/api/sites/probe        探测站点上游 /v1/models（自动抓取可用 model id）
  POST /console/api/sites/models       手动向已注册站点添加模型（写 LiteLLM + 同步登记簿）
  POST /console/api/sites/models/refresh  刷新 deployment 上游 model id（对外名不变，订阅方无感）
  POST /console/api/sites/models/delete   删除站点单个模型 deployment
  GET  /console/api/groups             分组清单（tags ∪ key 绑定；成员/绑定数）
  POST /console/api/groups/create      新建分组（带初始成员站点，retag）
  POST /console/api/groups/rename      改名（retag + 批量改 Key 绑定 + onboardd 同步）
  POST /console/api/groups/delete      删除（有 Key 绑定则拒绝）
  GET  /console/api/models             模型与 deployment（直选/别名判定）
  POST /console/api/models/alias       新建别名（克隆目标全部 deployment）
  GET  /console/api/keys               Key 清单（/key/list 全对象）
  POST /console/api/keys/create        建 Key（一次性返回全文；明文入加密保险库，可再查）
  POST /console/api/keys/reveal        管理员查看 Key 明文（保险库解密；旧 Key 未入库则 404）
  POST /console/api/keys/update        改备注名/分组/模型白名单（/key/update）
  POST /console/api/keys/block         禁用（/key/block）
  POST /console/api/keys/unblock       启用（/key/unblock）
  POST /console/api/keys/delete        删除（/key/delete）
  GET  /console/api/usage?days=N       用量聚合（/spend/logs 本地聚合）
  GET  /console/api/usage/logs?days=N  逐请求明细（时间/Key/模型/token/延迟/状态/request_id）
  GET  /console/api/my                 用户自查（/key/info + /mcp/usage 代查）
  GET  /console/api/mcp                外部 MCP 注册清单（脱敏）
  POST /console/api/mcp/register       注册外部 MCP（写配置 + 重启 mcp-hub）
  POST /console/api/mcp/remove         移除（写配置 + 重启 mcp-hub）
  GET  /console/api/mcp/tools          聚合 tools/list 预览（直连各外部 MCP）
  GET  /console/api/mcp/usage?days=N   按 Key 工具调用计数（usage.db）

会话：sqlite 落盘（容器重建/重部署不掉线；cookie 只放 sid+HMAC）；会话表只存
Key 的 sha256 哈希与尾 4 位——完整用户 Key 与 master key 均不落 sessions.db
（管理员会话不含任何密钥，管理面回环一律用进程 env 里的 master key）；
登录限速；变更类请求要求 X-Requested-With 头。LiteLLM 1.96.2 语义（设计 §12 r6 spike 结论）：
/key/list 需 return_full_object、禁用字段 blocked、tags 在 litellm_params、
/spend/logs 过滤参数不可靠故全量拉取本地聚合。
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import sqlite3
import struct
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import segno
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from urllib.parse import quote

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:4000")
ONBOARD_URL = os.environ.get("ONBOARDD_URL", "http://127.0.0.1:8100")
MCP_HUB_URL = os.environ.get("MCP_HUB_URL", "http://127.0.0.1:8200")
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
ONBOARD_ADMIN_TOKEN = os.environ["ONBOARD_ADMIN_TOKEN"]
# 管理员网页登录凭据（console.env，deploy.sh 从 vps/.env 生成）：邮箱 + 密码 + 可选 TOTP。
# 未配置 ADMIN_EMAIL 时回退旧行为（master key 可网页登录），兼容未迁移部署
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_TOTP_SECRET = os.environ.get("ADMIN_TOTP_SECRET", "").strip()
MCP_VISION_MODEL = os.environ.get("MCP_VISION_MODEL", "qwen3.6-35b-fp8")
EXTERNAL_MCP_CONF = Path(os.environ.get("EXTERNAL_MCP_CONF", "/etc/private-llm/external-mcp.json"))
MCP_USAGE_DB = Path(os.environ.get("MCP_USAGE_DB", "/var/lib/private-llm/mcp-hub/usage.db"))
WG_IFACE = os.environ.get("WG_IFACE", "wg0")
# 宿主机操作命令前缀（#7 容器化）：默认保留宿主机直跑语义；容器模式由 compose 注入
# docker.sock 版本（挂载 /var/run/docker.sock 的容器 ≈ 宿主机 root，见 runbook §7 取舍）
WG_EXEC = shlex.split(os.environ.get("WG_EXEC", "wg"))
MCP_RESTART_CMD = shlex.split(os.environ.get("MCP_RESTART_CMD", "systemctl restart mcp-hub"))
STATIC_DIR = Path(__file__).parent / "static"
STATE_DIR = Path(os.environ.get("CONSOLE_DATA", "/var/lib/private-llm/console"))
SECRET_PATH = STATE_DIR / "console.secret"
# 页面启用的 2FA 密钥（#8）：{enabled, secret, pending}；优先于 env 预置
TOTP_STATE_PATH = STATE_DIR / "totp.json"
# Key 明文保险库（管理员可再查）：明文经 Fernet 加密落 sqlite，密钥文件独立 0600。
# 安全边界变化见 runbook §7——网关成为密钥保管者，VPS 失陷即密钥失陷；轮换 = 删库重签
VAULT_DB = STATE_DIR / "keyvault.db"
VAULT_KEY_PATH = STATE_DIR / "keyvault.key"
SESSION_TTL = 8 * 3600
LOGIN_FAIL_LIMIT, LOGIN_WINDOW = 5, 60
HANDSHAKE_ONLINE = 180  # 最近握手 3 分钟内视为在线
# issue #46：drop_params=true 下通用 openai/ deployment 会静默丢弃 reasoning_effort
# （supported 列表不含、vLLM 上游实际支持）——retag/别名克隆重建 deployment 时
# 必须带上，否则一次分组改写就把直通配置洗掉
PASS_THROUGH_OPENAI_PARAMS = ["reasoning_effort"]

STATE_DIR.mkdir(parents=True, exist_ok=True)
if SECRET_PATH.exists():
    _SECRET = SECRET_PATH.read_bytes()
else:
    _SECRET = secrets.token_bytes(32)
    SECRET_PATH.write_bytes(_SECRET)
    os.chmod(SECRET_PATH, 0o600)

SESSIONS_DB = STATE_DIR / "sessions.db"            # 会话落盘：容器重建/重部署不掉线
with sqlite3.connect(SESSIONS_DB) as _conn:
    _cols = {r[1] for r in _conn.execute("PRAGMA table_info(sessions)")}
    if not _cols:                                  # 全新部署
        _conn.execute("CREATE TABLE sessions ("
                      "sid TEXT PRIMARY KEY, role TEXT NOT NULL, "
                      "key_hash TEXT NOT NULL DEFAULT '', key_last4 TEXT NOT NULL DEFAULT '', "
                      "exp REAL NOT NULL)")
    elif "key" in _cols:                           # 旧schema 存完整 Key → 迁移为哈希
        _rows = _conn.execute("SELECT sid, role, key, exp FROM sessions").fetchall()
        _conn.execute("DROP TABLE sessions")
        _conn.execute("CREATE TABLE sessions ("
                      "sid TEXT PRIMARY KEY, role TEXT NOT NULL, "
                      "key_hash TEXT NOT NULL DEFAULT '', key_last4 TEXT NOT NULL DEFAULT '', "
                      "exp REAL NOT NULL)")
        _conn.executemany(
            "INSERT INTO sessions (sid, role, key_hash, key_last4, exp) VALUES (?,?,?,?,?)",
            [(sid, role,
              "" if role == "admin" else hashlib.sha256(key.encode()).hexdigest(),
              "" if role == "admin" else key[-4:], exp)
             for sid, role, key, exp in _rows])
        print(f"!! sessions.db 已迁移：{len(_rows)} 个会话的完整密钥清除（仅保留哈希），"
              "建议轮换曾有会话落盘的 Key", file=sys.stderr)
    else:
        _conn.execute("CREATE TABLE IF NOT EXISTS sessions ("
                      "sid TEXT PRIMARY KEY, role TEXT NOT NULL, "
                      "key_hash TEXT NOT NULL DEFAULT '', key_last4 TEXT NOT NULL DEFAULT '', "
                      "exp REAL NOT NULL)")
os.chmod(SESSIONS_DB, 0o600)                       # 显式收紧：会话库含哈希，不随 umask 走
_login_fails: dict[str, list] = {}      # ip -> [window_start, fail_count]
_totp_used: dict[int, float] = {}       # TOTP timestep -> 使用时刻（防同一动态码重放）
NAME_RE = re.compile(r"[a-zA-Z0-9_-]{1,32}")
GROUP_RE = re.compile(r"[a-zA-Z0-9_-]{1,32}")
# 模型名/上游 model id 允许点号（qwen3.8-27b 一类版本号命名，站点名仍用 NAME_RE）
MODEL_RE = re.compile(r"[a-zA-Z0-9_.-]{1,64}")


# ---------------------------------------------------------------- 基础设施

def jerr(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


async def ll(method: str, path: str, *, key: str = LITELLM_MASTER_KEY, **kw) -> httpx.Response:
    """回环调 LiteLLM；异常转 502 可判读错误。"""
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.request(method, f"{LITELLM_BASE}{path}",
                                    headers={"Authorization": f"Bearer {key}"}, **kw)


async def ll_json(method: str, path: str, *, key: str = LITELLM_MASTER_KEY, **kw) -> tuple[int, object]:
    try:
        r = await ll(method, path, key=key, **kw)
    except httpx.HTTPError as exc:
        return 502, {"error": f"litellm unreachable: {exc}"}
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": r.text[:300]}


async def onboard(method: str, path: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.request(method, f"{ONBOARD_URL}{path}",
                                    headers={"x-admin-token": ONBOARD_ADMIN_TOKEN}, **kw)


def sign(sid: str) -> str:
    return hmac.new(_SECRET, sid.encode(), hashlib.sha256).hexdigest()[:32]


def session_of(request: Request) -> dict | None:
    raw = request.cookies.get("pll_session", "")
    if not raw or "." not in raw:
        return None
    sid, _, sig = raw.partition(".")
    if not hmac.compare_digest(sign(sid), sig):
        return None
    with sqlite3.connect(SESSIONS_DB) as conn:
        row = conn.execute("SELECT role, key_hash, key_last4, exp FROM sessions WHERE sid=?",
                           (sid,)).fetchone()
    if row is None or row[3] < time.time():
        return None
    return {"role": row[0], "key_hash": row[1], "key_last4": row[2]}


def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


async def require(request: Request, role: str = "admin") -> dict | JSONResponse:
    """会话校验 + 角色 + CSRF 头；返回 session 或错误响应。"""
    sess = session_of(request)
    if sess is None:
        return jerr("not logged in", 401)
    if role != "any" and sess["role"] != role:
        return jerr("admin only", 403)
    if request.method == "POST" and request.headers.get("x-requested-with") != "XMLHttpRequest":
        return jerr("missing X-Requested-With", 403)
    return sess


# ---------------------------------------------------------------- 登录 / 会话

def _str_eq(a: str, b: str) -> bool:
    """时序安全字符串比较（compare_digest 对非 ASCII str 会抛 TypeError，先过 sha256）。"""
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(),
                               hashlib.sha256(b.encode()).digest())


def _b32_key(secret_b32: str) -> bytes | None:
    s = "".join(secret_b32.split()).upper()
    try:
        return base64.b32decode(s + "=" * (-len(s) % 8))
    except (ValueError, TypeError):
        return None


def totp_verify(secret_b32: str, code: str) -> bool:
    """RFC 6238 TOTP（SHA1 / 6 位 / 30s），容忍 ±1 步时钟漂移；同一步长动态码仅可用一次。"""
    key = _b32_key(secret_b32)
    if key is None:
        return False
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return False
    step_now = int(time.time()) // 30
    for off in (-1, 0, 1):
        step = step_now + off
        mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
        o = mac[-1] & 0xF
        val = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
        if hmac.compare_digest(f"{val:06d}", code):
            if step in _totp_used:
                return False
            for stale in [k for k in _totp_used if k < step_now - 2]:
                _totp_used.pop(stale, None)
            _totp_used[step] = time.time()
            return True
    return False


if ADMIN_TOTP_SECRET and _b32_key(ADMIN_TOTP_SECRET) is None:
    print("!! ADMIN_TOTP_SECRET 不是合法 base32，管理员 2FA 校验将一律失败，请修正 console.env",
          file=sys.stderr)


def _totp_state() -> dict:
    try:
        st = json.loads(TOTP_STATE_PATH.read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_totp_state(st: dict) -> None:
    TOTP_STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n")
    os.chmod(TOTP_STATE_PATH, 0o600)


def active_totp() -> tuple[str, str]:
    """(secret, source)：安全设置页启用（state）优先于 console.env 预置（env）。"""
    st = _totp_state()
    if st.get("enabled") and st.get("secret"):
        return st["secret"], "state"
    return (ADMIN_TOTP_SECRET, "env") if ADMIN_TOTP_SECRET else ("", "")


# ---------------------------------------------------------------- Key 明文保险库

try:
    from cryptography.fernet import Fernet
except ImportError:            # 容器镜像经 fastmcp 传递依赖必有；本地裸跑提示
    Fernet = None

if Fernet is not None:
    if VAULT_KEY_PATH.exists():
        _vault_fernet = Fernet(VAULT_KEY_PATH.read_bytes().strip())
    else:
        _vault_key = Fernet.generate_key()
        VAULT_KEY_PATH.write_bytes(_vault_key + b"\n")
        os.chmod(VAULT_KEY_PATH, 0o600)
        _vault_fernet = Fernet(_vault_key)
    with sqlite3.connect(VAULT_DB) as _conn:
        _conn.execute("CREATE TABLE IF NOT EXISTS keys ("
                      "token TEXT PRIMARY KEY, cipher TEXT NOT NULL, created_at INTEGER NOT NULL)")
    os.chmod(VAULT_DB, 0o600)
else:
    _vault_fernet = None
    print("!! cryptography 未安装，Key 明文保险库不可用（reveal 将一律 404）", file=sys.stderr)


def vault_store(token_hash: str, plaintext: str) -> None:
    if _vault_fernet is None:
        return
    with sqlite3.connect(VAULT_DB) as conn:
        conn.execute("INSERT OR REPLACE INTO keys (token, cipher, created_at) VALUES (?,?,?)",
                     (token_hash, _vault_fernet.encrypt(plaintext.encode()).decode(), int(time.time())))


def vault_get(token_hash: str) -> str:
    if _vault_fernet is None:
        return ""
    try:
        with sqlite3.connect(VAULT_DB) as conn:
            row = conn.execute("SELECT cipher FROM keys WHERE token=?", (token_hash,)).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    try:
        return _vault_fernet.decrypt(row[0].encode()).decode()
    except Exception:           # 密钥文件轮换后旧密文不可解
        return ""


def _start_session(role: str, key_hash: str = "", key_last4: str = "") -> Response:
    sid = secrets.token_urlsafe(24)
    exp = time.time() + SESSION_TTL
    with sqlite3.connect(SESSIONS_DB) as conn:
        conn.execute("DELETE FROM sessions WHERE exp < ?", (time.time() - 3600,))   # 顺手清过期
        conn.execute("INSERT INTO sessions (sid, role, key_hash, key_last4, exp) VALUES (?,?,?,?,?)",
                     (sid, role, key_hash, key_last4, exp))
    resp = JSONResponse({"ok": True, "role": role})
    resp.set_cookie("pll_session", f"{sid}.{sign(sid)}", max_age=SESSION_TTL,
                    httponly=True, secure=True, samesite="lax", path="/console")
    return resp


async def api_admin_login_state(request: Request) -> Response:
    """登录页据此决定是否展示 2FA 输入框（只暴露开关，不暴露账号信息）。"""
    return JSONResponse({"configured": bool(ADMIN_EMAIL), "totp": bool(active_totp()[0])})


async def api_admin_login(request: Request) -> Response:
    ip = client_ip(request)
    now = time.time()
    win = _login_fails.get(ip)
    if win and now - win[0] < LOGIN_WINDOW and win[1] >= LOGIN_FAIL_LIMIT:
        return jerr("尝试过于频繁，请一分钟后再试", 429)
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        return jerr("missing X-Requested-With", 403)
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return jerr("管理员账号未配置（在 console.env 设 ADMIN_EMAIL / ADMIN_PASSWORD 后重启 console）", 503)
    try:
        body = await request.json()
        email, password, totp = body.get("email", ""), body.get("password", ""), body.get("totp", "")
    except (ValueError, AttributeError):
        return jerr("bad request", 400)
    # 统一报错文案，不区分错在哪一项；2FA 只在口令正确时校验（省计算，语义不变）
    ok = _str_eq(email.strip().lower(), ADMIN_EMAIL) and _str_eq(password, ADMIN_PASSWORD)
    totp_secret, _ = active_totp()
    if ok and totp_secret:
        ok = totp_verify(totp_secret, totp)
    if not ok:
        if not win or now - win[0] >= LOGIN_WINDOW:
            _login_fails[ip] = [now, 1]
        else:
            win[1] += 1
        return jerr("邮箱、密码或动态码错误", 401)
    _login_fails.pop(ip, None)
    # 管理接口本就以服务端 env 里的 master key 回环调 LiteLLM；管理会话不存任何密钥
    return _start_session("admin")


# ---------------------------------------------------------------- 2FA 管理（#8，admin）

async def api_2fa(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    secret, source = active_totp()
    return JSONResponse({"enabled": bool(secret), "source": source,
                         "pending": bool(_totp_state().get("pending"))})


async def api_2fa_setup(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    st = _totp_state()
    st["pending"] = secret
    _write_totp_state(st)
    label = quote(f"private-llm:{ADMIN_EMAIL or 'admin'}")
    uri = (f"otpauth://totp/{label}?secret={secret}&issuer=private-llm"
           "&algorithm=SHA1&digits=6&period=30")
    qr = segno.make(uri, error="m").svg_data_uri(scale=4)
    return JSONResponse({"secret": secret, "otpauth": uri, "qr": qr})


async def api_2fa_confirm(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        code = ((await request.json()).get("code") or "")
    except ValueError:
        return jerr("bad request", 400)
    st = _totp_state()
    pending = st.get("pending") or ""
    if not pending:
        return jerr("请先生成密钥", 400)
    if not totp_verify(pending, code):
        return jerr("动态码不正确", 401)
    # 已启用时这是密钥轮换：confirm 通过即切换（需持有新密钥的认证器）
    st.pop("pending", None)
    st["enabled"], st["secret"] = True, pending
    _write_totp_state(st)
    return JSONResponse({"ok": True})


async def api_2fa_disable(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    secret, source = active_totp()
    if not secret:
        return jerr("2FA 未启用", 400)
    if source == "env":
        return jerr("2FA 由 console.env 预置（ADMIN_TOTP_SECRET），如需停用请清空该变量并重启 console", 409)
    try:
        body = await request.json()
    except ValueError:
        return jerr("bad request", 400)
    if not (_str_eq(body.get("password", ""), ADMIN_PASSWORD) and totp_verify(secret, body.get("code") or "")):
        return jerr("密码或动态码错误", 401)
    _write_totp_state({})
    return JSONResponse({"ok": True})


async def api_login(request: Request) -> Response:
    ip = client_ip(request)
    now = time.time()
    win = _login_fails.get(ip)
    if win and now - win[0] < LOGIN_WINDOW and win[1] >= LOGIN_FAIL_LIMIT:
        return jerr("too many attempts, retry in a minute", 429)
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        return jerr("missing X-Requested-With", 403)
    try:
        key = (await request.json()).get("key", "").strip()
    except (ValueError, KeyError):
        return jerr("bad request", 400)
    role = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 管理员账号已配置时 master key 不再作为网页登录（改走 /admin-login），
            # 未配置则保留旧行为兜底
            if not ADMIN_EMAIL:
                r = await client.get(f"{LITELLM_BASE}/global/spend",
                                     headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 200:
                    role = "admin"
            if role is None:
                r = await client.get(f"{LITELLM_BASE}/key/info",
                                     headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 200:
                    role = "user"
    except httpx.HTTPError as exc:
        return jerr(f"gateway unreachable: {exc}", 502)
    if role is None:
        if not win or now - win[0] >= LOGIN_WINDOW:
            _login_fails[ip] = [now, 1]
        else:
            win[1] += 1
        return jerr("invalid key", 401)
    _login_fails.pop(ip, None)
    # 会话只落哈希 + 尾 4 位（展示用）；后续自查经 master key + 哈希对账
    kh = hashlib.sha256(key.encode()).hexdigest() if role == "user" else ""
    return _start_session(role, kh, key[-4:] if role == "user" else "")


async def api_logout(request: Request) -> Response:
    raw = request.cookies.get("pll_session", "")
    if "." in raw:
        with sqlite3.connect(SESSIONS_DB) as conn:
            conn.execute("DELETE FROM sessions WHERE sid=?", (raw.partition(".")[0],))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("pll_session", path="/console")
    return resp


async def api_me(request: Request) -> Response:
    sess = await require(request, role="any")
    if isinstance(sess, JSONResponse):
        return sess
    alias, group = "", ""
    if sess["role"] == "user":
        for k in await key_list_full():
            if k.get("token") == sess["key_hash"]:
                alias = k.get("key_alias") or ""
                group = (k.get("metadata") or {}).get("group") or "default"
                break
    return JSONResponse({"role": sess["role"], "alias": alias, "group": group,
                         "key_last4": sess["key_last4"] or "—"})


# ---------------------------------------------------------------- 数据源辅助

def _logs_rows(body) -> list[dict]:
    if isinstance(body, dict):
        return body.get("data") or []
    return body or []


async def fetch_logs() -> list[dict]:
    _, body = await ll_json("GET", "/spend/logs")
    return _logs_rows(body) if isinstance(body, (list, dict)) else []


def _local_dt(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def logs_since(logs: list[dict], days: float) -> list[dict]:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    out = []
    for r in logs:
        dt = _local_dt(r.get("startTime") or "")
        if dt and dt >= cutoff:
            out.append(r)
    return out


# 展示时区固定 Asia/Shanghai(+08)：LiteLLM 日志时间为 UTC；容器无 tzdata，用固定偏移零依赖
_CST = timezone(timedelta(hours=8))


def _aware_dt(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso_to_cst(iso: str) -> str:
    dt = _aware_dt(iso)
    return dt.astimezone(_CST).strftime("%Y-%m-%dT%H:%M:%S") if dt else (iso or "")[:19]


def row_tft_ms(row: dict) -> int:
    """首 token 时延 = completionStartTime - startTime（生产实测 100% 可算）。"""
    d1, d2 = _aware_dt(row.get("startTime") or ""), _aware_dt(row.get("completionStartTime") or "")
    return max(0, int((d2 - d1).total_seconds() * 1000)) if (d1 and d2) else 0


def wg_handshakes() -> dict[str, int]:
    """pubkey -> 距上次握手秒数（0=从未）。dump: interface 行 + peer 行。"""
    try:
        r = subprocess.run(WG_EXEC + ["show", WG_IFACE, "dump"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {}
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in r.stdout.strip().splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 6:
            continue
        ts = int(f[4] or 0)
        out[f[0]] = -1 if ts == 0 else max(0, int(time.time()) - ts)
    return out


async def onboard_sites() -> list[dict]:
    try:
        r = await onboard("GET", "/onboard/admin/list")
        sites = r.json().get("sites", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        sites = []
    for s in sites:
        s["models"] = json.loads(s.get("models") or "[]")
        s["groups"] = json.loads(s.get("groups") or "[]")
    return sites


async def litellm_deployments() -> list[dict]:
    _, body = await ll_json("GET", "/model/info")
    return body.get("data", []) if isinstance(body, dict) else []


def dep_tags(dep: dict) -> list[str]:
    return (dep.get("litellm_params") or {}).get("tags") or []


def dep_of_site(dep: dict, wg_ip: str) -> bool:
    return str((dep.get("litellm_params") or {}).get("api_base", "")).startswith(f"http://{wg_ip}:")


async def key_list_full() -> list[dict]:
    """全量 Key（1.96.2 上限 size=100；MVP 规模足够，超出再加分页）。"""
    _, body = await ll_json("GET", "/key/list", params={"return_full_object": "true", "size": 100})
    if isinstance(body, dict):
        keys = body.get("keys") or []
        return [k for k in keys if isinstance(k, dict)]
    return [k for k in (body or []) if isinstance(k, dict)]


async def retag_site(wg_ip: str, new_groups: list[str]) -> list[str]:
    """站点全部 deployment 换 tags：先 /model/new 建新、后 /model/delete 删旧（只用已验证端点）。"""
    deps = [d for d in await litellm_deployments() if dep_of_site(d, wg_ip)]
    errors = []
    for dep in deps:
        src = dep.get("litellm_params") or {}
        info = dep.get("model_info") or {}
        params = {
            "model": src.get("model"),
            "api_base": src.get("api_base"),
            "api_key": "none",                      # /model/info 读回不含 api_key，重建按上游无鉴权口径
            "tags": sorted(set(new_groups) - {"default"}),
            "allowed_openai_params": PASS_THROUGH_OPENAI_PARAMS,
            "connect_timeout": src.get("connect_timeout", 5),
            "timeout": src.get("timeout", 600),
        }
        for lim in ("rpm", "tpm"):
            if src.get(lim) is not None:
                params[lim] = src[lim]
            elif info.get(lim) is not None:
                params[lim] = info[lim]
        code, body = await ll_json("POST", "/model/new", json={
            "model_name": dep.get("model_name"), "litellm_params": params})
        if code != 200:
            errors.append(f"create {dep.get('model_name')}: {str(body)[:150]}")
            continue
        code, body = await ll_json("POST", "/model/delete", json={"id": info.get("id")})
        if code != 200:
            errors.append(f"delete old {dep.get('model_name')}: {str(body)[:150]}")
    return errors


async def sync_onboard_group(site_name: str, groups: list[str]) -> None:
    await onboard("POST", "/onboard/admin/groups", json={"site": site_name, "groups": groups})


async def fetch_upstream_models(wg_ip: str, port: int) -> tuple[list[dict], str]:
    """网关侧探测站点上游的 OpenAI 兼容 /v1/models（自动抓取 model id）。

    SSRF 口径（US-03 C8）：URL 由注册表里的 wg_ip + 受校验端口拼出，不接受
    任意用户提供的 URL；wg 网段属「本地显式放行」，与 LiteLLM 路由流量同一边界。
    返回 ([{id, owned_by}], "") 或 ([], 可判读错误)。
    """
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"http://{wg_ip}:{port}/v1/models")
    except httpx.HTTPError as exc:
        return [], f"上游不可达（隧道断开或服务未启动？）：{exc}"
    if r.status_code != 200:
        return [], f"上游返回 HTTP {r.status_code}"
    try:
        data = r.json().get("data") or []
    except ValueError:
        return [], "上游响应不是 JSON（确认该端口是 OpenAI 兼容服务）"
    seen, models = set(), []
    for m in data:
        mid = str(m.get("id") or "")
        if mid and mid not in seen:
            seen.add(mid)
            models.append({"id": mid, "owned_by": m.get("owned_by") or ""})
    return models, ""


def _dep_port(dep: dict) -> int:
    """从 api_base（http://wg_ip:port/v1）解出端口；解析失败给 0。"""
    tail = str((dep.get("litellm_params") or {}).get("api_base") or "").rsplit(":", 1)[-1]
    digits = tail.split("/")[0]
    return int(digits) if digits.isdigit() else 0


def _site_models_from_deps(deps: list[dict]) -> list[dict]:
    """LiteLLM deployment → onboardd 登记簿口径的模型清单（[{name, port, upstream_model}]），
    供手动加/刷新/删后全量回写（权威数据在 LiteLLM，登记簿只求与之一致）。"""
    return [{"name": d.get("model_name"), "port": _dep_port(d),
             "upstream_model": str((d.get("litellm_params") or {}).get("model") or "")
                               .removeprefix("openai/")}
            for d in deps]


def _dep_params(src: dict, info: dict, *, model: str, api_base: str, tags) -> dict:
    """重建/新建 deployment 的 litellm_params，口径对齐 retag_site 与 onboardd confirm：
    api_key 按上游无鉴权；直通白名单必须带，否则一次重建就洗掉 issue #46 的直通配置。"""
    params = {"model": model, "api_base": api_base, "api_key": "none",
              "tags": sorted({t for t in tags if t != "default"}),
              "allowed_openai_params": PASS_THROUGH_OPENAI_PARAMS,
              "connect_timeout": src.get("connect_timeout", 5),
              "timeout": src.get("timeout", 600)}
    for lim in ("rpm", "tpm"):
        if src.get(lim) is not None:
            params[lim] = src[lim]
        elif info.get(lim) is not None:
            params[lim] = info[lim]
    return params


async def sync_onboard_models(site_name: str, models: list[dict]) -> None:
    await onboard("POST", "/onboard/admin/models", json={"site": site_name, "models": models})


# ---------------------------------------------------------------- 仪表盘 / 用量

def err_text(row: dict) -> str:
    md = row.get("metadata") or {}
    return str(row.get("exception") or md.get("error_str") or md.get("status_code") or "failure")[:160]


def key_last4(row: dict) -> str:
    ak = str(row.get("api_key") or "")
    return f"…{ak[-4:]}" if len(ak) >= 8 else (ak or "—")


def row_cached(row: dict) -> int:
    """缓存读取 token（vLLM/OpenAI：prompt_tokens_details.cached_tokens；Anthropic：cache_read_input_tokens）。"""
    uo = (row.get("metadata") or {}).get("usage_object") or {}
    ptd = uo.get("prompt_tokens_details") or {}
    return int(ptd.get("cached_tokens") or uo.get("cache_read_input_tokens") or 0)


def row_effort(row: dict) -> str:
    """请求实际携带的思考强度（group_routing 钩子注入，经 metadata.spend_logs_metadata
    落库——LiteLLM 1.96.2 写库白名单不含 requester_metadata；requester_metadata/顶层
    仅作形态兜底）。历史日志为空。"""
    md = row.get("metadata") or {}
    sl = md.get("spend_logs_metadata") or {}
    rm = md.get("requester_metadata") or {}
    return str(sl.get("effort") or rm.get("effort") or md.get("effort") or "")


async def api_overview(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    logs, deps, sites, hs = await fetch_logs(), await litellm_deployments(), await onboard_sites(), wg_handshakes()
    today = logs_since(logs, 1)
    ok_rows = [r for r in today if r.get("status") != "failure"]
    totals = {
        "requests": len(today),
        "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in today),
        "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in today),
        "cached_tokens": sum(row_cached(r) for r in today),
        "errors": sum(1 for r in today if r.get("status") == "failure"),
    }
    site_rows = []
    for s in sites:
        ago = hs.get(s.get("pubkey") or "", -1)   # -1=未握手/不在 dump(wg 失败、peer 已摘),None 会让下方比较 TypeError→全表 500
        n_deps = sum(1 for d in deps if dep_of_site(d, s.get("wg_ip") or "#"))
        online = s["status"] in ("active", "partial") and 0 <= ago < HANDSHAKE_ONLINE
        site_rows.append({"name": s["name"], "wg_ip": s["wg_ip"], "handshake": ago,
                          "deployments": n_deps, "status": "online" if online else s["status"]})
    per_dep = {}
    for r in today:
        k = (r.get("model_id") or r.get("api_base") or "?", r.get("model_group") or "?")
        agg = per_dep.setdefault(k, {"requests": 0, "failures": 0})
        agg["requests"] += 1
        agg["failures"] += 1 if r.get("status") == "failure" else 0
    dep_rows = []
    for d in deps:
        k = ((d.get("model_info") or {}).get("id") or "?", d.get("model_name") or "?")
        agg = per_dep.get(k, {"requests": 0, "failures": 0})
        state = "无流量" if agg["requests"] == 0 else ("近期异常" if agg["failures"] > 0 else "健康")
        dep_rows.append({"model": d.get("model_name"),
                         "api_base": (d.get("litellm_params") or {}).get("api_base"),
                         "rpm": (d.get("model_info") or {}).get("rpm"),
                         "tpm": (d.get("model_info") or {}).get("tpm"),
                         "state": state, **agg})
    failures = [r for r in today if r.get("status") == "failure"]
    recent_errors = [{"time": (r.get("startTime") or "")[:19], "key": key_last4(r),
                      "model": r.get("model_group") or r.get("model") or "?", "detail": err_text(r)}
                     for r in failures[-5:]][::-1]
    online_sites = sum(1 for x in site_rows if x["status"] == "online")
    healthy_deps = sum(1 for x in dep_rows if x["state"] == "健康")
    return JSONResponse({"totals": totals,
                         "sites": {"online": online_sites, "total": len([s for s in sites if s["status"] != "revoked"]),
                                   "rows": site_rows},
                         "deployments": {"healthy": healthy_deps, "total": len(dep_rows), "rows": dep_rows},
                         "recent_errors": recent_errors,
                         "note": "近期错误仅覆盖已入账请求（鉴权失败不产生日志行）"})


async def api_usage(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        days = min(max(float(request.query_params.get("days", 1)), 0.02), 90)
    except ValueError:
        days = 1.0
    logs, keys = await fetch_logs(), await key_list_full()
    alias_of = {k.get("token"): k.get("key_alias") or "?" for k in keys}

    def ak_valid(ak: str) -> bool:
        # 只统计真实调用方：sha256 哈希（用户密钥）或 master 标识；失败鉴权的脏行（nope/invalid/None…）不入表
        return ak == "litellm_proxy_master_key" or (len(ak) == 64 and all(c in "0123456789abcdef" for c in ak))

    rows_map: dict[tuple, dict] = {}
    buckets: dict[str, dict] = {}          # 趋势图桶：小时(今天)/日期(多日)
    tft_sum, tft_n, dur_sum = 0, 0, 0
    for r in logs_since(logs, days):
        ak, model = str(r.get("api_key") or ""), r.get("model_group") or r.get("model") or "?"
        if not ak_valid(ak):
            continue
        agg = rows_map.setdefault((ak, model), {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                                "cached_tokens": 0, "duration_ms_sum": 0})
        agg["requests"] += 1
        agg["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
        agg["completion_tokens"] += int(r.get("completion_tokens") or 0)
        cached = row_cached(r)
        agg["cached_tokens"] += cached
        dur = int(r.get("request_duration_ms") or 0)
        agg["duration_ms_sum"] += dur
        dur_sum += dur
        tft = row_tft_ms(r)
        if tft:
            tft_sum += tft
            tft_n += 1
        dt = _aware_dt(r.get("startTime") or "")
        if dt:
            bkey = dt.astimezone(_CST).strftime("%H:00" if days <= 1 else "%m-%d")
            b = buckets.setdefault(bkey, {"reqs": 0, "in": 0, "out": 0, "cache": 0, "tft_sum": 0, "tft_n": 0})
            b["reqs"] += 1
            b["in"] += int(r.get("prompt_tokens") or 0)
            b["out"] += int(r.get("completion_tokens") or 0)
            b["cache"] += cached
            if tft:
                b["tft_sum"] += tft
                b["tft_n"] += 1
    rows = [{"key": ak[-4:],
             "alias": alias_of.get(ak) or ("管理员（master key）" if ak == "litellm_proxy_master_key" else "已删除密钥"),
             "model": model,
             "total_tokens": a["prompt_tokens"] + a["completion_tokens"] + a["cached_tokens"],
             "avg_ms": round(a["duration_ms_sum"] / a["requests"]) if a["requests"] else 0, **a}
            for (ak, model), a in sorted(rows_map.items())]
    failures = [r for r in logs_since(logs, days) if r.get("status") == "failure"]
    errors = [{"time": iso_to_cst(r.get("startTime") or ""), "key": key_last4(r),
               "model": r.get("model_group") or r.get("model") or "?", "detail": err_text(r)}
              for r in failures[-10:]][::-1]
    per_key = {}
    for row in rows:
        agg = per_key.setdefault(row["alias"], 0)
        per_key[row["alias"]] = agg + row["requests"]
    t = {"requests": sum(r["requests"] for r in rows),
         "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
         "completion_tokens": sum(r["completion_tokens"] for r in rows),
         "cached_tokens": sum(r["cached_tokens"] for r in rows)}
    t["total_tokens"] = t["prompt_tokens"] + t["completion_tokens"] + t["cached_tokens"]
    t["avg_tft"] = round(tft_sum / tft_n) if tft_n else 0
    t["avg_ms"] = round(dur_sum / t["requests"]) if t["requests"] else 0
    t["failures"] = len(failures)
    # 趋势桶（C 原型）：今天按 24 小时铺满，多日按日期铺满（空桶补零，前端直接画）
    if days <= 1:
        keys = [f"{h:02d}:00" for h in range(24)]
    else:
        base = datetime.now().astimezone(_CST)
        keys = [(base - timedelta(days=i)).strftime("%m-%d") for i in range(int(days) - 1, -1, -1)]
    hourly = []
    for k in keys:
        b = buckets.get(k) or {"reqs": 0, "in": 0, "out": 0, "cache": 0, "tft_sum": 0, "tft_n": 0}
        hourly.append({"label": k, "reqs": b["reqs"], "in": b["in"], "out": b["out"],
                       "cache": b["cache"], "avg_tft": round(b["tft_sum"] / b["tft_n"]) if b["tft_n"] else 0})
    return JSONResponse({"rows": rows, "errors": errors, "totals": t, "hourly": hourly,
                         "per_key": sorted(per_key.items(), key=lambda kv: -kv[1])})


async def api_usage_logs(request: Request) -> Response:
    """逐请求明细（US-P9 增补，参照 sub2api 日志视图）：时间/Key/模型/类型/token/延迟/状态，
    request_id + session_id 供排障。上限 500 行（/spend/logs 本身全量拉取，MVP 规模足够）。"""
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        days = min(max(float(request.query_params.get("days", 1)), 0.02), 90)
    except ValueError:
        days = 1.0
    logs, keys = await fetch_logs(), await key_list_full()
    alias_of = {k.get("token"): k.get("key_alias") or "?" for k in keys}

    def ak_valid(ak: str) -> bool:
        return ak == "litellm_proxy_master_key" or (len(ak) == 64 and all(c in "0123456789abcdef" for c in ak))

    out = []
    for r in logs_since(logs, days):
        ak = str(r.get("api_key") or "")
        if not ak_valid(ak):
            continue
        failed = r.get("status") == "failure"
        tft = row_tft_ms(r)
        out.append({
            "ts": iso_to_cst(r.get("startTime") or ""),
            "alias": alias_of.get(ak) or ("管理员（master key）" if ak == "litellm_proxy_master_key" else "已删除密钥"),
            "key": key_last4(r),
            "model": r.get("model_group") or r.get("model") or "?",
            "call_type": r.get("call_type") or "",
            "effort": row_effort(r),
            "prompt_tokens": int(r.get("prompt_tokens") or 0),
            "completion_tokens": int(r.get("completion_tokens") or 0),
            "cached_tokens": row_cached(r),
            "tft_ms": tft,
            "duration_ms": int(r.get("request_duration_ms") or 0),
            "status": "failure" if failed else "ok",
            "request_id": r.get("request_id") or "",
            "session_id": r.get("session_id") or "",
            # use_x_forwarded_for 时可能为 "client, proxy1, …" 链，取首跳
            "ip": str(r.get("requester_ip_address") or "").split(",")[0].strip(),
            "error": err_text(r) if failed else "",
        })
    out.sort(key=lambda r: r["ts"], reverse=True)
    return JSONResponse({"logs": out[:500], "count": len(out)})


# ---------------------------------------------------------------- 站点

async def api_sites(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    sites, hs, deps = await onboard_sites(), wg_handshakes(), await litellm_deployments()
    rows = []
    for s in sites:
        site_deps = [d for d in deps if dep_of_site(d, s.get("wg_ip") or "#")]
        tags = sorted({t for d in site_deps for t in dep_tags(d)}) if site_deps else \
               sorted({g for g in s["groups"] if g != "default"})
        ago = hs.get(s.get("pubkey") or "", -1)   # -1=未握手/不在 dump(wg 失败、peer 已摘),None 会让下方比较 TypeError→全表 500
        rows.append({
            "name": s["name"], "pubkey": (s.get("pubkey") or "")[:10] + "…=",
            "wg_ip": s["wg_ip"], "handshake": ago,
            "models": sorted({d.get("model_name") for d in site_deps}),
            "deps": [{"name": d.get("model_name"), "port": _dep_port(d),
                      "upstream": str((d.get("litellm_params") or {}).get("model") or "")
                                  .removeprefix("openai/")}
                     for d in site_deps],
            # 站点已知端口（deployment ∪ onboardd 登记簿）：前端「添加模型」的端口
            # 下拉用——手打端口易错（api_base 拼错即静默不可用），只允许选或显式自定义
            "known_ports": sorted({p for p in ({_dep_port(d) for d in site_deps} |
                                               {m.get("port") for m in s["models"]
                                                if isinstance(m, dict)})
                                   if isinstance(p, int) and p > 0}),
            "groups": tags, "status": s["status"],
            "online": s["status"] in ("active", "partial") and 0 <= ago < HANDSHAKE_ONLINE,
            "created_at": s.get("created_at"),
        })
    return JSONResponse({"sites": rows})


async def api_sites_token(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        site = body["site"]
        models = [{"name": m["name"], "port": int(m["port"]), "upstream_model": m.get("upstream_model")}
                  for m in body["models"]]
        groups = body.get("groups") or ["default"]
    except (ValueError, KeyError, TypeError):
        return jerr("bad request: expect {site, models:[{name,port}], groups?}", 400)
    if not NAME_RE.fullmatch(site or ""):
        return jerr("bad site name ([a-zA-Z0-9_-]{1,32})", 400)
    if not models or not all(MODEL_RE.fullmatch(m["name"] or "") and 1 <= m["port"] <= 65535 for m in models):
        return jerr("bad models (name + port required)", 400)
    bad = [g for g in groups if not GROUP_RE.fullmatch(g or "")]
    if bad:
        return jerr(f"bad group names: {bad}", 400)
    try:
        r = await onboard("POST", "/onboard/admin/tokens", json={"site": site, "models": models, "groups": groups})
    except httpx.HTTPError as exc:
        return jerr(f"onboardd unreachable: {exc}", 502)
    return JSONResponse(r.json(), status_code=r.status_code)


async def api_sites_revoke(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        site = (await request.json())["site"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {site}", 400)
    try:
        r = await onboard("POST", "/onboard/admin/revoke", json={"site": site})
    except httpx.HTTPError as exc:
        return jerr(f"onboardd unreachable: {exc}", 502)
    return JSONResponse(r.json(), status_code=r.status_code)


async def api_sites_groups(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        site, groups = body["site"], body["groups"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {site, groups:[]}", 400)
    sites = await onboard_sites()
    row = next((s for s in sites if s["name"] == site), None)
    if row is None:
        return jerr(f"unknown site {site}", 404)
    if row["status"] == "revoked":
        return jerr("site revoked", 400)
    bad = [g for g in groups if g != "default" and not GROUP_RE.fullmatch(g or "")]
    if bad:
        return jerr(f"bad group names: {bad}", 400)
    errors = await retag_site(row["wg_ip"], [g for g in groups if g != "default"])
    if errors:
        return jerr("; ".join(errors), 502)
    await sync_onboard_group(site, [g for g in groups if g != "default"])
    return JSONResponse({"ok": True, "site": site, "groups": [g for g in groups if g != "default"]})


async def _site_row_or_error(site: str) -> tuple[dict | None, JSONResponse | None]:
    """按名取 onboardd 登记的站点行；未知 404、已吊销 400（与 /sites/groups 同判）。"""
    sites = await onboard_sites()
    row = next((s for s in sites if s["name"] == site), None)
    if row is None:
        return None, jerr(f"unknown site {site}", 404)
    if row["status"] == "revoked":
        return None, jerr("site revoked", 400)
    return row, None


async def api_sites_probe(request: Request) -> Response:
    """探测站点某端口上游的 /v1/models，返回可用 model id（表单自动填充 / 刷新前预览用）。"""
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    site = request.query_params.get("site", "")
    try:
        port = int(request.query_params.get("port", ""))
    except ValueError:
        return jerr("bad port", 400)
    if not 1 <= port <= 65535:
        return jerr("bad port", 400)
    row, err = await _site_row_or_error(site)
    if err:
        return err
    models, perr = await fetch_upstream_models(row["wg_ip"], port)
    if perr:
        return jerr(perr, 502)
    return JSONResponse({"site": site, "port": port,
                         "api_base": f"http://{row['wg_ip']}:{port}/v1", "models": models})


async def api_sites_models(request: Request) -> Response:
    """手动向已注册站点添加模型（不经 install.sh 流程，典型场景：站点上新了一个模型）。
    payload 与 onboardd confirm 同口径；分组沿站点现状（deployment tags ∪ 登记簿分组）。"""
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        site, name, port = body["site"], body["name"], int(body["port"])
        upstream = (body.get("upstream_model") or "").strip() or None
    except (ValueError, KeyError, TypeError):
        return jerr("bad request: expect {site, name, port, upstream_model?}", 400)
    if not MODEL_RE.fullmatch(name or ""):
        return jerr("bad model name ([a-zA-Z0-9_.-]{1,64})", 400)
    if upstream is not None and not MODEL_RE.fullmatch(upstream):
        return jerr("bad upstream model id", 400)
    if not 1 <= port <= 65535:
        return jerr("bad port", 400)
    row, err = await _site_row_or_error(site)
    if err:
        return err
    deps = [d for d in await litellm_deployments() if dep_of_site(d, row["wg_ip"])]
    api_base = f"http://{row['wg_ip']}:{port}/v1"
    if any(d.get("model_name") == name and _dep_port(d) == port for d in deps):
        return jerr(f"{site} 已有 {name}:{port}（换上游 id 请用「刷新上游」）", 409)
    tags = sorted({t for d in deps for t in dep_tags(d)} |
                  {g for g in row["groups"] if g != "default"})
    params = _dep_params({}, {}, model=f"openai/{upstream or name}", api_base=api_base, tags=tags)
    code, rbody = await ll_json("POST", "/model/new", json={"model_name": name, "litellm_params": params})
    if code != 200:
        return JSONResponse(rbody if isinstance(rbody, dict) else {"error": str(rbody)[:300]},
                            status_code=code)
    new_dep = {"model_name": name,
               "litellm_params": {"model": f"openai/{upstream or name}", "api_base": api_base}}
    await sync_onboard_models(site, _site_models_from_deps(deps + [new_dep]))
    return JSONResponse({"ok": True, "site": site, "model": name, "port": port,
                         "upstream_model": upstream or name, "api_base": api_base})


async def _site_dep_or_error(site_row: dict, name: str, port: int):
    """定位站点某「对外名:端口」的 deployment（api_base 精确匹配，避免解析歧义）。"""
    api_base = f"http://{site_row['wg_ip']}:{port}/v1"
    deps = [d for d in await litellm_deployments() if dep_of_site(d, site_row["wg_ip"])]
    dep = next((d for d in deps if d.get("model_name") == name
                and (d.get("litellm_params") or {}).get("api_base") == api_base), None)
    if dep is None:
        return None, None, jerr(f"site {site_row['name']} 没有 {name}（端口 {port}）的 deployment", 404)
    return dep, deps, None


async def api_sites_models_refresh(request: Request) -> Response:
    """刷新 deployment 的上游 model id（站点换了模型、引擎/端口不变的场景）：
    对外名 / api_base / tags / 限流全保留，仅替换 litellm_params.model——
    先建新后删旧（与 retag 同一已验证模式，不留零 deployment 的 404 窗口），订阅方无感。"""
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        site, name, port = body["site"], body["name"], int(body["port"])
        upstream = (body.get("upstream_model") or "").strip()
    except (ValueError, KeyError, TypeError):
        return jerr("bad request: expect {site, name, port, upstream_model}", 400)
    if not MODEL_RE.fullmatch(upstream):
        return jerr("bad upstream model id", 400)
    row, err = await _site_row_or_error(site)
    if err:
        return err
    dep, deps, err = await _site_dep_or_error(row, name, port)
    if err:
        return err
    src = dep.get("litellm_params") or {}
    old_upstream = str(src.get("model") or "").removeprefix("openai/")
    if old_upstream == upstream:
        return JSONResponse({"ok": True, "unchanged": True, "site": site, "model": name,
                             "upstream_model": upstream})
    api_base = src.get("api_base")
    params = _dep_params(src, dep.get("model_info") or {}, model=f"openai/{upstream}",
                         api_base=api_base, tags=dep_tags(dep))
    code, rbody = await ll_json("POST", "/model/new", json={"model_name": name, "litellm_params": params})
    if code != 200:
        return JSONResponse(rbody if isinstance(rbody, dict) else {"error": str(rbody)[:300]},
                            status_code=code)
    code, rbody = await ll_json("POST", "/model/delete",
                                json={"id": (dep.get("model_info") or {}).get("id")})
    if code != 200:
        return jerr(f"新 deployment 已建但旧的下线失败（当前新旧并存），请重试删除：{str(rbody)[:150]}", 502)
    new_dep = {"model_name": name,
               "litellm_params": {"model": f"openai/{upstream}", "api_base": api_base}}
    await sync_onboard_models(site, _site_models_from_deps(
        [d for d in deps if d is not dep] + [new_dep]))
    return JSONResponse({"ok": True, "site": site, "model": name, "port": port,
                         "upstream_model": upstream, "previous": old_upstream})


async def api_sites_models_delete(request: Request) -> Response:
    """删除站点的单个模型 deployment（整站下线请用 /sites/revoke，会把 wg peer 一并摘除）。"""
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        site, name, port = body["site"], body["name"], int(body["port"])
    except (ValueError, KeyError, TypeError):
        return jerr("bad request: expect {site, name, port}", 400)
    row, err = await _site_row_or_error(site)
    if err:
        return err
    dep, deps, err = await _site_dep_or_error(row, name, port)
    if err:
        return err
    code, rbody = await ll_json("POST", "/model/delete",
                                json={"id": (dep.get("model_info") or {}).get("id")})
    if code != 200:
        return JSONResponse(rbody if isinstance(rbody, dict) else {"error": str(rbody)[:300]},
                            status_code=code)
    await sync_onboard_models(site, _site_models_from_deps([d for d in deps if d is not dep]))
    return JSONResponse({"ok": True, "site": site, "model": name, "port": port})


# ---------------------------------------------------------------- 分组

async def group_snapshot() -> dict:
    """分组现状：组名 -> {sites, keys}；default 为系统组（成员=全部未吊销站点）。"""
    sites, deps, keys = await onboard_sites(), await litellm_deployments(), await key_list_full()
    tags_of_ip = {}
    for d in deps:
        ip = str((d.get("litellm_params") or {}).get("api_base", "")).split("//")[-1].split(":")[0]
        tags_of_ip.setdefault(ip, set()).update(dep_tags(d))
    groups: dict[str, dict] = {}
    for s in sites:
        if s["status"] == "revoked":
            continue
        for g in (tags_of_ip.get(s["wg_ip"], set()) | {g for g in s["groups"] if g != "default"}):
            if g != "default":
                groups.setdefault(g, {"sites": set(), "keys": 0})["sites"].add(s["name"])
    for k in keys:
        g = ((k.get("metadata") or {}).get("group") or "default")
        if g != "default":
            groups.setdefault(g, {"sites": set(), "keys": 0})["keys"] += 1
    bound_default = sum(1 for k in keys if ((k.get("metadata") or {}).get("group") or "default") == "default")
    live = [s["name"] for s in sites if s["status"] != "revoked"]
    return {"groups": groups, "default": {"sites": set(live), "keys": bound_default},
            "sites": sites, "keys": keys}


async def api_groups(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    snap = await group_snapshot()
    rows = [{"name": "default", "system": True, "sites": sorted(snap["default"]["sites"]),
             "keys": snap["default"]["keys"]}]
    rows += [{"name": g, "system": False, "sites": sorted(v["sites"]), "keys": v["keys"]}
             for g, v in sorted(snap["groups"].items())]
    return JSONResponse({"groups": rows,
                         "sites": [{"name": s["name"], "wg_ip": s["wg_ip"], "status": s["status"]}
                                   for s in snap["sites"] if s["status"] != "revoked"]})


async def api_groups_create(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        name, members = body["name"], body.get("sites") or []
    except (ValueError, KeyError):
        return jerr("bad request: expect {name, sites:[]}", 400)
    if name == "default" or not GROUP_RE.fullmatch(name or ""):
        return jerr("bad group name ([a-zA-Z0-9_-]{1,32}, not default)", 400)
    if not members:
        return jerr("至少选择一个成员站点（空分组无承载对象）", 400)
    snap = await group_snapshot()
    if name in snap["groups"]:
        return jerr(f"group {name} already exists", 409)
    by_name = {s["name"]: s for s in snap["sites"]}
    unknown = [m for m in members if m not in by_name or by_name[m]["status"] == "revoked"]
    if unknown:
        return jerr(f"unknown or revoked sites: {unknown}", 400)
    errors = []
    for m in members:
        s = by_name[m]
        cur = await site_current_tags(s["wg_ip"])
        new_tags = sorted(cur | {name})
        errors += await retag_site(s["wg_ip"], new_tags)
        await sync_onboard_group(m, sorted(set(s["groups"]) - {"default"} | {name}))
    if errors:
        return jerr("; ".join(errors), 502)
    return JSONResponse({"ok": True, "name": name, "sites": members})


async def site_current_tags(wg_ip: str) -> set[str]:
    tags = set()
    for d in await litellm_deployments():
        if dep_of_site(d, wg_ip):
            tags.update(dep_tags(d))
    return tags


async def api_groups_rename(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        old, new = body["from"], body["to"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {from, to}", 400)
    if new == "default" or not GROUP_RE.fullmatch(new or ""):
        return jerr("bad group name", 400)
    snap = await group_snapshot()
    if old not in snap["groups"]:
        return jerr(f"unknown group {old}", 404)
    if new in snap["groups"]:
        return jerr(f"group {new} already exists", 409)
    errors = []
    for s in snap["sites"]:
        cur = await site_current_tags(s["wg_ip"])
        if old in cur:
            errors += await retag_site(s["wg_ip"], sorted((cur - {old}) | {new}))
            await sync_onboard_group(s["name"], sorted((set(s["groups"]) - {old}) | {new}))
    if errors:
        return jerr("; ".join(errors), 502)
    moved = 0
    for k in snap["keys"]:
        md = dict(k.get("metadata") or {})
        if md.get("group") == old:
            md["group"] = new
            code, body = await ll_json("POST", "/key/update", json={"key": k["token"], "metadata": md})
            moved += 1 if code == 200 else 0
    return JSONResponse({"ok": True, "from": old, "to": new, "keys_rebound": moved})


async def api_groups_delete(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        name = (await request.json())["name"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {name}", 400)
    if name == "default":
        return jerr("default 为系统组，不可删除", 400)
    snap = await group_snapshot()
    if name not in snap["groups"]:
        return jerr(f"unknown group {name}", 404)
    if snap["groups"][name]["keys"] > 0:
        return jerr(f"分组 {name} 仍有 {snap['groups'][name]['keys']} 把 Key 绑定，先改绑或删除这些 Key", 409)
    errors = []
    for s in snap["sites"]:
        cur = await site_current_tags(s["wg_ip"])
        if name in cur:
            errors += await retag_site(s["wg_ip"], sorted(cur - {name}))
            await sync_onboard_group(s["name"], sorted(set(s["groups"]) - {name}))
    if errors:
        return jerr("; ".join(errors), 502)
    return JSONResponse({"ok": True, "deleted": name})


# ---------------------------------------------------------------- 模型与别名

def _pairs(deps: list[dict]) -> set[tuple]:
    """一个模型名下全部 deployment 的 (api_base, 上游模型) 集合——别名判定用。"""
    return {((d.get("litellm_params") or {}).get("api_base") or "?",
             (d.get("litellm_params") or {}).get("model") or "?") for d in deps}


async def api_models(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    deps = await litellm_deployments()
    by_name: dict[str, list[dict]] = {}
    for d in deps:
        by_name.setdefault(d.get("model_name") or "?", []).append(d)
    pair_sets = {n: _pairs(ds) for n, ds in by_name.items()}
    alias_of = {}
    for n, ps in pair_sets.items():
        if ps and any(ps < ps2 for ps2 in pair_sets.values()):
            alias_of[n] = next(m for m, ps2 in pair_sets.items() if ps < ps2)
        elif len(pair_sets) > 1 and sum(1 for x in pair_sets.values() if x == ps) > 1:
            twins = sorted(m for m, x in pair_sets.items() if x == ps)
            base = next((m for m in twins if (by_name[m][0].get("litellm_params") or {}).get("model", "")
                         .removeprefix("openai/") == m), twins[0])
            for m in twins:
                if m != base:
                    alias_of[m] = base
    code, body = await ll_json("GET", "/v1/models", key=LITELLM_MASTER_KEY)
    models_out = []
    for n, ds in sorted(by_name.items()):
        info = ds[0].get("model_info") or {}
        models_out.append({
            "name": n, "type": "别名" if n in alias_of else "直选",
            "alias_target": alias_of.get(n),
            "rpm": info.get("rpm"), "tpm": info.get("tpm"),
            "deployments": [{"id": (d.get("model_info") or {}).get("id"),
                             "api_base": (d.get("litellm_params") or {}).get("api_base"),
                             "upstream": (d.get("litellm_params") or {}).get("model"),
                             "tags": dep_tags(d)} for d in ds],
        })
    return JSONResponse({"models": models_out,
                         "public_names": [m.get("id") for m in body.get("data", [])] if isinstance(body, dict) else [],
                         "routing": "least-busy（按在途请求数分流，故障冷却 60s）"})


async def api_models_alias(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        alias, target = body["alias"], body["target"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {alias, target}", 400)
    if not MODEL_RE.fullmatch(alias or ""):
        return jerr("bad alias name ([a-zA-Z0-9_.-]{1,64})", 400)
    deps = await litellm_deployments()
    by_name: dict[str, list[dict]] = {}
    for d in deps:
        by_name.setdefault(d.get("model_name") or "?", []).append(d)
    if alias in by_name:
        return jerr(f"model name {alias} already exists", 409)
    if target not in by_name:
        return jerr(f"unknown target model {target}", 404)
    created, errors = 0, []
    for d in by_name[target]:
        src, info = d.get("litellm_params") or {}, d.get("model_info") or {}
        params = {"model": src.get("model"), "api_base": src.get("api_base"), "api_key": "none",
                  "tags": dep_tags(d), "allowed_openai_params": PASS_THROUGH_OPENAI_PARAMS,
                  "connect_timeout": src.get("connect_timeout", 5),
                  "timeout": src.get("timeout", 600)}
        for lim in ("rpm", "tpm"):
            if src.get(lim) is not None or info.get(lim) is not None:
                params[lim] = src.get(lim) or info.get(lim)
        code, rbody = await ll_json("POST", "/model/new", json={
            "model_name": alias, "litellm_params": params})
        created += 1 if code == 200 else 0
        if code != 200:
            errors.append(str(rbody)[:150])
    if errors and created == 0:
        return jerr("; ".join(errors), 502)
    return JSONResponse({"ok": True, "alias": alias, "target": target, "deployments": created,
                         "errors": errors})


# ---------------------------------------------------------------- 用户 Key

def key_row(k: dict) -> dict:
    meta = k.get("metadata") or {}
    return {"token": k.get("token"), "alias": k.get("key_alias") or "(未命名)",
            "key_last4": "…" + str(k.get("key_name") or k.get("token") or "")[-4:],
            "group": meta.get("group") or "default",
            "models": k.get("models") or [],
            "blocked": bool(k.get("blocked")),
            "expires": k.get("expires"),
            "created_at": (k.get("created_at") or "")[:19],
            "last_active": (k.get("last_active") or "")[:19]}


async def api_keys(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    return JSONResponse({"keys": [key_row(k) for k in await key_list_full()]})


async def api_keys_create(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        alias, group, models = body.get("alias") or "", body.get("group") or "default", body.get("models") or []
    except ValueError:
        return jerr("bad request", 400)
    payload: dict = {"metadata": {"group": group}}
    if alias:
        payload["key_alias"] = alias
    if models:
        payload["models"] = models
    code, rbody = await ll_json("POST", "/key/generate", json=payload)
    if code != 200:
        return JSONResponse(rbody if isinstance(rbody, dict) else {"error": str(rbody)[:300]}, status_code=code)
    plaintext = (rbody or {}).get("key") or (rbody or {}).get("token") or ""
    if plaintext:
        vault_store(hashlib.sha256(plaintext.encode()).hexdigest(), plaintext)
    return JSONResponse({"ok": True,
                         "key": plaintext,
                         "alias": alias or "(未命名)", "group": group, "models": models,
                         "note": "已同时存入加密保险库，可随时在「使用」中查看"})


async def api_keys_reveal(request: Request) -> Response:
    """管理员查看 Key 明文（保险库解密）。旧 Key（保险库启用前创建）只有哈希，404。"""
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        token = (await request.json())["key"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {key}", 400)
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        return jerr("bad key hash", 400)
    plaintext = vault_get(token)
    if not plaintext:
        return jerr("该 Key 明文不在保险库（创建于保险库启用前或密钥已轮换），请禁用后重新签发", 404)
    return JSONResponse({"key": plaintext})


async def api_keys_update(request: Request) -> Response:
    """列表页行内改分组 / 编辑弹窗改名与白名单（LiteLLM /key/update，key 传哈希）。"""
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        token = body["key"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {key}", 400)
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        return jerr("bad key hash", 400)
    payload: dict = {"key": token}
    if "alias" in body:
        alias = (body.get("alias") or "").strip()
        if alias:
            if len(alias) > 64:
                return jerr("bad alias", 400)
            payload["key_alias"] = alias
    if "group" in body:
        group = body.get("group") or "default"
        if not GROUP_RE.fullmatch(group):
            return jerr("bad group name", 400)
        payload["metadata"] = {"group": group}
    if "models" in body:
        models = body.get("models") or []
        if not isinstance(models, list) or any(not NAME_RE.fullmatch(str(m) or "") for m in models):
            return jerr("bad models list", 400)
        payload["models"] = models
    if len(payload) == 1:
        return jerr("nothing to update", 400)
    code, rbody = await ll_json("POST", "/key/update", json=payload)
    if code != 200:
        return JSONResponse(rbody if isinstance(rbody, dict) else {"error": str(rbody)[:300]}, status_code=code)
    return JSONResponse({"ok": True, "note": "分组路由即时生效"})


async def api_keys_toggle(request: Request, block: bool) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        token = (await request.json())["key"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {key}", 400)
    path = "/key/block" if block else "/key/unblock"
    code, rbody = await ll_json("POST", path, json={"key": token})
    return JSONResponse(rbody if isinstance(rbody, dict) else {"error": str(rbody)[:300]}, status_code=code)


async def api_keys_delete(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        token = (await request.json())["key"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {key}", 400)
    code, rbody = await ll_json("POST", "/key/delete", json={"keys": [token]})
    return JSONResponse(rbody if isinstance(rbody, dict) else {"error": str(rbody)[:300]}, status_code=code)


async def api_keys_block(request: Request) -> Response:
    return await api_keys_toggle(request, True)


async def api_keys_unblock(request: Request) -> Response:
    return await api_keys_toggle(request, False)


# ---------------------------------------------------------------- 用户自查（user 角色）

def _my_mcp_usage(key_hash: str) -> dict:
    """按会话 Key 哈希直读 mcp-hub 用量账本（usage 库只存 16 位哈希前缀），
    不再持完整 Key 回调 mcp-hub。"""
    try:
        with sqlite3.connect(f"file:{MCP_USAGE_DB}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT tool, COUNT(*) FROM usage WHERE key_hash = ? GROUP BY tool",
                              (key_hash[:16],)).fetchall()
    except sqlite3.Error:
        return {"tools": {}, "total": 0}
    tools = {tool: n for tool, n in rows}
    return {"tools": tools, "total": sum(tools.values())}


async def api_my(request: Request) -> Response:
    sess = await require(request, role="any")
    if isinstance(sess, JSONResponse):
        return sess
    # master key 不在 LiteLLM key 表中，其用量以调用日志中的 litellm_proxy_master_key
    # 标识聚合；用户虚拟 Key 经 master key 拉全量 Key 表按哈希对账（会话不持完整 Key）
    if sess["role"] == "admin":
        alias, group, models = "管理员（master key）", "—", []
        created, expires, match_key = "", None, "litellm_proxy_master_key"
        mcp, mcp_note = {"tools": {}, "total": 0}, "master key 不经 MCP 通道（MCP 端点仅接受用户虚拟 Key）"
    else:
        info = next((k for k in await key_list_full() if k.get("token") == sess["key_hash"]), None)
        if info is None:
            return jerr("用量查询失败：密钥已删除或状态异常，请重新登录", 502)
        alias = info.get("key_alias") or "（未命名）"
        group = (info.get("metadata") or {}).get("group") or "default"
        models = info.get("models") or []
        created = (info.get("created_at") or "")[:19]
        expires = info.get("expires")
        match_key = sess["key_hash"]
        mcp, mcp_note = _my_mcp_usage(sess["key_hash"]), ""
    today_tokens = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    today_models: dict[str, dict] = {}
    for row in logs_since(await fetch_logs(), 1):
        if str(row.get("api_key") or "") != match_key:
            continue
        cached = row_cached(row)
        today_tokens["requests"] += 1
        today_tokens["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
        today_tokens["completion_tokens"] += int(row.get("completion_tokens") or 0)
        today_tokens["cached_tokens"] += cached
        model = row.get("model_group") or row.get("model") or "?"
        agg = today_models.setdefault(model, {"model": model, "requests": 0, "prompt_tokens": 0,
                                              "completion_tokens": 0, "cached_tokens": 0})
        agg["requests"] += 1
        agg["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
        agg["completion_tokens"] += int(row.get("completion_tokens") or 0)
        agg["cached_tokens"] += cached
    return JSONResponse({
        "role": sess["role"],
        "alias": alias,
        "key_last4": "…" + sess["key_last4"] if sess["key_last4"] else "—",
        "group": group,
        "models": models,
        "created_at": created,
        "expires": expires,
        "today": today_tokens,
        "today_models": sorted(today_models.values(), key=lambda m: -m["requests"]),
        "mcp": mcp,
        "mcp_note": mcp_note,
    })


# ---------------------------------------------------------------- MCP 管理面

def mask_entry(e: dict) -> dict:
    ak = e.get("api_key") or ""
    return {"name": e.get("name"), "url": e.get("url"),
            "api_key_last4": f"…{ak[-4:]}" if len(ak) >= 8 else (ak or "未配置"),
            "prefix": e.get("prefix", f"{e.get('name')}_")}


def read_mcp_conf() -> list[dict]:
    try:
        data = json.loads(EXTERNAL_MCP_CONF.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def write_mcp_conf(entries: list[dict]) -> None:
    EXTERNAL_MCP_CONF.parent.mkdir(parents=True, exist_ok=True)
    EXTERNAL_MCP_CONF.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
    os.chmod(EXTERNAL_MCP_CONF, 0o600)


def restart_mcp_hub() -> str:
    r = subprocess.run(MCP_RESTART_CMD, capture_output=True, text=True, timeout=60)
    return "ok" if r.returncode == 0 else r.stderr.strip()[:200]


async def api_mcp(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    return JSONResponse({
        "builtin": {"tool": "analyze_image(image_url, question)", "model": MCP_VISION_MODEL,
                    "endpoint": "/mcp（Streamable HTTP + Bearer）",
                    "boundary": "以调用者 Key 回调回环模型通道，凭据不出网关（C5）"},
        "external": [mask_entry(e) for e in read_mcp_conf()],
        "upload": {"endpoint": "POST /mcp/upload（multipart file=，同一 Key）",
                   "ttl": "30 分钟", "limit": "≤10MB，jpg/png/webp/gif"},
    })


async def api_mcp_register(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        body = await request.json()
        name, url, api_key = body["name"], body["url"], body.get("api_key") or ""
        prefix = body.get("prefix") or f"{name}_"
    except (ValueError, KeyError):
        return jerr("bad request: expect {name, url, api_key?, prefix?}", 400)
    if not NAME_RE.fullmatch(name or ""):
        return jerr("bad name ([a-zA-Z0-9_-]{1,32})", 400)
    if not url.startswith(("http://", "https://")):
        return jerr("bad url", 400)
    if not re.fullmatch(r"[a-z0-9_]{1,16}", prefix or ""):
        return jerr("bad prefix ([a-z0-9_]{1,16})", 400)
    entries = read_mcp_conf()
    if any(e["name"] == name for e in entries):
        return jerr(f"mcp {name} already registered", 409)
    entries.append({"name": name, "url": url, "api_key": api_key, "prefix": prefix})
    write_mcp_conf(entries)
    return JSONResponse({"ok": True, "restart": restart_mcp_hub(),
                         "note": "已重启 mcp-hub 生效；进行中的工具调用会被中断"})


async def api_mcp_remove(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        name = (await request.json())["name"]
    except (ValueError, KeyError):
        return jerr("bad request: expect {name}", 400)
    entries = read_mcp_conf()
    kept = [e for e in entries if e["name"] != name]
    if len(kept) == len(entries):
        return jerr(f"mcp {name} not registered", 404)
    write_mcp_conf(kept)
    return JSONResponse({"ok": True, "restart": restart_mcp_hub()})


async def api_mcp_tools(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    tools = [{"name": "analyze_image", "source": "内建",
              "description": f"视觉识别（{MCP_VISION_MODEL}）"}]
    for e in read_mcp_conf():
        prefix = e.get("prefix", f"{e['name']}_")
        try:
            from fastmcp import Client
            from fastmcp.client.auth import BearerAuth
            async with Client(e["url"], auth=BearerAuth(e["api_key"]) if e.get("api_key") else None,
                              timeout=15) as client:
                listed = await client.list_tools()
            tools += [{"name": f"{prefix}{t.name}", "source": e["name"],
                       "description": (t.description or "")[:120]} for t in listed]
        except Exception as exc:  # 单个外部服务不可达不阻断整体预览
            tools.append({"name": f"{prefix}*", "source": e["name"],
                          "description": f"不可达：{str(exc)[:120]}"})
    return JSONResponse({"tools": tools})


async def api_mcp_usage(request: Request) -> Response:
    sess = await require(request)
    if isinstance(sess, JSONResponse):
        return sess
    try:
        days = min(max(float(request.query_params.get("days", 1)), 0.02), 90)
    except ValueError:
        days = 1.0
    cutoff = time.time() - days * 86400
    try:
        with sqlite3.connect(f"file:{MCP_USAGE_DB}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT key_hash, tool, ts FROM usage WHERE ts >= ? ORDER BY ts",
                              (int(cutoff),)).fetchall()
    except sqlite3.Error as exc:
        return jerr(f"usage db unreadable: {exc}", 500)
    alias_of = {k.get("token"): k.get("key_alias") or "?" for k in await key_list_full()}
    agg: dict[tuple, int] = {}
    for kh, tool, _ts in rows:
        agg[(kh, tool)] = agg.get((kh, tool), 0) + 1
    keys_map: dict[str, dict] = {}
    for (kh, tool), n in agg.items():
        entry = keys_map.setdefault(kh, {"alias": match_alias(kh, alias_of), "tools": {}})
        entry["tools"][tool] = n
    return JSONResponse({"keys": [{"key_hash": kh, **v, "total": sum(v["tools"].values())}
                                  for kh, v in keys_map.items()]})


def match_alias(short_hash: str, alias_of: dict) -> str:
    """usage.db 只存 sha256 前 16 位，用前缀匹配回找 Key 别名。"""
    for full, alias in alias_of.items():
        if str(full).startswith(short_hash):
            return alias
    return f"…{short_hash[-4:]}"


# ---------------------------------------------------------------- 组装

async def console_redirect(request: Request) -> Response:
    return RedirectResponse("/console/", status_code=307)


# 未登录可取的静态资源（登录页本体 + 样式 + 图标）；其余页面源码一律会话门禁，
# 避免内部拓扑/组件名/策略文案经公开 URL 外泄（安全收敛，对应评审意见「对外尽量少暴露内部信息」）
PUBLIC_STATIC = {"login.html", "admin-login.html", "assets/portal.css", "favicon.ico",
                 "manifest.webmanifest"}
# 品牌图标族整目录公开：favicon / manifest / PWA 图标在登录页就需要加载（issue #41）
PUBLIC_STATIC_PREFIX = "assets/brand/"


async def console_static(request: Request) -> Response:
    rel = request.path_params.get("rest", "").lstrip("/")
    if rel == "":
        rel = "index.html"
    if (rel not in PUBLIC_STATIC and not rel.startswith(PUBLIC_STATIC_PREFIX)
            and session_of(request) is None):
        return RedirectResponse("/console/login.html", status_code=302)
    target = (STATIC_DIR / rel).resolve()
    root = STATIC_DIR.resolve()
    if root not in target.parents or not target.is_file():
        return jerr("not found", 404)
    # no-store：控制台页面/JS 迭代频繁且用户极少，直接不缓存，每次导航拿新文件。
    # 踩坑记录（2026-08-15）：无 Cache-Control 时浏览器启发式缓存旧页面；改 no-cache
    # 后，修复前已缓存的条目不带该指令仍可能被沿用——过渡期用户继续拿到旧代码，
    # 故最终采用 no-store 一劳永逸
    return FileResponse(target, headers={"Cache-Control": "no-store"})


api_routes = [
    Route("/console/api/login", api_login, methods=["POST"]),
    Route("/console/api/admin-login/state", api_admin_login_state, methods=["GET"]),
    Route("/console/api/admin-login", api_admin_login, methods=["POST"]),
    Route("/console/api/2fa", api_2fa, methods=["GET"]),
    Route("/console/api/2fa/setup", api_2fa_setup, methods=["POST"]),
    Route("/console/api/2fa/confirm", api_2fa_confirm, methods=["POST"]),
    Route("/console/api/2fa/disable", api_2fa_disable, methods=["POST"]),
    Route("/console/api/logout", api_logout, methods=["POST"]),
    Route("/console/api/me", api_me, methods=["GET"]),
    Route("/console/api/overview", api_overview, methods=["GET"]),
    Route("/console/api/usage", api_usage, methods=["GET"]),
    Route("/console/api/usage/logs", api_usage_logs, methods=["GET"]),
    Route("/console/api/sites", api_sites, methods=["GET"]),
    Route("/console/api/sites/token", api_sites_token, methods=["POST"]),
    Route("/console/api/sites/revoke", api_sites_revoke, methods=["POST"]),
    Route("/console/api/sites/groups", api_sites_groups, methods=["POST"]),
    Route("/console/api/sites/probe", api_sites_probe, methods=["GET"]),
    Route("/console/api/sites/models", api_sites_models, methods=["POST"]),
    Route("/console/api/sites/models/refresh", api_sites_models_refresh, methods=["POST"]),
    Route("/console/api/sites/models/delete", api_sites_models_delete, methods=["POST"]),
    Route("/console/api/groups", api_groups, methods=["GET"]),
    Route("/console/api/groups/create", api_groups_create, methods=["POST"]),
    Route("/console/api/groups/rename", api_groups_rename, methods=["POST"]),
    Route("/console/api/groups/delete", api_groups_delete, methods=["POST"]),
    Route("/console/api/models", api_models, methods=["GET"]),
    Route("/console/api/models/alias", api_models_alias, methods=["POST"]),
    Route("/console/api/keys", api_keys, methods=["GET"]),
    Route("/console/api/keys/create", api_keys_create, methods=["POST"]),
    Route("/console/api/keys/reveal", api_keys_reveal, methods=["POST"]),
    Route("/console/api/keys/update", api_keys_update, methods=["POST"]),
    Route("/console/api/keys/block", api_keys_block, methods=["POST"]),
    Route("/console/api/keys/unblock", api_keys_unblock, methods=["POST"]),
    Route("/console/api/keys/delete", api_keys_delete, methods=["POST"]),
    Route("/console/api/my", api_my, methods=["GET"]),
    Route("/console/api/mcp", api_mcp, methods=["GET"]),
    Route("/console/api/mcp/register", api_mcp_register, methods=["POST"]),
    Route("/console/api/mcp/remove", api_mcp_remove, methods=["POST"]),
    Route("/console/api/mcp/tools", api_mcp_tools, methods=["GET"]),
    Route("/console/api/mcp/usage", api_mcp_usage, methods=["GET"]),
]

app = Starlette(routes=api_routes + [
    Route("/console", console_redirect),
    Route("/console/{rest:path}", console_static, methods=["GET", "HEAD"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("CONSOLE_PORT", "8300")), log_level="info")
