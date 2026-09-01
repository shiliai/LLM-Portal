"""onboardd：站点注册 API（设计 §3.4，US-P7/P8）。

对外（经 nginx /onboard/*）：
  GET  /onboard/install?token=...      渲染下发 install.sh（站点一行执行）
  POST /onboard/register {token, pubkey}     验 token → wg 加 peer → 返回站点 wg0.conf
  POST /onboard/confirm  {token, ok, results} 自检全绿 → LiteLLM /model/new 注册 deployment

本机管理（site-add/site-revoke/site-list/consoled 调用，需 x-admin-token）：
  POST /onboard/admin/tokens {site, models, groups}   签发一次性 token（15min）
  POST /onboard/admin/revoke {site}                   吊销：wg 摘 peer + LiteLLM 摘 deployment
  POST /onboard/admin/groups {site, groups}           r6：站点分组同步（LiteLLM retag 由 consoled 负责）
  POST /onboard/admin/models {site, models}           站点模型清单同步（手动加/刷新/删后回写，
                                                      LiteLLM 侧操作由 consoled 完成）
  GET  /onboard/admin/list                            站点清单

状态存 SQLite；peer 持久化回写 /etc/wireguard/wg0.conf。wg 命令前缀 WG_EXEC：
默认宿主机直跑；容器化部署（#7）由 compose 注入 `docker exec private-llm-wireguard wg`
（wg0 在宿主机网络命名空间，由 wireguard sidecar 托管）。
"""

import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

DOMAIN = os.environ.get("DOMAIN", "llm-portal.example.com")
# offload 模式（TLS 在上游设备终结）：对外基地址通常带高位端口（https://域名:8080），
# 站点 install/register 回调地址用它；站点 wg Endpoint 指向网关可达地址（内网部署=内网 IP，
# 上游反代不转发 UDP）。standalone/external 两模式下三者一致，均无需设置。
# rstrip：容忍 .env 里 PUBLIC_BASE 末尾误带 /（否则 install_command 拼出 //onboard/…）
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", f"https://{DOMAIN}").rstrip("/")
WG_ENDPOINT_HOST = os.environ.get("WG_ENDPOINT_HOST", DOMAIN)
WG_CONF = Path(os.environ.get("WG_CONF", "/etc/wireguard/wg0.conf"))
WG_IFACE = os.environ.get("WG_IFACE", "wg0")
SITE_WG_IFACE = os.environ.get("SITE_WG_IFACE", "wg0")
for _iface_name, _iface_value in (("WG_IFACE", WG_IFACE), ("SITE_WG_IFACE", SITE_WG_IFACE)):
    if not re.fullmatch(r"[a-zA-Z0-9_=+.-]{1,15}", _iface_value):
        raise RuntimeError(f"invalid {_iface_name}: {_iface_value!r}")
WG_EXEC = shlex.split(os.environ.get("WG_EXEC", "wg"))
WG_PORT = os.environ.get("WG_PORT", "51820")
WG_SUBNET_PREFIX = os.environ.get("WG_SUBNET_PREFIX", "10.77.0")  # 站点 IP 从 .11 递增
WG_ALLOWED = f"{WG_SUBNET_PREFIX}.0/24"                          # 站点侧 AllowedIPs（整段进隧道）
GW_WG_IP = os.environ.get("WG_VPS_IP", "10.77.0.1")             # 网关自身 wg 地址（install.sh 自检 ping）
LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:4000")
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
# issue #46：drop_params=true 下通用 openai/ deployment 的 supported 参数列表不含
# reasoning_effort，会被静默丢弃而 vLLM 上游实际支持——注册时统一直通（实测其余
# 思考参数 enable_thinking/reasoning 走 extra_body 本就不丢，无需列入）
ALLOWED_OPENAI_PARAMS = ["reasoning_effort"]
ADMIN_TOKEN = os.environ["ONBOARD_ADMIN_TOKEN"]
TOKEN_TTL = 900  # 15 分钟
DATA_DIR = Path(os.environ.get("ONBOARDD_DATA", "/var/lib/private-llm/onboardd"))
DB_PATH = DATA_DIR / "onboardd.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)

VPS_PUBLIC_KEY_PATH = Path(os.environ.get("VPS_PUBLIC_KEY_PATH", str(DATA_DIR) + "/../wireguard-public.key"))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sites (
                name TEXT PRIMARY KEY, pubkey TEXT UNIQUE, wg_ip TEXT UNIQUE,
                models TEXT, groups TEXT, status TEXT, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY, site TEXT, models TEXT, groups TEXT,
                wg_ip TEXT, expires_at INTEGER, used INTEGER DEFAULT 0);
            """
        )


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def vps_public_key() -> str:
    key = VPS_PUBLIC_KEY_PATH.read_text().strip()
    if not key:
        raise RuntimeError(f"missing VPS wireguard public key at {VPS_PUBLIC_KEY_PATH}")
    return key


def next_wg_ip() -> str:
    with db() as conn:
        used = {row["wg_ip"] for row in conn.execute("SELECT wg_ip FROM sites UNION SELECT wg_ip FROM tokens")}
    for i in range(11, 251):
        ip = f"{WG_SUBNET_PREFIX}.{i}"
        if ip not in used:
            return ip
    raise RuntimeError("no free wireguard ip")


# ---------------------------------------------------------------- 公网入口（token 保护）

async def install(request: Request) -> Response:
    token = request.query_params.get("token", "")
    with db() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
    if row is None or row["used"] or row["expires_at"] < time.time():
        return PlainTextResponse("invalid, expired or used token\n", status_code=403)
    ports = " ".join(str(m["port"]) for m in json.loads(row["models"]))
    script = (INSTALL_SH.format(token=token, endpoint=PUBLIC_BASE)
              .replace("__VPS_PUBLIC_KEY__", vps_public_key())
              .replace("__WG_ENDPOINT__", f"{WG_ENDPOINT_HOST}:{WG_PORT}")
              .replace("__WG_ALLOWED__", WG_ALLOWED)
              .replace("__GW_WG_IP__", GW_WG_IP)
              .replace("__SITE_WG_IFACE__", SITE_WG_IFACE)
              .replace("__MODEL_PORTS__", ports))
    return PlainTextResponse(script, media_type="text/x-shellscript")


async def register(request: Request) -> Response:
    body = await request.json() if request.method == "POST" else dict(request.query_params)
    token, pubkey = body.get("token", ""), body.get("pubkey", "")
    if not re.fullmatch(r"[A-Za-z0-9+/]{43}=", pubkey or ""):
        return JSONResponse({"error": "invalid pubkey (expect wg base64)"}, status_code=400)
    with db() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
        if row is None or row["used"] or row["expires_at"] < time.time():
            return JSONResponse({"error": "invalid, expired or used token"}, status_code=403)
        wg_ip = row["wg_ip"]
        # 站点重装场景：同 token 重复注册允许更新公钥（token 尚未确认即未 used）
        existing = conn.execute("SELECT * FROM sites WHERE wg_ip=?", (wg_ip,)).fetchone()
        if existing and existing["pubkey"] != pubkey:
            r = run(WG_EXEC + ["set", WG_IFACE, "peer", existing["pubkey"], "remove"])
            if r.returncode != 0:
                return JSONResponse({"error": f"wg remove old peer failed: {r.stderr.strip()}"}, status_code=500)
            remove_peer_from_conf(existing["pubkey"])
            conn.execute("DELETE FROM sites WHERE wg_ip=?", (wg_ip,))
        conn.execute("UPDATE tokens SET used=1 WHERE token=?", (token,))
    r = run(WG_EXEC + ["set", WG_IFACE, "peer", pubkey, "allowed-ips", f"{wg_ip}/32"])
    if r.returncode != 0:
        return JSONResponse({"error": f"wg set peer failed: {r.stderr.strip()}"}, status_code=500)
    append_peer_to_conf(pubkey, wg_ip, row["site"])
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sites (name, pubkey, wg_ip, models, groups, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (row["site"], pubkey, wg_ip, row["models"], row["groups"], "registered", int(time.time())),
        )
    conf = (
        "[Interface]\n"
        f"PrivateKey = <SITE_PRIVATE_KEY>  # 站点私钥仅在本机，install.sh 已写好\n"
        f"Address = {wg_ip}/24\n"
        "MTU = 1280                    # 跨境链路大 UDP 包丢弃率高，1280 实测吞吐 3-6 倍于默认 1420\n"
        "[Peer]\n"
        f"PublicKey = {vps_public_key()}\n"
        f"Endpoint = {WG_ENDPOINT_HOST}:{WG_PORT}\n"
        f"AllowedIPs = {WG_ALLOWED}\n"
        "PersistentKeepalive = 25\n"
    )
    return JSONResponse({"wg_ip": wg_ip, "wg_config": conf})


async def confirm(request: Request) -> Response:
    body = await request.json()
    token, ok = body.get("token", ""), bool(body.get("ok"))
    results = body.get("results", {})
    with db() as conn:
        trow = conn.execute("SELECT * FROM tokens WHERE token=?", (token,)).fetchone()
        srow = conn.execute("SELECT * FROM sites WHERE name=?", (trow["site"],)).fetchone() if trow else None
    if srow is None or srow["status"] != "registered":
        return JSONResponse({"error": "site not in registered state"}, status_code=400)
    if not ok:
        with db() as conn:
            conn.execute("UPDATE sites SET status='failed' WHERE name=?", (srow["name"],))
        return JSONResponse({"registered": False, "reason": "self-check failed", "results": results})
    # 分组即 tag（US-P13）：default 组 = 隐式全量池（不打 tag）；
    # 只把非 default 分组名写入 deployment tags（1.96.2 的 default tag 是兜底池语义，不能用于分组隔离）
    groups = [g for g in (json.loads(srow["groups"]) or ["default"]) if g != "default"]
    models = json.loads(srow["models"])
    registered, errors = [], []
    async with httpx.AsyncClient(timeout=30) as client:
        for m in models:
            payload = {
                "model_name": m["name"],
                "litellm_params": {
                    "model": f"openai/{m.get('upstream_model') or m['name']}",
                    "api_base": f"http://{srow['wg_ip']}:{m['port']}/v1",
                    "api_key": "none",
                    "tags": groups,
                    "allowed_openai_params": ALLOWED_OPENAI_PARAMS,
                    "connect_timeout": 5,
                    "timeout": 600,
                },
            }
            r = await client.post(
                f"{LITELLM_BASE}/model/new",
                json=payload,
                headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
            )
            (registered if r.status_code == 200 else errors).append(
                {"model": m["name"], "detail": r.json() if r.status_code == 200 else r.text[:200]}
            )
    status = "active" if not errors else "partial"
    with db() as conn:
        conn.execute("UPDATE sites SET status=? WHERE name=?", (status, srow["name"]))
        conn.execute("DELETE FROM tokens WHERE token=?", (token,))
    return JSONResponse({"registered": not errors, "models": registered, "errors": errors, "status": status})


# ---------------------------------------------------------------- 本机管理 API

def require_admin(request: Request) -> bool:
    return request.headers.get("x-admin-token", "") == ADMIN_TOKEN


async def admin_token(request: Request) -> Response:
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    site = body["site"]
    models = [{"name": m["name"], "port": int(m["port"]), "upstream_model": m.get("upstream_model")} for m in body["models"]]
    groups = body.get("groups") or ["default"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", site):
        return JSONResponse({"error": "bad site name"}, status_code=400)
    with db() as conn:
        if conn.execute("SELECT 1 FROM sites WHERE name=? AND status IN ('active','partial')", (site,)).fetchone():
            return JSONResponse({"error": f"site {site} already exists (revoke first)"}, status_code=409)
        conn.execute("DELETE FROM tokens WHERE site=? AND used=0", (site,))
        token = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO tokens (token, site, models, groups, wg_ip, expires_at) VALUES (?,?,?,?,?,?)",
            (token, site, json.dumps(models), json.dumps(groups), next_wg_ip(), int(time.time()) + TOKEN_TTL),
        )
    return JSONResponse({
        "token": token,
        "expires_in": TOKEN_TTL,
        "install_command": f'curl -fsSL "{PUBLIC_BASE}/onboard/install?token={token}" | sudo bash',
    })


async def admin_revoke(request: Request) -> Response:
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    site = (await request.json())["site"]
    with db() as conn:
        row = conn.execute("SELECT * FROM sites WHERE name=?", (site,)).fetchone()
    if row is None:
        return JSONResponse({"error": f"unknown site {site}"}, status_code=404)
    outputs = {}
    r = run(WG_EXEC + ["set", WG_IFACE, "peer", row["pubkey"], "remove"])
    outputs["wg_remove"] = "ok" if r.returncode == 0 else r.stderr.strip()
    remove_peer_from_conf(row["pubkey"])
    deleted = []
    async with httpx.AsyncClient(timeout=30) as client:
        info = await client.get(f"{LITELLM_BASE}/model/info", headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"})
        for dep in info.json().get("data", []):
            if str(dep.get("litellm_params", {}).get("api_base", "")).startswith(f"http://{row['wg_ip']}:"):
                r = await client.post(
                    f"{LITELLM_BASE}/model/delete",
                    json={"id": dep["model_info"]["id"]},
                    headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
                )
                deleted.append({"model": dep.get("model_name"), "ok": r.status_code == 200})
    outputs["deployments_deleted"] = deleted
    with db() as conn:
        conn.execute("UPDATE sites SET status='revoked' WHERE name=?", (site,))
    return JSONResponse({"site": site, "status": "revoked", "detail": outputs})


async def admin_list(request: Request) -> Response:
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    with db() as conn:
        rows = conn.execute("SELECT name, pubkey, wg_ip, models, groups, status, created_at FROM sites ORDER BY created_at").fetchall()
    return JSONResponse({"sites": [dict(row) for row in rows]})


async def admin_groups(request: Request) -> Response:
    """r6：控制台调整站点分组后同步注册表（LiteLLM 侧 retag 由 consoled 完成）。"""
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    site, groups = body.get("site", ""), body.get("groups", [])
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", site or ""):
        return JSONResponse({"error": "bad site name"}, status_code=400)
    if not isinstance(groups, list) or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", g or "") for g in groups):
        return JSONResponse({"error": "bad groups"}, status_code=400)
    with db() as conn:
        cur = conn.execute("UPDATE sites SET groups=? WHERE name=?", (json.dumps(groups), site))
        if cur.rowcount == 0:
            return JSONResponse({"error": f"unknown site {site}"}, status_code=404)
    return JSONResponse({"ok": True, "site": site, "groups": groups})


async def admin_models(request: Request) -> Response:
    """站点模型清单同步（全量替换）：consoled 手动加模型/刷新上游 id/删除后回写注册表，
    使 admin/list 与 LiteLLM 实际 deployment 保持一致。权威数据在 LiteLLM——
    这里只做登记簿同步，不做任何 LiteLLM 写操作。"""
    if not require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    site, models = body.get("site", ""), body.get("models", [])
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", site or ""):
        return JSONResponse({"error": "bad site name"}, status_code=400)
    # 模型名允许点号（qwen3.8-27b 一类版本号命名）；port 必须可转 int
    if not isinstance(models, list) or not all(
            isinstance(m, dict) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", m.get("name") or "")
            and isinstance(m.get("port"), int) for m in models):
        return JSONResponse({"error": "bad models (expect [{name, port, upstream_model?}])"},
                            status_code=400)
    with db() as conn:
        cur = conn.execute("UPDATE sites SET models=? WHERE name=?", (json.dumps(models), site))
        if cur.rowcount == 0:
            return JSONResponse({"error": f"unknown site {site}"}, status_code=404)
    return JSONResponse({"ok": True, "site": site, "models": models})


# ---------------------------------------------------------------- wg0.conf 持久化辅助

PEER_BLOCK = "# peer {name}\n[Peer]\nPublicKey = {pubkey}\nAllowedIPs = {ip}/32\n"


def append_peer_to_conf(pubkey: str, ip: str, name: str) -> None:
    WG_CONF.parent.mkdir(parents=True, exist_ok=True)
    if not WG_CONF.exists():  # 兜底：接口由 deploy.sh 先建，正常不会走到
        raise RuntimeError(f"{WG_CONF} missing; run deploy.sh first")
    if pubkey in WG_CONF.read_text():
        return
    with WG_CONF.open("a") as fh:
        fh.write(PEER_BLOCK.format(name=name, pubkey=pubkey, ip=ip))


def remove_peer_from_conf(pubkey: str) -> None:
    if not WG_CONF.exists():
        return
    blocks = WG_CONF.read_text().split("\n\n")
    kept = [b for b in blocks if pubkey not in b]
    WG_CONF.write_text("\n\n".join(kept) + "\n")


# ---------------------------------------------------------------- install.sh 模板（站点侧一行执行）

INSTALL_SH = """#!/usr/bin/env bash
# private-llm 站点一键接入（US-P7）：wireguard → 注册公钥 → 隧道自启 → 自检 → 注册模型
set -euo pipefail
TOKEN="{token}"
ENDPOINT="{endpoint}"
SITE_WG_IFACE="__SITE_WG_IFACE__"
SITE_WG_CONF="/etc/wireguard/${{SITE_WG_IFACE}}.conf"

echo "== private-llm site onboarding =="
if ! command -v wg >/dev/null; then
  echo "-- installing wireguard-tools"
  if command -v apt-get >/dev/null; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wireguard-tools
  elif command -v yum >/dev/null; then yum install -y epel-release elrepo-release && yum install -y kmod-wireguard wireguard-tools
  else echo "no supported package manager"; exit 1; fi
fi

echo "-- generating site keypair (private key never leaves this machine)"
umask 077
wg genkey | tee /tmp/pll.key | wg pubkey > /tmp/pll.pub
PUBKEY=$(cat /tmp/pll.pub)

echo "-- registering public key with gateway"
REG=$(curl -fsS -X POST "$ENDPOINT/onboard/register" -H 'content-type: application/json' \\
     -d "{{\\"token\\":\\"$TOKEN\\",\\"pubkey\\":\\"$PUBKEY\\"}}")
WG_IP=$(echo "$REG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["wg_ip"])')

echo "-- writing $SITE_WG_CONF (wg_ip=$WG_IP)"
mkdir -p /etc/wireguard; umask 077
sed "s|<SITE_PRIVATE_KEY>|$(cat /tmp/pll.key)|" > "$SITE_WG_CONF" <<'EOF'
[Interface]
PrivateKey = <SITE_PRIVATE_KEY>
Address = __WG_IP__/24
MTU = 1280

[Peer]
PublicKey = __VPS_PUB__
Endpoint = __ENDPOINT_WG__
AllowedIPs = __WG_ALLOWED__
PersistentKeepalive = 25
EOF
sed -i "s|__WG_IP__|$WG_IP|; s|__VPS_PUB__|__VPS_PUBLIC_KEY__|; s|__ENDPOINT_WG__|__WG_ENDPOINT__|" "$SITE_WG_CONF"

echo "-- enabling wg-quick@${{SITE_WG_IFACE}} (auto-start & self-healing)"
if systemctl is-active --quiet "wg-quick@${{SITE_WG_IFACE}}"; then
  systemctl restart "wg-quick@${{SITE_WG_IFACE}}"
else
  systemctl enable --now "wg-quick@${{SITE_WG_IFACE}}"
fi
sleep 2

echo "-- tuning tunnel transport (BBR, robust to cross-border random loss)"
modprobe tcp_bbr 2>/dev/null || true
cat > /etc/sysctl.d/99-private-llm-tunnel.conf <<'SYSCTL'
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq
net.core.rmem_max = 7500000
net.core.wmem_max = 7500000
SYSCTL
sysctl --system >/dev/null

echo "-- self-check"
OK=1
if ping -c 2 -W 3 __GW_WG_IP__ >/dev/null 2>&1; then PING=pass; else PING=fail; OK=0; fi
PORTS="__MODEL_PORTS__"
for P in $PORTS; do
  if curl -sf -m 5 "http://127.0.0.1:$P/v1/models" >/dev/null; then eval "PORT_$P=pass"; else eval "PORT_$P=fail"; OK=0; fi
done
echo "ping_vps=$PING"
for P in $PORTS; do echo "model_port_$P=$(eval echo \\$PORT_$P)"; done

echo "-- confirming with gateway"
RESULT=$(curl -fsS -X POST "$ENDPOINT/onboard/confirm" -H 'content-type: application/json' \\
  -d "{{\\"token\\":\\"$TOKEN\\",\\"ok\\":$OK,\\"results\\":{{\\"ping\\":\\"$PING\\"}}}}")
echo "$RESULT"
if [ "$OK" = "1" ] && echo "$RESULT" | grep -q '"registered": *true'; then
  echo "== SUCCESS: site models are live on the gateway =="
else
  echo "== FAILED: not registered (see results above) =="; exit 1
fi
"""


app = Starlette(routes=[
    Route("/onboard/install", install, methods=["GET"]),
    Route("/onboard/register", register, methods=["POST"]),
    Route("/onboard/confirm", confirm, methods=["POST"]),
    Route("/onboard/admin/tokens", admin_token, methods=["POST"]),
    Route("/onboard/admin/revoke", admin_revoke, methods=["POST"]),
    Route("/onboard/admin/groups", admin_groups, methods=["POST"]),
    Route("/onboard/admin/models", admin_models, methods=["POST"]),
    Route("/onboard/admin/list", admin_list, methods=["GET"]),
])

init_db()
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ONBOARDD_PORT", "8100")), log_level="info")
