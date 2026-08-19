#!/usr/bin/env python3
"""compat 可观测性（issue #62）：/metrics 端点、request_id 贯穿、错误响应带 rid。"""
from __future__ import annotations

import asyncio
import json
import unittest

from starlette.testclient import TestClient

import compat_proxy as cp


def histogram_count(histogram) -> float:
    return next(sample.value for sample in histogram.collect()[0].samples if sample.name.endswith("_count"))


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


class FakeStreamingUpstreamResponse(FakeUpstreamResponse):
    """提供一个可在首个 chunk 后由客户端取消的 SSE 上游。"""

    def __init__(self):
        super().__init__({"content-type": "text/event-stream"}, b"")

    async def aiter_raw(self):
        yield b"data: {\"choices\": []}\n\n"
        await asyncio.Event().wait()


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


class StreamingLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_cancellation_is_accounted_once(self):
        upstream = FakeStreamingUpstreamResponse()
        client = FakeUpstreamClient(upstream)
        original_client = cp.client
        cp.client = client  # type: ignore[assignment]
        active = cp.metrics["active"].labels(proto="openai")
        cancelled_total = cp.metrics["total"].labels(proto="openai", status_class="client_disconnect")
        active_before = active._value.get()
        cancelled_before = histogram_count(cancelled_total)
        body = json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = cp.Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
            },
            receive,
        )
        try:
            response = await cp.compat_proxy(request)
            iterator = response.body_iterator
            self.assertEqual(await anext(iterator), b"data: {\"choices\": []}\n")
            with self.assertRaises(asyncio.CancelledError):
                await iterator.athrow(asyncio.CancelledError())
            await iterator.aclose()
        finally:
            cp.client = original_client

        self.assertEqual(active._value.get(), active_before)
        self.assertEqual(histogram_count(cancelled_total), cancelled_before + 1)


if __name__ == "__main__":
    unittest.main()
