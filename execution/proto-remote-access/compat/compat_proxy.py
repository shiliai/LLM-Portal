#!/usr/bin/env python3
"""private-llm 协议兼容层（issue #9 / US-13 us13-v1）。

链路：nginx → compat-proxy(:8400) → litellm:4000，仅代理三条路径：
  /v1/messages  /v1/messages/count_tokens  /v1/chat/completions
其余路径不经本服务；内部容器（mcp-hub/onboardd/console）仍直连 litellm。

只做协议层确定性修复，鉴权/路由/记账仍归 LiteLLM：
  1. Anthropic 内联 system 规范化（US-13 v1）：/messages 与 /count_tokens 共用同一
     纯函数 normalize_anthropic_messages，每条内联 system 的结构化内容块合并进最近
     前一条 user 消息，无前置 user 时原地转合成 user；顶层 system、消息顺序、
     tool_use/tool_result ID、cache_control、thinking 块全部不动。
  2. 强制工具选择：单工具 required/any 改写为指定该工具（上游实测可完整恢复）；
     多工具返回稳定 400，不静默删参、不代客户端选第一个。
  3. OpenAI 流式 finish_reason 修正：已观察到 tool_calls fragments 的 choice 最终
     报 stop 时规范化为 tool_calls（仅重写该行，其余字节原样）。

原则：请求不命中任何规则 → 原始字节透传（保护 prompt cache 前缀与上游语义）；
SSE 逐行流式转发、绝不缓冲完整响应；指标脱敏——只记规则版本/索引/哈希，
不记 API key、头值或消息正文。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time as _time
import uuid as _uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from compat_metrics import metrics, metrics_response, status_class as _status_class

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://litellm:4000").rstrip("/")
COMPAT_PORT = int(os.environ.get("COMPAT_PORT", "8400"))
US13_VERSION = "us13-v1"
PROXY_PATHS = ("/v1/messages", "/v1/messages/count_tokens", "/v1/chat/completions")

# 逐跳头 + 交给 httpx 按目标重建的头（Host/Content-Length/Accept-Encoding）；
# Authorization、x-api-key、anthropic-version、anthropic-beta、X-Forwarded-For 等一律原样透传
REQ_DROP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailers", "upgrade", "proxy-authenticate", "proxy-authorization", "accept-encoding",
}
RESP_DROP = {
    "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailers", "upgrade", "proxy-authenticate", "proxy-authorization",
}

MULTI_TOOL_MESSAGE = (
    "forced tool_choice (required / {\"type\":\"any\"}) with multiple tools is not supported "
    "by the upstream model stack: the gateway will not silently drop the directive nor pick "
    "a tool on the client's behalf. Use tool_choice auto/none, name a specific tool, "
    f"or send exactly one tool. (compat {US13_VERSION}, issue #9)"
)
UPSTREAM_MESSAGE = (
    "gateway compatibility proxy could not reach the LLM upstream; "
    "the request was not processed. Retry shortly."
)


class CompatReject(Exception):
    """协议层稳定拒绝（多工具 forced 等）：携带对客户端可判读的错误码。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def metric(event: str, **fields: Any) -> None:
    """脱敏指标行（stdout → docker logs）。绝不输出 API key、头值或消息正文。"""
    print(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": event, **fields}, ensure_ascii=False), flush=True)


def _blocks(content: Any) -> list[Any]:
    """内容 → 结构化块列表（字符串包成 text 块；不做模糊字符串拼接）。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def normalize_anthropic_messages(body: dict) -> dict | None:
    """US-13 v1 内联 system 规范化（纯函数、确定性：同输入逐字节同输出）。

    就地修改 body["messages"] 并返回指标；未命中（无内联 system）返回 None，
    调用方据此保持原始请求字节透传。
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    if not any(isinstance(m, dict) and m.get("role") == "system" for m in messages):
        return None
    out: list[Any] = []
    merged: list[int] = []
    synthetic: list[int] = []
    last_user_at: int | None = None  # out 中的下标
    for i, msg in enumerate(messages):
        if not (isinstance(msg, dict) and msg.get("role") == "system"):
            out.append(msg)
            if isinstance(msg, dict) and msg.get("role") == "user":
                last_user_at = len(out) - 1
            continue
        blocks = _blocks(msg.get("content"))
        if last_user_at is not None:
            target = out[last_user_at]
            # 合并到最近前一条 user：保留其原块顺序，system 块按会话顺序追加在后
            target["content"] = _blocks(target.get("content")) + blocks
            merged.append(i)
        else:
            out.append({"role": "user", "content": blocks})
            last_user_at = len(out) - 1
            synthetic.append(i)
    body["messages"] = out
    canonical = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    return {
        "merged": merged,
        "synthetic": synthetic,
        # 规范化后消息的短哈希：/messages 与 /count_tokens 同输入应一致（可对账）
        "norm_hash": hashlib.sha256(canonical.encode()).hexdigest()[:12],
    }


def _single_tool_name(body: dict, proto: str) -> str | None:
    tools = body.get("tools")
    if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
        return None
    tool = tools[0]
    name = (tool.get("function") or {}).get("name") if proto == "openai" else tool.get("name")
    return name if isinstance(name, str) and name else None


def rewrite_forced_tool_choice(body: dict, proto: str) -> dict | None:
    """强制工具选择兼容：单工具改写为指定该工具；多工具稳定 400。

    返回改写指标；未命中（非 forced / 工具数非 1 / 结构异常）返回 None，
    结构异常交 LiteLLM/上游按原生语义报错，不在本层发明行为。
    """
    tc = body.get("tool_choice")
    forced = tc == "required" if proto == "openai" else isinstance(tc, dict) and tc.get("type") == "any"
    if not forced:
        return None
    tools = body.get("tools")
    if not isinstance(tools, list):
        return None
    if len(tools) > 1:
        raise CompatReject(400, "forced_tool_choice_unsupported", MULTI_TOOL_MESSAGE)
    if len(tools) != 1:
        return None
    name = _single_tool_name(body, proto)
    if name is None:
        return None
    if proto == "openai":
        body["tool_choice"] = {"type": "function", "function": {"name": name}}
    else:
        rewritten = {"type": "tool", "name": name}
        if isinstance(tc, dict) and "disable_parallel_tool_use" in tc:
            rewritten["disable_parallel_tool_use"] = tc["disable_parallel_tool_use"]
        body["tool_choice"] = rewritten
    return {"from": "required" if proto == "openai" else "any", "to": name, "tools": len(tools)}


# DeepSeek 原生 DSML 工具调用标记：forced（指定函数）路径下 vLLM 会把裸标记文本放进
# function.arguments（auto 路径则是干净 JSON）。回传历史时 vLLM 解析 arguments 期望 JSON → 400。
_DSML_INVOKE = re.compile(r"<｜DSML｜invoke name=\"([^\"]+)\">(.*?)</｜DSML｜invoke>", re.DOTALL)
_DSML_PARAM = re.compile(r"<｜DSML｜parameter name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?>(.*?)</｜DSML｜parameter>", re.DOTALL)


def parse_dsml_arguments(text: str) -> list[dict] | None:
    """DSML invoke 标记 → 参数 dict 列表（每 invoke 一个）；非 DSML 结构返回 None，绝不猜测。"""
    invokes = _DSML_INVOKE.findall(text)
    if not invokes:
        return None
    parsed: list[dict] = []
    for _name, body in invokes:
        args: dict[str, Any] = {}
        for pname, is_string, value in _DSML_PARAM.findall(body):
            value = value.strip()
            if is_string == "false":
                try:
                    args[pname] = json.loads(value)
                except ValueError:
                    args[pname] = value
            else:
                args[pname] = value
        parsed.append(args)
    return parsed


def _normalize_dsml_arguments(arguments: Any) -> str | None:
    """非法 JSON 的 arguments 若为 DSML → 紧凑 JSON；合法 JSON / 非 DSML 原样返回 None。"""
    if not isinstance(arguments, str) or "<｜DSML｜" not in arguments:
        return None
    try:
        json.loads(arguments)
        return None
    except ValueError:
        pass
    parsed = parse_dsml_arguments(arguments)
    if not parsed:
        return None
    return json.dumps(parsed[0], separators=(",", ":"), ensure_ascii=False)


def normalize_dsml_history(body: dict) -> dict | None:
    """请求侧：assistant 历史 tool_calls 中的 DSML arguments → JSON（流式响应拼回/历史遗留同样治）。"""
    changed = 0
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            fixed = _normalize_dsml_arguments(function.get("arguments"))
            if fixed is not None:
                function["arguments"] = fixed
                changed += 1
    return {"calls": changed} if changed else None


def normalize_dsml_response(value: dict) -> bool:
    """响应侧（OpenAI 非流式）：message.tool_calls 的 DSML arguments → JSON，客户端免解析标记。"""
    changed = False
    choices = value.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            fixed = _normalize_dsml_arguments(function.get("arguments"))
            if fixed is not None:
                function["arguments"] = fixed
                changed = True
    return changed


class OpenAIStreamFixer:
    """OpenAI SSE finish_reason 修正状态机（逐 choice）。

    vLLM 实测：产生 tool_calls fragments 后最终 chunk 仍报 finish_reason=stop；
    该 choice 一旦见过 fragments 且最终为 stop → 改写为 tool_calls。未修改行字节原样。
    """

    def __init__(self) -> None:
        self.saw_fragments: set[int] = set()
        self.fixed: list[int] = []

    def process_line(self, raw: bytes, endpoint: str = "") -> bytes:
        stripped = raw.rstrip(b"\r\n")
        if not stripped.startswith(b"data:"):
            return raw
        data = stripped[5:].strip()
        if not data or data == b"[DONE]":
            return raw
        try:
            obj = json.loads(data)
        except ValueError:
            return raw
        choices = obj.get("choices") if isinstance(obj, dict) else None
        if not isinstance(choices, list) or not choices:
            return raw
        changed = False
        for idx, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and delta.get("tool_calls"):
                self.saw_fragments.add(idx)
            if choice.get("finish_reason") == "stop" and idx in self.saw_fragments:
                choice["finish_reason"] = "tool_calls"
                changed = True
                self.fixed.append(idx)
        if not changed:
            return raw
        if endpoint:
            metric("compat.finish_reason_fix", endpoint=endpoint, choices=sorted(set(self.fixed)))
        terminator = b"\r\n" if raw.endswith(b"\r\n") else (b"\n" if raw.endswith(b"\n") else b"")
        return b"data: " + json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode() + terminator


class _LineSplitter:
    """字节流 → 带换行符的行（SSE 逐行转发，跨 chunk 断行安全）。"""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        lines: list[bytes] = []
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                break
            lines.append(bytes(self._buf[: i + 1]))
            del self._buf[: i + 1]
        return lines

    def flush(self) -> bytes | None:
        if not self._buf:
            return None
        out = bytes(self._buf)
        self._buf.clear()
        return out


async def _passthrough(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    async for chunk in chunks:
        yield chunk


async def _openai_sse_rewritten(chunks: AsyncIterator[bytes], endpoint: str) -> AsyncIterator[bytes]:
    fixer = OpenAIStreamFixer()
    splitter = _LineSplitter()
    async for chunk in chunks:
        for line in splitter.feed(chunk):
            yield fixer.process_line(line, endpoint)
    tail = splitter.flush()
    if tail is not None:
        yield fixer.process_line(tail, endpoint)


def error_response(reject: CompatReject, proto: str, rid: str = "") -> Response:
    """按协议族返回稳定错误体：OpenAI error.code / Anthropic error.type 可判读。"""
    err_type = "invalid_request_error" if reject.status == 400 else "api_error"
    if proto == "anthropic":
        payload: dict[str, Any] = {"type": "error", "error": {"type": err_type, "message": reject.message}}
    else:
        payload = {"error": {"message": reject.message, "type": err_type, "code": reject.code}}
    headers = {"x-compat-rule": US13_VERSION}
    if rid:
        headers["x-request-id"] = rid
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status_code=reject.status,
        media_type="application/json",
        headers=headers,
    )


async def metrics_route(request: Request) -> Response:
    return metrics_response()


async def compat_proxy(request: Request) -> Response:
    t0 = _time.perf_counter()
    path = request.url.path
    query = request.url.query
    proto = "anthropic" if path.startswith("/v1/messages") else "openai"
    url = LITELLM_BASE + path + (f"?{query}" if query else "")
    raw = await request.body()

    # request_id 贯穿（issue #62 / README D4）：优先沿用入口（nginx $request_id / 客户端）
    # X-Request-Id，否则自生成；透传给 LiteLLM 并对客户端回传 x-request-id。
    # 仅用于跨层对账（结构化日志），**决不作为 Prometheus 标签**（高基数）。
    rid = request.headers.get("x-request-id") or _uuid.uuid4().hex
    metrics["active"].labels(proto=proto).inc()

    def finalize(resp_status: str, cause: str = "") -> None:
        metrics["active"].labels(proto=proto).dec()
        metrics["total"].labels(proto=proto, status_class=resp_status).observe(_time.perf_counter() - t0)
        if resp_status == "error":
            metrics["errors"].labels(cause=cause).inc()
        elif resp_status != "client_disconnect":
            metrics["requests"].labels(
                endpoint=path, proto=proto, stream=stream_flag, status_class=resp_status).inc()

    out_body = raw
    stream_flag = ""
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        if isinstance(parsed.get("stream"), bool):
            stream_flag = str(parsed["stream"]).lower()
        try:
            tc_info = rewrite_forced_tool_choice(parsed, proto)
        except CompatReject as exc:
            metric("compat.reject", request_id=rid, endpoint=path, reason=exc.code, tools=len(parsed.get("tools") or []))
            finalize("error", cause="reject_" + exc.code)
            return error_response(exc, proto, rid=rid)
        sys_info = normalize_anthropic_messages(parsed) if proto == "anthropic" else None
        dsml_info = normalize_dsml_history(parsed) if proto == "openai" else None
        if tc_info or sys_info or dsml_info:
            out_body = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode()
            if tc_info:
                metric("compat.tool_choice_rewrite", request_id=rid, endpoint=path, **tc_info)
            if sys_info:
                metric("compat.transform", rule=US13_VERSION, request_id=rid, endpoint=path, **sys_info)
            if dsml_info:
                metric("compat.dsml_args_normalized", request_id=rid, endpoint=path, side="request", **dsml_info)
    t_parsed = _time.perf_counter()
    metrics["parse"].labels(proto=proto).observe(t_parsed - t0)

    fwd = [(k, v) for k, v in request.headers.items() if k.lower() not in REQ_DROP and k.lower() != "x-request-id"]
    fwd.append(("accept-encoding", "identity"))  # 响应不压缩：字节透传 + SSE 行改写的前提
    fwd.append(("x-request-id", rid))            # 贯穿 request_id → LiteLLM（不引入缓冲）
    upstream_request = client.build_request(request.method, url, headers=fwd, content=out_body)
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        metric("compat.upstream_error", request_id=rid, endpoint=path, error=type(exc).__name__)
        finalize("error", cause="upstream_" + type(exc).__name__)
        return error_response(CompatReject(502, "upstream_unavailable", UPSTREAM_MESSAGE), proto, rid=rid)
    metrics["upstream_header"].labels(endpoint=path, proto=proto).observe(_time.perf_counter() - t_parsed)

    _up_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in RESP_DROP}
    _up_headers["x-request-id"] = rid  # 回传客户端（不含内部敏感信息）
    headers = Headers(_up_headers)
    status_class = _status_class(upstream.status_code)
    content_type = upstream.headers.get("content-type", "").split(";")[0].strip()
    if not stream_flag:
        stream_flag = "true" if content_type == "text/event-stream" else "false"

    # OpenAI 非流式响应：forced 路径下 vLLM 会回 DSML 标记文本作 arguments——读转 JSON 再回客户端。
    # 非流式响应本就要完整到达，读改写不引入缓冲延迟；流式仍走逐行转发不受影响。
    if path == "/v1/chat/completions" and upstream.status_code == 200 and content_type == "application/json":
        raw_response = await upstream.aread()
        await upstream.aclose()
        try:
            value = json.loads(raw_response)
        except ValueError:
            value = None
        if isinstance(value, dict) and normalize_dsml_response(value):
            metric("compat.dsml_args_normalized", request_id=rid, endpoint=path, side="response", calls=1)
            raw_response = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        finalize(status_class)
        return Response(raw_response, status_code=upstream.status_code, headers=headers)
    if content_type == "text/event-stream" and path == "/v1/chat/completions":
        body_stream = _openai_sse_rewritten(upstream.aiter_raw(), path)
    else:
        body_stream = _passthrough(upstream.aiter_raw())

    # 流式 total 观测在流真正结束时结算（含客户端提前断开→client_disconnect），不缓冲不等待。
    async def _timed() -> AsyncIterator[bytes]:
        try:
            async for chunk in body_stream:
                yield chunk
        finally:
            finalize(status_class)
    return StreamingResponse(
        _timed(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(upstream.aclose),
    )


client = httpx.AsyncClient(
    # read 3600s 对齐 nginx /v1/ 超时；透明代理不重试不跟随重定向
    timeout=httpx.Timeout(connect=5.0, read=3600.0, write=120.0, pool=10.0),
    follow_redirects=False,
)

ROUTES = [
    Route(p, compat_proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    for p in PROXY_PATHS
] + [Route("/metrics", metrics_route, methods=["GET"])]


@asynccontextmanager
async def _lifespan(_: Any) -> AsyncIterator[None]:
    yield
    await client.aclose()


app = Starlette(routes=ROUTES, lifespan=_lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=COMPAT_PORT, log_level="info", timeout_keep_alive=120)
