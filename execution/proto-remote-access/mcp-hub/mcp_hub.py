"""mcp-hub：网关托管视觉 MCP + 外部 MCP 代理（设计 §3.3，US-P4/P12）。

入口（经 nginx，同一把用户虚拟 Key 鉴权）：
  /mcp                Streamable HTTP（MCP 协议）：analyze_image + 外部 MCP 前缀工具
  /mcp/upload         POST multipart：本地图 → 限时临时 URL（30min TTL，白名单 jpg/png/webp/gif，≤10MB）
  /mcp/files/<token>  临时文件读取（随机不可猜 token，无鉴权、到期即清理）
  /mcp/usage          GET：本 Key 的 MCP 工具调用计数

凭据边界（C5）：用户 Key 只用于网关鉴权与记账——经 LiteLLM /key/info 验真，
analyze_image 以调用者自己的 Key 回调回环 LiteLLM（token 记调用者账上），
永不发往任何上游。
"""

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from fastmcp import FastMCP, Client
from fastmcp.client.auth import BearerAuth
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_http_request

# ---------------------------------------------------------------- 配置

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:4000")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://private-llm.onlyservice.io")
VISION_MODEL = os.environ.get("MCP_VISION_MODEL", "qwen3.6-35b-fp8")
DATA_DIR = Path(os.environ.get("MCP_HUB_DATA", "/var/lib/private-llm/mcp-hub"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "usage.db"
EXTERNAL_MCP_CONF = Path(os.environ.get("EXTERNAL_MCP_CONF", "/etc/private-llm/external-mcp.json"))
UPLOAD_TTL = int(os.environ.get("UPLOAD_TTL", "1800"))  # 30 分钟
MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
KEY_CACHE_TTL = 60  # /key/info 结果进程内缓存，秒

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def record_usage(key: str, tool: str) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("INSERT INTO usage VALUES (?,?,?)", (key_hash(key), tool, int(time.time())))


with sqlite3.connect(DB_PATH) as _db:
    _db.execute("CREATE TABLE IF NOT EXISTS usage (key_hash TEXT, tool TEXT, ts INTEGER)")


# ---------------------------------------------------------------- Key 验证（LiteLLM /key/info，带缓存）

_key_cache: dict[str, tuple[float, dict]] = {}


async def check_key(key: str) -> dict | None:
    """验真用户虚拟 Key；返回 key info（含 metadata），无效返回 None。Key 旅程到此为止。"""
    if not key:
        return None
    hit = _key_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < KEY_CACHE_TTL:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{LITELLM_BASE}/key/info", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    info = r.json().get("key_info", {})
    if info.get("is_disabled"):
        return None
    _key_cache[key] = (now, info)
    return info


class LiteLLMTokenVerifier(TokenVerifier):
    """MCP 传输层 Bearer 鉴权：Streamable HTTP + 标准 Bearer（US-P4 可移植集）。"""

    async def verify_token(self, token: str) -> AccessToken | None:
        info = await check_key(token)
        if info is None:
            return None
        return AccessToken(token=token, client_id=key_hash(token), scopes=[])


def current_key(ctx) -> str:
    """工具内取调用者 Key：经 contextvar 回到 HTTP 请求头。"""
    req = get_http_request()
    auth = req.headers.get("authorization", "") if req else ""
    if auth.lower().startswith("bearer "):
        return auth[7:]
    raise ToolError("missing bearer key")


# ---------------------------------------------------------------- 内建视觉工具（US-P4）

async def fetch_image_data(url: str) -> tuple[bytes, str]:
    """取图：支持公网 URL 与本网关临时文件路径（/mcp/files/<token>）。"""
    if url.startswith("/mcp/files/"):
        token = url.rsplit("/", 1)[-1].split(".")[0]
        path = UPLOAD_DIR / token
        for suffix in ALLOWED_TYPES.values():
            candidate = path.with_suffix(suffix)
            if candidate.exists():
                return candidate.read_bytes(), candidate.stat().st_mtime
        raise ToolError(f"image not found or expired: {url}")
    if not url.startswith(("http://", "https://")):
        raise ToolError(f"not a valid image url: {url!r}")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url)
    if r.status_code != 200 or len(r.content) > 50 * 1024 * 1024:
        raise ToolError(f"cannot fetch image (status={r.status_code})")
    mime = r.headers.get("content-type", "").split(";")[0].strip()
    if mime not in ALLOWED_TYPES:
        mime = mimetypes.guess_type(url)[0] or ""
    if mime not in ALLOWED_TYPES:
        raise ToolError(f"unsupported image type: {mime or 'unknown'}")
    return r.content, mime


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(ttl_loop())
    await register_external_tools()
    try:
        yield
    finally:
        task.cancel()
        for client in external_clients.values():
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass


mcp = FastMCP("mcp-hub", auth=LiteLLMTokenVerifier(), lifespan=lifespan)


@mcp.tool
async def analyze_image(image_url: str, question: str, ctx: Context) -> str:
    """用视觉模型识别图片。image_url 支持公网图片 URL 或 /mcp/upload 返回的临时 URL；
    question 为针对图片的问题。"""
    key = current_key(ctx)
    data, mime = await fetch_image_data(image_url)
    b64 = base64.b64encode(data).decode()
    body = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": question},
            ],
        }],
        "max_tokens": 2048,
    }
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(
            f"{LITELLM_BASE}/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {key}"},  # 调用者自己的 Key：token 记其账上
        )
    record_usage(key, "analyze_image")
    if r.status_code != 200:
        raise ToolError(f"vision model error: {r.status_code} {r.text[:300]}")
    choices = r.json().get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content") or "(empty response)"


# ---------------------------------------------------------------- 外部 MCP 代理（US-P12）

external_clients: dict[str, Client] = {}


async def register_external_tools() -> None:
    """按配置文件把外部 MCP 工具以 <前缀><名> 透出（前缀限 [a-z0-9_] 防冲突）。"""
    if not EXTERNAL_MCP_CONF.exists():
        return
    try:
        entries = json.loads(EXTERNAL_MCP_CONF.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[mcp-hub] external mcp config error: {exc}")
        return
    for entry in entries:
        name, prefix = entry["name"], entry.get("prefix", f"{entry['name']}_")
        if "REPLACE" in str(entry.get("api_key", "")) or "REPLACE" in entry.get("url", ""):
            print(f"[mcp-hub] external mcp '{name}': skipped (placeholder credentials)")
            continue
        try:
            client = Client(entry["url"], auth=BearerAuth(entry["api_key"]) if entry.get("api_key") else None)
            await client.__aenter__()
            tools = await client.list_tools()
            for tool in tools:

                async def _proxy(_client=client, _name=tool.name, **kwargs):
                    result = await _client.call_tool(_name, kwargs)
                    return result.content[0].text if result.content else "(empty)"

                from fastmcp.tools import Tool as FastTool
                mcp.add_tool(FastTool(
                    fn=_proxy,
                    name=f"{prefix}{tool.name}",
                    description=f"[{name}] {tool.description or ''}".strip(),
                    parameters=tool.parameters or {"type": "object", "properties": {}},
                ))
            print(f"[mcp-hub] external mcp '{name}': {len(tools)} tools proxied (prefix '{prefix}')")
        except Exception as exc:  # 外部服务不可达不阻断启动
            print(f"[mcp-hub] external mcp '{name}' unavailable: {exc}")


# ---------------------------------------------------------------- 临时文件清理

async def ttl_loop() -> None:
    while True:
        now = time.time()
        for path in UPLOAD_DIR.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime > UPLOAD_TTL:
                    path.unlink()
            except OSError:
                pass
        await asyncio.sleep(60)


# ---------------------------------------------------------------- HTTP 辅助入口（upload / usage / files）

async def _bearer_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


async def upload(request: Request) -> Response:
    key = await _bearer_of(request)
    if await check_key(key) is None:
        return JSONResponse({"error": "invalid or missing key"}, status_code=401)
    form = await request.form()
    upload_file = form.get("file")
    if upload_file is None or isinstance(upload_file, str):
        return JSONResponse({"error": "multipart field 'file' required"}, status_code=400)
    content_type = (upload_file.content_type or "").split(";")[0].strip()
    if content_type not in ALLOWED_TYPES:
        return JSONResponse({"error": f"unsupported type {content_type!r}; allowed: {sorted(ALLOWED_TYPES)}"}, status_code=400)
    data = await upload_file.read()
    if len(data) > MAX_UPLOAD:
        return JSONResponse({"error": "file too large (max 10MB)"}, status_code=413)
    token = secrets.token_urlsafe(18)
    (UPLOAD_DIR / (token + ALLOWED_TYPES[content_type])).write_bytes(data)
    record_usage(key, "upload")
    return JSONResponse({"url": f"{PUBLIC_BASE}/mcp/files/{token}{ALLOWED_TYPES[content_type]}", "expires_in": UPLOAD_TTL})


async def usage(request: Request) -> Response:
    key = await _bearer_of(request)
    if await check_key(key) is None:
        return JSONResponse({"error": "invalid or missing key"}, status_code=401)
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT tool, COUNT(*) FROM usage WHERE key_hash=? GROUP BY tool", (key_hash(key),)
        ).fetchall()
    return JSONResponse({"key": f"sk-...{key[-4:]}", "tools": {tool: count for tool, count in rows}, "total": sum(c for _, c in rows)})


async def files(request: Request) -> Response:
    name = request.path_params["name"]
    path = UPLOAD_DIR / name
    if not path.is_file() or not path.parent == UPLOAD_DIR:
        return JSONResponse({"error": "not found or expired"}, status_code=404)
    if time.time() - path.stat().st_mtime > UPLOAD_TTL:
        path.unlink(missing_ok=True)
        return JSONResponse({"error": "expired"}, status_code=404)
    mime, _ = mimetypes.guess_type(name)
    return Response(path.read_bytes(), media_type=mime or "application/octet-stream")


# ---------------------------------------------------------------- 组装 ASGI 应用（勿用 from_fastapi：会剥 authorization 头）

app = mcp.http_app(path="/mcp")
# 同一 ASGI app 上追加辅助路由（不经 Mount，保留 authorization 头）
app.routes.append(Route("/mcp/upload", upload, methods=["POST"]))
app.routes.append(Route("/mcp/usage", usage, methods=["GET"]))
app.routes.append(Route("/mcp/files/{name}", files, methods=["GET"]))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MCP_HUB_PORT", "8200")), log_level="info")
