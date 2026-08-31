"""mcp-hub：网关托管视觉 MCP + 外部 MCP 代理（设计 §3.3，US-P4/P12，issue #71）。

入口（经 nginx，同一把用户虚拟 Key 鉴权）：
  /mcp                Streamable HTTP（MCP 协议）：analyze_image（image_url 或 image_base64）
                      + upload_image（Base64 → 临时 URL）+ 外部 MCP 前缀工具
  /mcp/upload         POST multipart：本地图 → 限时临时 URL（30min TTL，白名单 jpg/png/webp/gif，≤10MB）
  /mcp/files/<token>  临时文件读取（随机不可猜 token，无鉴权、到期即清理）
  /mcp/usage          GET：本 Key 的 MCP 工具调用计数

凭据边界（C5）：用户 Key 只用于网关鉴权与记账——经 LiteLLM /key/info 验真，
analyze_image 以调用者自己的 Key 回调回环 LiteLLM（token 记调用者账上），
永不发往任何上游。
"""

import asyncio
import base64
import binascii
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
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.utilities.authorization import AuthContext

# ---------------------------------------------------------------- 配置

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:4000")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://llm-portal.example.com")
VISION_MODEL_FALLBACK = os.environ.get("MCP_VISION_MODEL", "").strip()
DATA_DIR = Path(os.environ.get("MCP_HUB_DATA", "/var/lib/private-llm/mcp-hub"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "usage.db"
VISION_CONF = Path(os.environ.get("MCP_VISION_CONF", "/etc/private-llm/vision/config.json"))
EXTERNAL_MCP_CONF = Path(os.environ.get("EXTERNAL_MCP_CONF", "/etc/private-llm/external-mcp.json"))
UPLOAD_TTL = int(os.environ.get("UPLOAD_TTL", "1800"))  # 30 分钟
MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def record_usage(key: str, tool: str) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("INSERT INTO usage VALUES (?,?,?)", (key_hash(key), tool, int(time.time())))


with sqlite3.connect(DB_PATH) as _db:
    _db.execute("CREATE TABLE IF NOT EXISTS usage (key_hash TEXT, tool TEXT, ts INTEGER)")


# ---------------------------------------------------------------- Key 验证（LiteLLM /key/info）


async def check_key(key: str) -> dict | None:
    """验真用户虚拟 Key；返回 key info（含 metadata），无效返回 None。Key 旅程到此为止。"""
    if not key:
        return None
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
    return info


class LiteLLMTokenVerifier(TokenVerifier):
    """MCP 传输层 Bearer 鉴权：Streamable HTTP + 标准 Bearer（US-P4 可移植集）。"""

    async def verify_token(self, token: str) -> AccessToken | None:
        info = await check_key(token)
        if info is None:
            return None
        group = (info.get("metadata") or {}).get("group") or "default"
        return AccessToken(token=token, client_id=key_hash(token), scopes=[str(group)])


def group_access(ctx: AuthContext) -> bool:
    """无 tags 的工具全局可用；有 tags 的外部工具仅向命中分组的 Key 开放。"""
    groups = ctx.component.tags
    return not groups or bool(ctx.token and groups.intersection(ctx.token.scopes))


def current_key(ctx) -> str:
    """工具内取调用者 Key：经 contextvar 回到 HTTP 请求头。"""
    req = get_http_request()
    auth = req.headers.get("authorization", "") if req else ""
    if auth.lower().startswith("bearer "):
        return auth[7:]
    raise ToolError("missing bearer key")


# ---------------------------------------------------------------- 内建视觉工具（US-P4）

def vision_model() -> str:
    """Return the admin-selected model, with the environment value as a migration fallback."""
    try:
        config = json.loads(VISION_CONF.read_text())
        selected = config.get("model") if isinstance(config, dict) else None
        if isinstance(selected, str) and selected.strip():
            return selected.strip()
    except (OSError, json.JSONDecodeError):
        pass
    if VISION_MODEL_FALLBACK:
        return VISION_MODEL_FALLBACK
    raise ToolError("vision model is not configured; select one in MCP management")

async def fetch_image_data(url: str) -> tuple[bytes, str]:
    """取图：支持公网 URL 与本网关临时文件路径（/mcp/files/<token>）。"""
    if url.startswith("/mcp/files/"):
        token = url.rsplit("/", 1)[-1].split(".")[0]
        path = UPLOAD_DIR / token
        for suffix in ALLOWED_TYPES.values():
            candidate = path.with_suffix(suffix)
            if candidate.exists():
                return candidate.read_bytes(), mimetypes.types_map[suffix]
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


# ------------------------------------------------ 内嵌图片共用链路（issue #71）


def sniff_image_type(data: bytes) -> str | None:
    """按字节签名识别图片类型（仅白名单内），识别不了返回 None。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image_bytes(data: bytes, declared_mime: str | None) -> str:
    """校验图片字节：非空、≤10MB、签名在白名单内；声明了 MIME 时还须与签名一致。
    返回实际 MIME。错误信息只含类型名，不回显图片内容。"""
    if not data:
        raise ToolError("empty image data")
    if len(data) > MAX_UPLOAD:
        raise ToolError("image too large (max 10MB)")
    actual = sniff_image_type(data)
    if actual is None:
        raise ToolError(f"unsupported image format; allowed: {sorted(ALLOWED_TYPES)}")
    if declared_mime is not None:
        if declared_mime not in ALLOWED_TYPES:
            raise ToolError(f"unsupported mime type {declared_mime!r}; allowed: {sorted(ALLOWED_TYPES)}")
        if declared_mime != actual:
            raise ToolError(f"declared mime {declared_mime!r} does not match image signature {actual!r}")
    return actual


def decode_inline_image(image_base64: str | None, mime_type: str | None) -> tuple[bytes, str]:
    """解码内嵌 Base64 图片（analyze_image / upload_image 共用）：严格 Base64（容忍空白与换行）、
    非空、≤10MB、MIME 白名单且与实际签名一致。返回 (原始字节, MIME)。"""
    if not isinstance(image_base64, str) or not image_base64.strip():
        raise ToolError("image_base64 must be a non-empty base64 string")
    compact = "".join(image_base64.split())  # 容忍 base64(1) 输出被折行复制
    if len(compact) > MAX_UPLOAD * 2:  # base64 至多膨胀 4/3；解码前先拒超长输入
        raise ToolError("image too large (max 10MB)")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise ToolError("image_base64 is not valid base64")
    return data, validate_image_bytes(data, mime_type)


def store_image(data: bytes, mime: str) -> str:
    """图片落盘为临时文件（随机 token + 白名单后缀；TTL 由 ttl_loop 清理），返回临时 URL。"""
    file_token = secrets.token_urlsafe(18)
    (UPLOAD_DIR / (file_token + ALLOWED_TYPES[mime])).write_bytes(data)
    return f"{PUBLIC_BASE}/mcp/files/{file_token}{ALLOWED_TYPES[mime]}"


@asynccontextmanager
async def lifespan(app):
    global loaded_config_sha256
    task = asyncio.create_task(ttl_loop())
    await register_external_tools()
    config_bytes = EXTERNAL_MCP_CONF.read_bytes() if EXTERNAL_MCP_CONF.exists() else b""
    loaded_config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    try:
        yield
    finally:
        task.cancel()
        for client in external_clients.values():
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass


mcp = FastMCP(
    "mcp-hub",
    auth=LiteLLMTokenVerifier(),
    lifespan=lifespan,
    middleware=[AuthMiddleware(auth=group_access)],
)


@mcp.tool
async def analyze_image(question: str, image_url: str | None = None,
                        image_base64: str | None = None, mime_type: str | None = None,
                        ctx: Context = None) -> str:
    """用视觉模型识别图片。图片二选一：image_url（公网图片 URL，或 upload_image /
    POST /mcp/upload 返回的临时 URL）；image_base64（本地图片的 Base64，可配 mime_type，
    缺省按字节签名识别，单次调用直接分析、无需先上传）。question 为针对图片的问题。"""
    key = current_key(ctx)
    if image_url and image_base64:
        raise ToolError("provide either image_url or image_base64, not both")
    if image_base64:
        data, mime = decode_inline_image(image_base64, mime_type)
        store_image(data, mime)  # 与 upload_image 同一存储路径：随机 token + 30min TTL
    elif image_url:
        data, mime = await fetch_image_data(image_url)
    else:
        raise ToolError("image_url or image_base64 is required")
    b64 = base64.b64encode(data).decode()
    body = {
        "model": vision_model(),
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


@mcp.tool
async def upload_image(image_base64: str, mime_type: str | None = None, ctx: Context = None) -> str:
    """上传本地图片（Base64 编码字节，可配 mime_type，缺省按字节签名识别），返回 JSON 字符串
    {"url": …, "expires_in": 1800}：临时 URL 30 分钟有效，可反复传给 analyze_image 的
    image_url。一次性识别不必先上传，直接用 analyze_image 的 image_base64。"""
    key = current_key(ctx)
    data, mime = decode_inline_image(image_base64, mime_type)
    url = store_image(data, mime)
    record_usage(key, "upload_image")
    return json.dumps({"url": url, "expires_in": UPLOAD_TTL})


# ---------------------------------------------------------------- 外部 MCP 代理（US-P12）

external_clients: dict[str, Client] = {}
loaded_config_sha256 = ""
loaded_tool_owners: dict[str, str] = {}
loaded_entry_state: dict[str, dict] = {}
loaded_config_ok = True


async def register_external_tools() -> None:
    """Load every configured external MCP and attest its exposed FastMCP surface."""
    global loaded_config_ok
    loaded_tool_owners.clear()
    loaded_entry_state.clear()
    loaded_tool_owners["analyze_image"] = "builtin"
    loaded_tool_owners["upload_image"] = "builtin"
    loaded_config_ok = True
    if not EXTERNAL_MCP_CONF.exists():
        return
    try:
        entries = json.loads(EXTERNAL_MCP_CONF.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        loaded_config_ok = False
        loaded_entry_state["registry"] = {"ok": False, "reason": "invalid_registry", "tools": []}
        print("[mcp-hub] external mcp config error")
        return
    if not isinstance(entries, list):
        loaded_config_ok = False
        loaded_entry_state["registry"] = {"ok": False, "reason": "invalid_registry", "tools": []}
        return
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            loaded_config_ok = False
            loaded_entry_state[f"invalid-{len(loaded_entry_state)}"] = {
                "ok": False, "reason": "invalid_entry", "tools": []}
            continue
        name, prefix = entry["name"], entry.get("prefix", f"{entry['name']}_")
        if name in names or not isinstance(prefix, str) or not prefix:
            loaded_config_ok = False
            loaded_entry_state[name] = {"ok": False, "reason": "invalid_entry", "tools": []}
            continue
        names.add(name)
        raw_groups = entry.get("groups", [])
        if not isinstance(raw_groups, list) or any(not isinstance(group, str) or not group
                                                   for group in raw_groups):
            loaded_config_ok = False
            loaded_entry_state[name] = {"ok": False, "reason": "invalid_groups", "tools": []}
            print(f"[mcp-hub] external mcp '{name}': skipped (invalid groups list)")
            continue
        groups = set(raw_groups)
        if "REPLACE" in str(entry.get("api_key", "")) or "REPLACE" in entry.get("url", ""):
            loaded_config_ok = False
            loaded_entry_state[name] = {"ok": False, "reason": "placeholder", "tools": []}
            print(f"[mcp-hub] external mcp '{name}': skipped (placeholder credentials)")
            continue
        try:
            client = Client(entry["url"], auth=BearerAuth(entry["api_key"]) if entry.get("api_key") else None)
            await client.__aenter__()
            tools = list(await client.list_tools())
            exposed = [f"{prefix}{tool.name}" for tool in tools]
            if not tools or len(set(exposed)) != len(exposed) or any(tool_name in loaded_tool_owners
                                                                       for tool_name in exposed):
                await client.__aexit__(None, None, None)
                loaded_config_ok = False
                loaded_entry_state[name] = {"ok": False, "reason": "tool_collision", "tools": []}
                print(f"[mcp-hub] external mcp '{name}': skipped (tool collision)")
                continue
            external_clients[name] = client
            for tool in tools:

                async def _proxy(_client=client, _name=tool.name,
                                 _exposed_name=f"{prefix}{tool.name}", **kwargs):
                    result = await _client.call_tool(_name, kwargs)
                    # The upstream may return an MCP tool error without raising.  It is
                    # still a failed call and therefore must not enter the usage ledger.
                    if not (getattr(result, "isError", False) or getattr(result, "is_error", False)):
                        record_usage(current_key(None), _exposed_name)
                    return result.content[0].text if result.content else "(empty)"

                from fastmcp.tools import FunctionTool
                mcp.add_tool(FunctionTool(
                    fn=_proxy,
                    name=f"{prefix}{tool.name}",
                    description=f"[{name}] {tool.description or ''}".strip(),
                    parameters=tool.inputSchema or {"type": "object", "properties": {}},
                    tags=groups,
                    run_in_thread=False,
                ))
            actual = []
            for tool_name in exposed:
                exposed_tool = await mcp.get_tool(tool_name)
                if exposed_tool is None or exposed_tool.name != tool_name:
                    loaded_config_ok = False
                    loaded_entry_state[name] = {"ok": False, "reason": "surface_mismatch", "tools": []}
                    break
                actual.append(tool_name)
            else:
                for tool_name in actual:
                    loaded_tool_owners[tool_name] = name
                loaded_entry_state[name] = {"ok": True, "reason": "", "tools": actual}
            scope = ",".join(sorted(groups)) if groups else "global"
            if loaded_entry_state[name]["ok"]:
                print(f"[mcp-hub] external mcp '{name}': {len(tools)} tools proxied "
                      f"(prefix '{prefix}', groups '{scope}')")
        except Exception as exc:  # 外部服务不可达不阻断启动
            loaded_config_ok = False
            loaded_entry_state[name] = {"ok": False, "reason": "unavailable", "tools": []}
            print(f"[mcp-hub] external mcp '{name}' unavailable")


async def config_state(request: Request) -> Response:
    return JSONResponse({"sha256": loaded_config_sha256, "ok": loaded_config_ok,
                         "tools": loaded_tool_owners, "entries": loaded_entry_state})


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
    content_type = (upload_file.content_type or "").split(";")[0].strip() or None
    data = await upload_file.read()
    if len(data) > MAX_UPLOAD:
        return JSONResponse({"error": "file too large (max 10MB)"}, status_code=413)
    try:  # 与内嵌 Base64 路径共用校验：签名须与声明 MIME 一致（issue #71）
        mime = validate_image_bytes(data, content_type)
    except ToolError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    url = store_image(data, mime)
    record_usage(key, "upload")
    return JSONResponse({"url": url, "expires_in": UPLOAD_TTL})


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


# ---------------------------------------------------------------- 网关主页（隐藏 LiteLLM Swagger 的对外脸面）

HOMEPAGE_PATH = Path(__file__).parent / "homepage.html"


async def homepage(request: Request) -> Response:
    return Response(HOMEPAGE_PATH.read_bytes(), media_type="text/html")


# ---------------------------------------------------------------- 组装 ASGI 应用（勿用 from_fastapi：会剥 authorization 头）

app = mcp.http_app(path="/mcp")
# 同一 ASGI app 上追加辅助路由（不经 Mount，保留 authorization 头）
app.routes.append(Route("/", homepage, methods=["GET"]))
app.routes.append(Route("/mcp/upload", upload, methods=["POST"]))
app.routes.append(Route("/mcp/usage", usage, methods=["GET"]))
app.routes.append(Route("/mcp/files/{name}", files, methods=["GET"]))
app.routes.append(Route("/internal/config-state", config_state, methods=["GET"]))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MCP_HUB_PORT", "8200")), log_level="info")
