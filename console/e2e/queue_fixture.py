"""Loopback-only deterministic upstream for the live task view browser check."""
import asyncio
import hashlib
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

release, end = asyncio.Event(), asyncio.Event()
pending = 0
running = 0
PORT = int(os.environ["QUEUE_FIXTURE_PORT"])


async def inference(request: Request):
    global pending, running
    pending += 1
    await release.wait()
    pending -= 1
    running += 1

    async def chunks():
        global running
        try:
            yield b'data: {"choices":[{"delta":{"content":"fixture"}}],"usage":{"prompt_tokens":1234,"completion_tokens":1,"prompt_tokens_details":{"cached_tokens":256}}}\n\n'
            await end.wait()
            yield b'data: {"choices":[],"usage":{"prompt_tokens":1234,"completion_tokens":8,"prompt_tokens_details":{"cached_tokens":256}}}\n\ndata: [DONE]\n\n'
        finally:
            running -= 1
    return StreamingResponse(chunks(), media_type="text/event-stream")


async def control(request: Request):
    if request.method == "POST":
        (release if request.path_params["action"] == "release" else end).set()
    return JSONResponse({"pending": pending, "running": running})


async def data(request: Request):
    path = request.url.path
    if path == "/metrics":
        return PlainTextResponse(f"tensorfold:requests_running {running}\ntensorfold:num_requests_running {running}\n"
                                 f"tensorfold:requests_waiting {pending}\ntensorfold:num_requests_waiting {pending}\n")
    if path == "/key/list":
        return JSONResponse({"keys": [{"token": hashlib.sha256(b"sk-queue-browser").hexdigest(),
                                       "key_alias": "browser-user", "metadata": {"group": "test"}}]})
    if path == "/model/info":
        return JSONResponse({"data": [{"model_name": name, "litellm_params": {"api_base": f"http://127.0.0.1:{PORT}/v1"}}
                                      for name in ["test-deepseek", "test-alias", "GLM-5.3-Flash-EXL3", "deepseek-v4-flash",
                                                   "deepseek-v4-flash-0731", "DeepSeek-V4.1-Flash-1017"]]})
    if path == "/onboard/admin/list":
        return JSONResponse({"sites": [{"name": "test-gb10", "wg_ip": "127.0.0.1", "groups": [], "models": [],
                                       "status": "active", "pubkey": "fixture"}]})
    return JSONResponse({})


app = Starlette(routes=[Route("/v1/chat/completions", inference, methods=["POST"]),
                        Route("/test/{action}", control, methods=["GET", "POST"]),
                        Route("/{path:path}", data)])
