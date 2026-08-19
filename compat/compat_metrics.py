# -*- coding: utf-8 -*-
"""compat 协议层 Prometheus 指标（issue #62）。

分段：parse（请求读取+解析/变换）、upstream_header（发到 LiteLLM 到拿到响应头）、
total（全请求，含流式 streaming 完成）；另含 requests/errors/active。
标签低基数（README D7）：endpoint / proto / stream / status_class；无 request_id/调用方。
独立 CollectorRegistry；不随响应缓冲（SSE 仍逐行转发）。
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

REGISTRY = CollectorRegistry()
_NS = "compat"
# 固定小桶：覆盖 sub-ms 网关开销到分钟级慢请求；低基数
BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

metrics = {
    "requests": Counter(
        _NS + "_requests_total", "代理请求总数", ["endpoint", "proto", "stream", "status_class"], registry=REGISTRY),
    "errors": Counter(
        _NS + "_errors_total", "错误数（拒绝/上游失败）", ["cause"], registry=REGISTRY),
    "active": Gauge(
        _NS + "_active_requests", "活跃在途请求", ["proto"], registry=REGISTRY),
    "parse": Histogram(
        _NS + "_parse_seconds", "请求读取+解析/变换耗时", ["proto"], buckets=BUCKETS, registry=REGISTRY),
    "upstream_header": Histogram(
        _NS + "_upstream_header_seconds", "发到 LiteLLM 到拿到响应头耗时", ["endpoint", "proto"], buckets=BUCKETS, registry=REGISTRY),
    "total": Histogram(
        _NS + "_total_seconds", "全请求耗时（含流式完成）", ["proto", "status_class"], buckets=BUCKETS, registry=REGISTRY),
}


def metrics_response() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def status_class(code: int) -> str:
    if code >= 500:
        return "5xx"
    if code >= 400:
        return "4xx"
    if code >= 300:
        return "3xx"
    return "2xx"
