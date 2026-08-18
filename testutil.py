"""服务单测共享工具（issue #10）：不依赖网络/docker/真实 LiteLLM。

- install_litellm_stub：monkeypatch 掉 httpx.AsyncClient，回环 LiteLLM 调用全部
  走进程内桩（handler 依 (method, path, bearer) 决定响应），并记录请求供断言；
- load_service：console/onboardd/mcp-hub 都在 import 时读 env、建状态目录/SQLite，
  每个测试用独立模块名加载一份干净实例。
"""
from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx

_counter = itertools.count()


class StubResponse:
    """httpx.Response 替身：只实现服务用到的 status_code/json/text。"""

    def __init__(self, status_code: int = 200, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("stub response has no json body")
        return self._json


def install_litellm_stub(monkeypatch, handler):
    """把 httpx.AsyncClient 换成桩客户端；handler(method, path, bearer, json) ->
    (status, json_body)。返回调用记录列表（dict: method/path/bearer/json）。"""
    calls: list[dict] = []

    class _StubClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, headers=None, **kw):
            headers = headers or {}
            auth = headers.get("Authorization", "")
            bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
            record = {"method": method, "path": urlparse(url).path, "bearer": bearer,
                      "json": kw.get("json")}
            calls.append(record)
            status, payload = handler(method, record["path"], bearer, kw.get("json"))
            return StubResponse(status, payload)

        async def get(self, url, **kw):
            return await self.request("GET", url, **kw)

        async def post(self, url, **kw):
            return await self.request("POST", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    return calls


def load_service(py_path: Path | str, env: dict):
    """以全新模块名加载服务源码。env 先写入 os.environ（服务 import 时自读）。
    用唯一名避开 sys.modules 缓存，保证每个测试拿到独立状态目录/数据库。"""
    import os

    name = f"_svc_under_test_{next(_counter)}"
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, str(py_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
