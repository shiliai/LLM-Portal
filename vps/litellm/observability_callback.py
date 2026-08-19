# -*- coding: utf-8 -*-
"""private-llm 全链路分段可观测性——LiteLLM 自定义回调（issue #62）。

与 vps/litellm/group_routing.py 同机制挂载（litellm_settings.callbacks），在 LiteLLM
进程内补齐 LiteLLM 原生 /metrics 不给的阶段：

  * TTFT（首 Token）：async_log_stream_event 首个 chunk 打点（start_time -> 首 chunk）
  * 生成阶段：total - TTFT（流式与全响应一致口径，见 README D3）
  * 请求总耗时 / token / 错误（按 model / group / deployment / stream / status_class）

设计约束（README D7/D8）：
  * 指标标签低基数：model / group / deployment / stream / status_class；**不做 request_id/caller**。
  * request_id（X-Request-Id）只进结构化日志行与历史，用于跨层对账，**不作为标签**。
  * 全程 try/except 兜底：任何观测异常都不允许中断模型请求（监控故障不影响模型路径）。
  * 独立 CollectorRegistry + 独立 HTTP 端点（默认 :48400），与 LiteLLM 自带 registry 不冲突。

挂载（部署时）：
  1. 把本文件拷入 litellm 容器可导入目录（与 config.yaml/group_routing.py 同卷：/app/proxy）。
  2. vps/litellm/config.yaml 的 litellm_settings.callbacks 追加 "observability_callback.obs_hook"。
  3. LiteLLM main-stable 自带 prometheus_client。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server
    _HAVE_PROM = True
except Exception:  # pragma: no cover - 非 LiteLLM 环境（本地单测）
    _HAVE_PROM = False
    CollectorRegistry = Counter = Gauge = Histogram = start_http_server = None  # type: ignore

log = logging.getLogger("litellm.observability")
_PORT = int(os.environ.get("LITELLM_OBS_PORT", "48400"))

OBS_NS = "litellm"
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

_REGISTRY: Any = None
_METRICS: dict = {}


def _init_metrics() -> None:
    global _REGISTRY, _METRICS
    labels = ["model", "group", "deployment", "stream"]
    _REGISTRY = CollectorRegistry()
    _METRICS = {
        "requests": Counter(OBS_NS + "_requests_total", "请求总数", labels, registry=_REGISTRY),
        "errors": Counter(OBS_NS + "_errors_total", "错误数", labels + ["status_class", "error_type"], registry=_REGISTRY),
        "ttft": Histogram(OBS_NS + "_ttft_seconds", "首 Token 耗时", labels, buckets=BUCKETS, registry=_REGISTRY),
        "generation": Histogram(OBS_NS + "_generation_seconds", "生成阶段 = total - ttft", labels, buckets=BUCKETS, registry=_REGISTRY),
        "total": Histogram(OBS_NS + "_total_seconds", "请求总耗时", labels, buckets=BUCKETS, registry=_REGISTRY),
        "tokens": Counter(OBS_NS + "_tokens_total", "token 用量", ["type"], registry=_REGISTRY),
        "active": Gauge(OBS_NS + "_active_requests", "活跃在途请求", ["model"], registry=_REGISTRY),
    }
    if start_http_server is not None:
        try:
            start_http_server(_PORT, addr="127.0.0.1", registry=_REGISTRY)
            log.info("observability_callback /metrics on 127.0.0.1:%s", _PORT)
        except Exception as exc:  # pragma: no cover
            log.warning("observability_callback metrics server start failed: %s", exc)


# 首 Token 去重池（有界，防泄漏；仅进程内存）
_FIRST_SEEN: set[str] = set()
_FIRST_LOCK = threading.Lock()


def _labels_from(kwargs: dict) -> dict:
    """低基数标签（尽力而为）。model/group(metadata.tags[0])/deployment(model_info.id)/stream。"""
    lp = kwargs.get("litellm_params") or {}
    model = str(lp.get("model") or kwargs.get("model") or "unknown")
    mi = lp.get("model_info") or kwargs.get("model_info") or {}
    dep = str(mi.get("id") or mi.get("db_model") or "") if isinstance(mi, dict) else ""
    group = ""
    md = lp.get("metadata") or kwargs.get("metadata") or {}
    if isinstance(md, dict):
        tags = md.get("tags")
        if isinstance(tags, list) and tags:
            group = str(tags[0])
    stream = str(bool(kwargs.get("stream") or lp.get("stream"))).lower()
    return {"model": model, "group": group, "deployment": dep, "stream": stream}


def _safe(fn):
    def _wrapped(*a, **k):
        if not _HAVE_PROM or _METRICS is None:
            return
        try:
            fn(*a, **k)
        except Exception:
            log.exception("observability_callback metric failed")
    return _wrapped


@_safe
def _inc(metric: str, labels: dict) -> None:
    _METRICS[metric].labels(**labels).inc()


@_safe
def _observe(metric: str, labels: dict, seconds: float) -> None:
    _METRICS[metric].labels(**labels).observe(seconds)


@_safe
def _gauge_add(model: str, delta: int) -> None:
    g = _METRICS["active"].labels(model=model)
    g.inc() if delta > 0 else g.dec()


@_safe
def _tokens(p: int, c: int) -> None:
    _METRICS["tokens"].labels(type="prompt").inc(p)
    _METRICS["tokens"].labels(type="completion").inc(c)


def _request_id(kwargs: dict, response_obj: Any = None) -> str:
    try:
        if isinstance(response_obj, dict) and response_obj.get("request_id"):
            return str(response_obj["request_id"])
        lp = kwargs.get("litellm_params") or {}
        md = lp.get("metadata") or kwargs.get("metadata") or {}
        if isinstance(md, dict) and md.get("request_id"):
            return str(md["request_id"])
        return str(kwargs.get("litellm_call_id") or "")
    except Exception:
        return ""


def _start_ts(start_time: Any) -> float:
    if isinstance(start_time, dt.datetime):
        return start_time.timestamp()
    if isinstance(start_time, (int, float)):
        return float(start_time)
    return 0.0


def _correl_log(rid: str, event: str, kwargs: dict, **fields: Any) -> None:
    """结构化 JSON 日志（stdout -> docker logs）携带 request_id 供跨层对账；不含敏感字段。"""
    try:
        lb = _labels_from(kwargs)
        print(json.dumps({
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": event,
            "request_id": rid,
            "model": lb["model"], "group": lb["group"],
            "deployment": lb["deployment"], "stream": lb["stream"],
            **fields,
        }, ensure_ascii=False), flush=True)
    except Exception:
        pass


class ObservabilityCallback:
    """LiteLLM CustomLogger 实现（挂 instance：obs_hook）。"""
    INSTANCES: list["ObservabilityCallback"] = []

    def __init__(self) -> None:
        self.INSTANCES.append(self)
        # 进程内只初始化一次（_METRICS 从空 dict 起步，非 None）
        if _HAVE_PROM and not _METRICS:
            _init_metrics()

    async def async_log_stream_event(self, event, kwargs, response_obj, start_time, end_time) -> None:
        # 活跃在途 +1；TTFT 首 chunk 打点
        rid = _request_id(kwargs, response_obj)
        _gauge_add(_labels_from(kwargs)["model"], 1)
        call_id = str(kwargs.get("litellm_call_id") or "")
        if call_id:
            with _FIRST_LOCK:
                if call_id in _FIRST_SEEN:
                    return
                _FIRST_SEEN.add(call_id)
                if len(_FIRST_SEEN) > 10000:
                    _FIRST_SEEN.clear()
        now = end_time.timestamp() if isinstance(end_time, dt.datetime) else dt.datetime.now(dt.timezone.utc).timestamp()
        ttft = max(now - _start_ts(start_time), 0.0)
        _observe("ttft", _labels_from(kwargs), ttft)
        _correl_log(rid, "ll.ttft", kwargs, ttft=round(ttft, 4))
        return

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        labels = _labels_from(kwargs)
        rid = _request_id(kwargs, response_obj)
        _gauge_add(labels["model"], -1)
        now = end_time.timestamp() if isinstance(end_time, dt.datetime) else dt.datetime.now(dt.timezone.utc).timestamp()
        total = max(now - _start_ts(start_time), 0.0)
        # 非流式无首 chunk：口径上 TTFT 记 0（不把非流式当慢 TTFT）；生成段=total
        ttft = 0.0 if str(kwargs.get("stream")).lower() != "true" else 0.0
        generation = max(total - ttft, 0.0)
        _inc("requests", labels)
        _observe("total", labels, total)
        _observe("generation", labels, generation)
        try:
            usage = kwargs.get("usage") or {}
            _tokens(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        except Exception:
            pass
        _correl_log(rid, "ll.success", kwargs, total=round(total, 4), generation=round(generation, 4))
        return

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        labels = _labels_from(kwargs)
        rid = _request_id(kwargs, response_obj)
        _gauge_add(labels["model"], -1)
        try:
            sc = response_obj.status_code if hasattr(response_obj, "status_code") else (
                response_obj.get("status_code") if isinstance(response_obj, dict) else 500)
        except Exception:
            sc = 500
        status_class = "5xx" if (sc and sc >= 500) else ("4xx" if (sc and sc >= 400) else "other")
        err_type = "unknown"
        exc = kwargs.get("exception")
        if exc is not None:
            err_type = type(exc).__name__
        _inc("errors", {**labels, "status_class": status_class, "error_type": err_type})
        _correl_log(rid, "ll.failure", kwargs, status=sc, error_type=err_type)
        return


# LiteLLM callbacks 须指向实例而非类（与 group_routing_hook 一致）
obs_hook = ObservabilityCallback()
