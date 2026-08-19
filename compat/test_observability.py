#!/usr/bin/env python3
"""compat 可观测性（issue #62）：/metrics 端点、request_id 贯穿、错误响应带 rid。"""
from __future__ import annotations

import json
import unittest

from starlette.testclient import TestClient

import compat_proxy as cp


class FakeUpstreamResponse:
    """模拟 LiteLLM 非流式 JSON 200 响应，供请求级测试。"""

    def __init__(self, headers: dict, body: bytes):
        self.status_code = 200
        self.headers = headers
        self._body = body
        self.sent_headers = None  # 捕获 build_request 的 headers

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        pass

    async def aiter_raw(self):  # pragma: no cover - 非流式不走到
        yield self._body
        return


class FakeUpstreamClient:
    """替换 cp.client：捕获转发头 + 返回固定响应。"""

    def __init__(self, upstream: FakeUpstreamResponse):
        self.upstream = upstream
        self.last_headers = None
        self.last_url = None

    def build_request(self, method, url, headers, content):
        self.last_url = url
        self.last_headers = dict(headers)
        return {"url": url, "headers": headers, "content": content}

    async def send(self, request, stream=True):
        return self.upstream

    async def aclose(self):
        pass


class MetricsEndpointTest(unittest.TestCase):
    def test_metrics_route_exposes_registry(self):
        with TestClient(cp.app) as client:
            r = client.get("/metrics")
        self.assertEqual(r.status_code, 200)
        self.assertIn("compat_requests_total", r.text)
        self.assertIn("compat_parse_seconds", r.text)

    def test_status_class(self):
        self.assertEqual(cp._status_class(200), "2xx")
        self.assertEqual(cp._status_class(301), "3xx")
        self.assertEqual(cp._status_class(404), "4xx")
        self.assertEqual(cp._status_class(503), "5xx")


class ErrorResponseRequestIdTest(unittest.TestCase):
    def test_error_response_includes_rid(self):
        resp = cp.error_response(cp.CompatReject(400, "forced_tool_choice_unsupported", "boom"), "openai", rid="abc123")
        self.assertEqual(resp.headers.get("x-request-id"), "abc123")

    def test_error_response_no_rid_omits_header(self):
        resp = cp.error_response(cp.CompatReject(400, "x", "boom"), "openai")
        self.assertNotIn("x-request-id", resp.headers)


class RequestIdPassthroughTest(unittest.TestCase):
    BODY = json.dumps({"model": "m", "max_tokens": 4, "messages": [{"role": "user", "content": "hi"}]}).encode()

    def test_forwarded_and_echoed(self):
        fake = FakeUpstreamResponse({"content-type": "application/json"}, b'{"choices": []}')
        client = FakeUpstreamClient(fake)
        orig = cp.client
        cp.client = client  # type: ignore[assignment]
        try:
            with TestClient(cp.app) as tc:
                r = tc.post(
                    "/v1/chat/completions",
                    content=self.BODY,
                    headers={"x-request-id": "rid-from-nginx", "content-type": "application/json"},
                )
        finally:
            cp.client = orig
        self.assertEqual(r.status_code, 200)
        # 透传给上游
        self.assertEqual(client.last_headers.get("x-request-id"), "rid-from-nginx")
        # 回传客户端
        self.assertEqual(r.headers.get("x-request-id"), "rid-from-nginx")

    def test_generated_when_absent(self):
        fake = FakeUpstreamResponse({"content-type": "application/json"}, b'{"choices": []}')
        client = FakeUpstreamClient(fake)
        orig = cp.client
        cp.client = client  # type: ignore[assignment]
        try:
            with TestClient(cp.app) as tc:
                r = tc.post(
                    "/v1/chat/completions",
                    content=self.BODY,
                    headers={"content-type": "application/json"},
                )
        finally:
            cp.client = orig
        self.assertEqual(r.status_code, 200)
        forwarded = client.last_headers.get("x-request-id")
        self.assertTrue(forwarded)
        self.assertEqual(r.headers.get("x-request-id"), forwarded)
        self.assertEqual(len(forwarded), 32)  # uuid4().hex


if __name__ == "__main__":
    unittest.main()
