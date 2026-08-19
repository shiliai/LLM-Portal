# -*- coding: utf-8 -*-
"""LiteLLM request-stage metrics callback (issue #62).

LiteLLM 1.96.2 invokes CustomLogger callbacks with these signatures:

* ``async_log_stream_event(kwargs, response_obj, start_time, end_time)``
* ``async_log_success_event(kwargs, response_obj, start_time, end_time)``
* ``async_log_failure_event(kwargs, response_obj, start_time, end_time)``

The proxy pre-call hook owns the initial active-attempt increment. Lifecycle
metrics are attempt-level and keyed by LiteLLM call ID; the trusted proxy request
ID is a correlation value and temporary pre-call alias only. Stream and terminal
callbacks retain real first-token timing, while generation is emitted only when
that boundary exists. State, label tuples, and stale cleanup are bounded;
instrumentation failures never affect a model request.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

try:
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover - lets the callback be unit-tested without LiteLLM installed
    class CustomLogger:  # type: ignore[no-redef]
        pass

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server
    _HAVE_PROM = True
except Exception:  # pragma: no cover - observability must remain optional
    _HAVE_PROM = False
    CollectorRegistry = Counter = Gauge = Histogram = start_http_server = None  # type: ignore


log = logging.getLogger("litellm.observability")
_PORT = int(os.environ.get("LITELLM_OBS_PORT", "48400"))

OBS_NS = "litellm"
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
_MAX_REQUEST_STATES = 10_000
_STALE_AFTER_SECONDS = 15 * 60
_JANITOR_INTERVAL_SECONDS = 60
_PRIMARY_SERIES_LIMIT = 240
_DYNAMIC_LABELS = {"model", "group", "deployment", "site"}

_REGISTRY: Any = None
_METRICS: dict[str, Any] = {}
_METRIC_SERIES: dict[str, set[tuple[tuple[str, str], ...]]] = {}
_METRIC_PRIMARY_COUNTS: dict[str, int] = {}
_SERIES_LOCK = threading.Lock()
_JANITOR_LOCK = threading.Lock()
_JANITOR_STARTED = False


def _init_metrics(*, start_server: bool = True) -> None:
    """Create a callback-private registry and, in production, its HTTP listener."""
    global _REGISTRY, _METRICS
    labels = ["model", "group", "deployment", "site", "stream"]
    with _SERIES_LOCK:
        _METRIC_SERIES.clear()
        _METRIC_PRIMARY_COUNTS.clear()
    _REGISTRY = CollectorRegistry()
    _METRICS = {
        "requests": Counter(OBS_NS + "_requests_total", "Request count", labels, registry=_REGISTRY),
        "errors": Counter(
            OBS_NS + "_errors_total", "Request failures", labels + ["status_class"], registry=_REGISTRY),
        "ttft": Histogram(OBS_NS + "_ttft_seconds", "Time to first token", labels, buckets=BUCKETS, registry=_REGISTRY),
        "generation": Histogram(
            OBS_NS + "_generation_seconds", "Generation time = total - ttft", labels, buckets=BUCKETS, registry=_REGISTRY),
        "total": Histogram(OBS_NS + "_total_seconds", "Total request time", labels, buckets=BUCKETS, registry=_REGISTRY),
        "tokens": Counter(OBS_NS + "_tokens_total", "Token usage", ["type"], registry=_REGISTRY),
        "active": Gauge(OBS_NS + "_active_requests", "Requests in flight", ["model", "site"], registry=_REGISTRY),
    }
    if start_server and start_http_server is not None:
        try:
            # Docker publishes this container port only on host loopback. It still
            # must bind all container interfaces for the published port to route.
            start_http_server(_PORT, addr="0.0.0.0", registry=_REGISTRY)
            log.info("observability_callback /metrics on 0.0.0.0:%s", _PORT)
        except Exception as exc:  # pragma: no cover - port contention must not block LiteLLM
            log.warning("observability_callback metrics server start failed: %s", exc)
    if start_server and os.environ.get("LITELLM_OBS_DISABLE_JANITOR") != "1":
        _start_janitor()


@dataclass
class _RequestState:
    key: str
    active_model: str
    active_site: str
    request_id: str
    start_time: float | None
    first_event_time: float | None
    last_seen: float


# Ordered dictionaries make eviction deterministic and keep both active and
# completed deduplication state bounded.
_REQUESTS: OrderedDict[str, _RequestState] = OrderedDict()
_FINISHED: OrderedDict[str, float] = OrderedDict()
_STATE_LOCK = threading.Lock()


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _label(value: Any, default: str = "unknown") -> str:
    """Keep metric values compact; values still come only from routing metadata."""
    if value is None:
        return default
    text = str(value).strip()
    if not text or len(text) > 128 or "\n" in text or "\r" in text:
        return default
    return text


def _stream_value(kwargs: Any) -> str:
    params = _get(kwargs, "litellm_params", {}) or {}
    value = _get(kwargs, "stream", _get(params, "stream", False))
    return "true" if value is True or str(value).lower() in {"1", "true", "yes"} else "false"


def _metadata_sources(kwargs: Any) -> list[Any]:
    params = _get(kwargs, "litellm_params", {}) or {}
    return [
        _get(params, "metadata", None),
        _get(params, "litellm_metadata", None),
        _get(kwargs, "metadata", None),
        _get(kwargs, "litellm_metadata", None),
    ]


def _labels_from(kwargs: Any) -> dict[str, str]:
    """Stable, low-cardinality dimensions from LiteLLM's routed callback data."""
    params = _get(kwargs, "litellm_params", {}) or {}
    model_info = _get(params, "model_info", _get(kwargs, "model_info", {})) or {}
    model = _label(_get(params, "model", _get(kwargs, "model", None)))
    deployment = _label(_get(model_info, "id", _get(model_info, "db_model", None)))
    site = _label(_get(model_info, "site", None), default=deployment)
    group = "unknown"
    for metadata in _metadata_sources(kwargs):
        tags = _get(metadata, "tags", None)
        if isinstance(tags, (list, tuple)) and tags:
            group = _label(tags[0])
            break
        if isinstance(tags, str) and tags:
            group = _label(tags)
            break
    return {
        "model": model,
        "group": group,
        "deployment": deployment,
        "site": site,
        "stream": _stream_value(kwargs),
    }


def _header_value(headers: Any) -> str:
    if headers is None:
        return ""
    if isinstance(headers, (list, tuple)):
        for item in headers:
            if isinstance(item, (list, tuple)) and len(item) == 2 and str(item[0]).lower() == "x-request-id":
                return _label(item[1], default="")
        return ""
    for key in ("x-request-id", "X-Request-Id", "x_request_id"):
        value = _get(headers, key, None)
        if value:
            return _label(value, default="")
    return ""


def _trusted_request_id(kwargs: Any) -> str:
    """Read only the proxy request header controlled by the gateway boundary."""
    params = _get(kwargs, "litellm_params", {}) or {}
    sources = [
        _get(params, "proxy_server_request", None),
        _get(params, "request", None),
        _get(kwargs, "proxy_server_request", None),
        _get(kwargs, "request", None),
    ]
    for source in sources:
        request_id = _header_value(source)
        if request_id:
            return request_id
        request_id = _header_value(_get(source, "headers", None))
        if request_id:
            return request_id
        request_id = _header_value(_get(source, "additional_headers", None))
        if request_id:
            return request_id
    return ""


def _request_id(kwargs: Any, response_obj: Any = None) -> str:
    """Prefer the canonical proxy header, then fall back to log-only metadata."""
    trusted = _trusted_request_id(kwargs)
    if trusted:
        return trusted
    params = _get(kwargs, "litellm_params", {}) or {}
    for source in [kwargs, params, *_metadata_sources(kwargs), response_obj]:
        for key in ("request_id", "x_request_id", "x-request-id"):
            value = _get(source, key, None)
            if value:
                return _label(value, default="")
    return ""


def _call_id(kwargs: Any) -> str:
    params = _get(kwargs, "litellm_params", {}) or {}
    logging_obj = _get(kwargs, "litellm_logging_obj", None)
    for source in (kwargs, params, logging_obj):
        call_id = _get(source, "litellm_call_id", _get(source, "call_id", None))
        if call_id:
            return str(call_id)
    return ""


def _state_keys(kwargs: Any) -> list[str]:
    """Call ID is the attempt identity; request ID is only a pre-call alias."""
    keys: list[str] = []
    call_id = _call_id(kwargs)
    request_id = _trusted_request_id(kwargs)
    if call_id:
        keys.append("call:" + call_id)
    if request_id:
        keys.append("request:" + request_id)
    return keys


def _call_key(kwargs: Any) -> str:
    keys = _state_keys(kwargs)
    return keys[0] if keys else ""


def _timestamp(value: Any) -> float | None:
    if isinstance(value, dt.datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _now(value: Any) -> float:
    return _timestamp(value) if _timestamp(value) is not None else time.time()


def _safe(fn):
    def _wrapped(*args: Any, **kwargs: Any) -> None:
        if not _HAVE_PROM or not _METRICS:
            return
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("observability_callback metric failed")
    return _wrapped


def _bounded_metric_labels(metric: str, labels: dict[str, str]) -> dict[str, str]:
    """Bound complete label tuples, not just each dimension independently."""
    key = tuple(sorted(labels.items()))
    with _SERIES_LOCK:
        seen = _METRIC_SERIES.setdefault(metric, set())
        if key in seen:
            return labels
        primary_count = _METRIC_PRIMARY_COUNTS.get(metric, 0)
        if primary_count < _PRIMARY_SERIES_LIMIT:
            seen.add(key)
            _METRIC_PRIMARY_COUNTS[metric] = primary_count + 1
            return labels
        overflow = {
            name: ("other" if name in _DYNAMIC_LABELS else value)
            for name, value in labels.items()
        }
        seen.add(tuple(sorted(overflow.items())))
        return overflow


@_safe
def _inc(metric: str, labels: dict[str, str]) -> None:
    _METRICS[metric].labels(**_bounded_metric_labels(metric, labels)).inc()


@_safe
def _observe(metric: str, labels: dict[str, str], seconds: float) -> None:
    _METRICS[metric].labels(**_bounded_metric_labels(metric, labels)).observe(max(seconds, 0.0))


@_safe
def _gauge_add(model: str, site: str, delta: int) -> None:
    labels = _bounded_metric_labels("active", {"model": model, "site": site})
    gauge = _METRICS["active"].labels(**labels)
    gauge.inc() if delta > 0 else gauge.dec()


@_safe
def _tokens(prompt_tokens: int, completion_tokens: int) -> None:
    _METRICS["tokens"].labels(type="prompt").inc(prompt_tokens)
    _METRICS["tokens"].labels(type="completion").inc(completion_tokens)


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _usage_from(kwargs: Any, response_obj: Any) -> tuple[int, int]:
    """Support LiteLLM ModelResponse.usage, dict responses, and callback kwargs."""
    params = _get(kwargs, "litellm_params", {}) or {}
    candidates = [
        _get(response_obj, "usage", None),
        _get(kwargs, "usage", None),
        _get(params, "usage", None),
        _get(_get(response_obj, "response", None), "usage", None),
    ]
    for usage in candidates:
        if usage is None:
            continue
        prompt = _token_count(_get(usage, "prompt_tokens", _get(usage, "input_tokens", None)))
        completion = _token_count(_get(usage, "completion_tokens", _get(usage, "output_tokens", None)))
        if prompt or completion:
            return prompt, completion
    return 0, 0


def _status_class(kwargs: Any, response_obj: Any) -> str:
    exception = _get(kwargs, "exception", None)
    if isinstance(exception, asyncio.CancelledError) or "cancel" in type(exception).__name__.lower():
        return "cancelled"
    for source in (response_obj, exception, kwargs):
        code = _get(source, "status_code", _get(source, "status", None))
        try:
            value = int(code)
        except (TypeError, ValueError):
            continue
        if value >= 500:
            return "5xx"
        if value >= 400:
            return "4xx"
        if value >= 300:
            return "3xx"
        return "other"
    return "other"


def _correl_log(request_id: str, event: str, kwargs: Any, **fields: Any) -> None:
    """Structured correlation log. Request IDs are deliberately never metric labels."""
    try:
        labels = _labels_from(kwargs)
        print(json.dumps({
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": event,
            "request_id": request_id,
            **labels,
            **fields,
        }, ensure_ascii=False), flush=True)
    except Exception:
        pass


def _trim_finished_locked(now: float) -> None:
    while _FINISHED:
        _, seen_at = next(iter(_FINISHED.items()))
        if len(_FINISHED) <= _MAX_REQUEST_STATES and now - seen_at <= _STALE_AFTER_SECONDS:
            break
        _FINISHED.popitem(last=False)


def _evict_stale_locked(now: float) -> list[_RequestState]:
    evicted: list[_RequestState] = []
    while _REQUESTS:
        _, state = next(iter(_REQUESTS.items()))
        if len(_REQUESTS) <= _MAX_REQUEST_STATES and now - state.last_seen <= _STALE_AFTER_SECONDS:
            break
        key, state = _REQUESTS.popitem(last=False)
        _FINISHED[key] = now  # suppress a late terminal callback from double-counting
        evicted.append(state)
    _trim_finished_locked(now)
    return evicted


def _decrement_evicted(states: list[_RequestState]) -> None:
    for state in states:
        _gauge_add(state.active_model, state.active_site, -1)


def _expire_stale_requests() -> int:
    """Expire abandoned active state even when no later request arrives."""
    with _STATE_LOCK:
        evicted = _evict_stale_locked(time.monotonic())
    _decrement_evicted(evicted)
    return len(evicted)


def _janitor_loop() -> None:  # pragma: no cover - deterministic cleanup is unit-tested directly
    while True:
        time.sleep(_JANITOR_INTERVAL_SECONDS)
        try:
            _expire_stale_requests()
        except Exception:
            log.exception("observability_callback stale-state cleanup failed")


def _start_janitor() -> None:
    global _JANITOR_STARTED
    with _JANITOR_LOCK:
        if _JANITOR_STARTED:
            return
        _JANITOR_STARTED = True
        threading.Thread(target=_janitor_loop, name="litellm-observability-janitor", daemon=True).start()


def _begin_or_get(kwargs: Any, start_time: Any = None) -> _RequestState | None:
    keys = _state_keys(kwargs)
    if not keys:
        return None
    preferred_key = keys[0]
    now_mono = time.monotonic()
    start = _timestamp(start_time)
    labels = _labels_from(kwargs)
    request_id = _request_id(kwargs)
    created = False
    with _STATE_LOCK:
        evicted = _evict_stale_locked(now_mono)
        # Only the immutable attempt key suppresses duplicate attempt callbacks.
        dedupe_key = preferred_key
        if dedupe_key in _FINISHED:
            state = None
        else:
            state = next((_REQUESTS.get(key) for key in keys if key in _REQUESTS), None)
            if state is None:
                state = _RequestState(
                    preferred_key, labels["model"], labels["site"], request_id, start, None, now_mono)
                _REQUESTS[preferred_key] = state
                created = True
            else:
                if state.key != preferred_key:
                    _REQUESTS.pop(state.key, None)
                    state.key = preferred_key
                    _REQUESTS[preferred_key] = state
                state.last_seen = now_mono
                if state.start_time is None and start is not None:
                    state.start_time = start
                if not state.request_id and request_id:
                    state.request_id = request_id
                _REQUESTS.move_to_end(preferred_key)
    _decrement_evicted(evicted)
    if created:
        _gauge_add(labels["model"], labels["site"], 1)
    return state


def _take_terminal_state(kwargs: Any, start_time: Any) -> _RequestState | None:
    """Remove one lifecycle state and atomically mark its terminal callback seen."""
    keys = _state_keys(kwargs)
    key = keys[0] if keys else ""
    now_mono = time.monotonic()
    start = _timestamp(start_time)
    labels = _labels_from(kwargs)
    request_id = _request_id(kwargs)
    transient = False
    with _STATE_LOCK:
        evicted = _evict_stale_locked(now_mono)
        if key and key in _FINISHED:
            state = None
        elif key:
            found_key = next((candidate for candidate in keys if candidate in _REQUESTS), None)
            state = _REQUESTS.pop(found_key, None) if found_key else None
            if state is None:
                # A pre-hook may be bypassed by an unusual LiteLLM entry point.
                # Preserve terminal metrics while keeping the gauge balanced.
                state = _RequestState(
                    key, labels["model"], labels["site"], request_id, start, None, now_mono)
                transient = True
            elif state.start_time is None and start is not None:
                state.start_time = start
            state.key = key
            _FINISHED[key] = now_mono
            _FINISHED.move_to_end(key)
            _trim_finished_locked(now_mono)
        else:
            # LiteLLM normally supplies litellm_call_id. Without one, avoid
            # retaining uncorrelatable state while still recording this terminal event.
            state = _RequestState(
                "", labels["model"], labels["site"], request_id, start, None, now_mono)
            transient = True
    _decrement_evicted(evicted)
    if state is not None:
        if transient:
            _gauge_add(state.active_model, state.active_site, 1)
        _gauge_add(state.active_model, state.active_site, -1)
    return state


def _completion_start(kwargs: Any) -> float | None:
    params = _get(kwargs, "litellm_params", {}) or {}
    return _timestamp(_get(kwargs, "completion_start_time", _get(params, "completion_start_time", None)))


def _has_generated_content(response_obj: Any) -> bool:
    """Reject role/metadata-only chunks when identifying the first generated token."""
    choices = _get(response_obj, "choices", None) or []
    for choice in choices:
        delta = _get(choice, "delta", None)
        for source in (delta, choice):
            for key in ("content", "text", "tool_calls", "function_call"):
                value = _get(source, key, None)
                if value not in (None, "", [], {}):
                    return True
    for key in ("output_text", "text", "content"):
        value = _get(response_obj, key, None)
        if value not in (None, "", [], {}):
            return True
    return False


class ObservabilityCallback(CustomLogger):
    """LiteLLM 1.96.2 CustomLogger callback instance."""

    def __init__(self) -> None:
        super().__init__()
        if _HAVE_PROM and not _METRICS:
            _init_metrics()

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Start lifecycle state once, before proxy routing invokes LiteLLM."""
        try:
            _begin_or_get(data)
        except Exception:
            log.exception("observability_callback pre-call state failed")
        return data

    async def async_log_stream_event(self, kwargs, response_obj, start_time, end_time) -> None:
        """Record exactly the first stream callback as TTFT."""
        try:
            state = _begin_or_get(kwargs, start_time)
            if state is None:
                return
            event_time = _completion_start(kwargs)
            if event_time is None:
                if not _has_generated_content(response_obj):
                    return
                event_time = _now(end_time)
            with _STATE_LOCK:
                current = _REQUESTS.get(state.key)
                if current is None or current.first_event_time is not None:
                    return
                current.first_event_time = event_time
                current.last_seen = time.monotonic()
                _REQUESTS.move_to_end(current.key)
                started = current.start_time if current.start_time is not None else event_time
            ttft = max(event_time - started, 0.0)
            labels = _labels_from(kwargs)
            _observe("ttft", labels, ttft)
            _correl_log(current.request_id or _request_id(kwargs, response_obj), "ll.ttft", kwargs, ttft=round(ttft, 4))
        except Exception:
            log.exception("observability_callback stream state failed")

    def _terminal_metrics(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any,
    ) -> tuple[_RequestState, dict[str, str], float, float | None] | None:
        state = _take_terminal_state(kwargs, start_time)
        if state is None:
            return None
        total = max(_now(end_time) - (state.start_time if state.start_time is not None else _now(start_time)), 0.0)
        first_event = state.first_event_time
        if first_event is None:
            first_event = _completion_start(kwargs)
        has_first_token = first_event is not None and state.start_time is not None
        ttft = max(first_event - state.start_time, 0.0) if has_first_token else None
        labels = _labels_from(kwargs)
        if state.first_event_time is None and ttft is not None:
            _observe("ttft", labels, ttft)
        _inc("requests", labels)
        _observe("total", labels, total)
        generation = max(total - ttft, 0.0) if ttft is not None else None
        if generation is not None:
            _observe("generation", labels, generation)
        return state, labels, total, generation

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        try:
            terminal = self._terminal_metrics(kwargs, response_obj, start_time, end_time)
            if terminal is None:
                return
            state, _, total, generation = terminal
            prompt_tokens, completion_tokens = _usage_from(kwargs, response_obj)
            _tokens(prompt_tokens, completion_tokens)
            timing = {"total": round(total, 4)}
            if generation is not None:
                timing["generation"] = round(generation, 4)
            _correl_log(
                state.request_id or _request_id(kwargs, response_obj),
                "ll.success",
                kwargs,
                **timing,
            )
        except Exception:
            log.exception("observability_callback success state failed")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
        try:
            terminal = self._terminal_metrics(kwargs, response_obj, start_time, end_time)
            if terminal is None:
                return
            state, labels, total, generation = terminal
            status_class = _status_class(kwargs, response_obj)
            _inc("errors", {**labels, "status_class": status_class})
            timing = {"total": round(total, 4)}
            if generation is not None:
                timing["generation"] = round(generation, 4)
            _correl_log(
                state.request_id or _request_id(kwargs, response_obj),
                "ll.failure",
                kwargs,
                status_class=status_class,
                **timing,
            )
        except Exception:
            log.exception("observability_callback failure state failed")


# LiteLLM callbacks must reference an instance, not the class.
obs_hook = ObservabilityCallback()
