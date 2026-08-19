"""Deterministic lifecycle tests for the LiteLLM observability callback."""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import prometheus_client
import pytest


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _kwargs(call_id: str, *, stream: bool, request_id: str = "rid-123") -> dict:
    return {
        "litellm_call_id": call_id,
        "model": "client-model",
        "stream": stream,
        "litellm_params": {
            "model": "routed-model",
            "model_info": {"id": "deployment-a"},
            "metadata": {"tags": ["group-a"], "request_id": request_id},
        },
    }


@pytest.fixture
def obs(monkeypatch):
    """Load an isolated callback module with real prometheus-client primitives."""
    monkeypatch.setattr(prometheus_client, "start_http_server", lambda *args, **kwargs: None)
    module_name = "observability_callback_test_" + uuid.uuid4().hex
    path = Path(__file__).parent / "observability_callback.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._REQUESTS.clear()
    module._FINISHED.clear()
    module._METRICS = {}
    module._REGISTRY = None
    module._init_metrics(start_server=False)
    return module


def _value(obs, name: str, labels: dict[str, str]) -> float:
    return obs._REGISTRY.get_sample_value(name, labels) or 0.0


def _labels(*, stream: str) -> dict[str, str]:
    return {
        "model": "routed-model",
        "group": "group-a",
        "deployment": "deployment-a",
        "stream": stream,
    }


def test_litellm_1962_callback_signatures(obs):
    hook = obs.ObservabilityCallback()
    expected = ["kwargs", "response_obj", "start_time", "end_time"]
    assert list(inspect.signature(hook.async_log_stream_event).parameters) == expected
    assert list(inspect.signature(hook.async_log_success_event).parameters) == expected
    assert list(inspect.signature(hook.async_log_failure_event).parameters) == expected
    assert set(obs._METRICS["errors"]._labelnames) == {
        "model", "group", "deployment", "stream", "status_class",
    }


def test_stream_records_first_ttft_then_total_generation_and_dict_usage(obs):
    async def scenario():
        hook = obs.ObservabilityCallback()
        kwargs = _kwargs("stream-1", stream=True)
        await hook.async_pre_call_hook(None, None, kwargs, "chat_completion")
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 1

        await hook.async_log_stream_event(kwargs, {}, _at(0), _at(2))
        await hook.async_log_stream_event(kwargs, {}, _at(0), _at(3))
        labels = _labels(stream="true")
        assert _value(obs, "litellm_ttft_seconds_count", labels) == 1
        assert _value(obs, "litellm_ttft_seconds_sum", labels) == 2

        await hook.async_log_success_event(
            kwargs,
            {"usage": {"prompt_tokens": 3, "completion_tokens": 5}},
            _at(0),
            _at(7),
        )
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 0
        assert _value(obs, "litellm_requests_total", labels) == 1
        assert _value(obs, "litellm_total_seconds_sum", labels) == 7
        assert _value(obs, "litellm_generation_seconds_sum", labels) == 5
        assert _value(obs, "litellm_tokens_total", {"type": "prompt"}) == 3
        assert _value(obs, "litellm_tokens_total", {"type": "completion"}) == 5
        assert not obs._REQUESTS

        # LiteLLM retries/callback duplication must not make a second terminal record.
        await hook.async_log_success_event(kwargs, {"usage": {"prompt_tokens": 99}}, _at(0), _at(8))
        assert _value(obs, "litellm_requests_total", labels) == 1
        assert _value(obs, "litellm_tokens_total", {"type": "prompt"}) == 3

    asyncio.run(scenario())


def test_nonstream_usage_object_and_proxy_header_request_id(obs):
    async def scenario():
        hook = obs.ObservabilityCallback()
        kwargs = _kwargs("nonstream-1", stream=False, request_id="")
        kwargs["litellm_params"]["proxy_server_request"] = SimpleNamespace(
            headers={"x-request-id": "rid-from-header"}
        )
        assert obs._request_id(kwargs) == "rid-from-header"

        await hook.async_pre_call_hook(None, None, kwargs, "chat_completion")
        await hook.async_log_success_event(
            kwargs,
            SimpleNamespace(usage=SimpleNamespace(input_tokens=7, output_tokens=11)),
            _at(10),
            _at(14),
        )
        labels = _labels(stream="false")
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 0
        assert _value(obs, "litellm_requests_total", labels) == 1
        assert _value(obs, "litellm_total_seconds_sum", labels) == 4
        assert _value(obs, "litellm_generation_seconds_sum", labels) == 4
        assert obs._REGISTRY.get_sample_value("litellm_ttft_seconds_count", labels) is None
        assert _value(obs, "litellm_tokens_total", {"type": "prompt"}) == 7
        assert _value(obs, "litellm_tokens_total", {"type": "completion"}) == 11

    asyncio.run(scenario())


def test_pre_call_uses_litellm_logging_object_call_id(obs):
    async def scenario():
        hook = obs.ObservabilityCallback()
        pre_call_data = _kwargs("unused-at-pre-call", stream=False, request_id="")
        pre_call_data.pop("litellm_call_id")
        pre_call_data["litellm_logging_obj"] = SimpleNamespace(litellm_call_id="logging-object-1")
        terminal_kwargs = _kwargs("logging-object-1", stream=False, request_id="")

        await hook.async_pre_call_hook(None, None, pre_call_data, "chat_completion")
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 1
        await hook.async_log_success_event(terminal_kwargs, {}, _at(0), _at(1))
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 0
        assert _value(obs, "litellm_requests_total", _labels(stream="false")) == 1

    asyncio.run(scenario())


def test_request_id_only_pre_call_joins_terminal_callback_with_both_ids(obs):
    async def scenario():
        hook = obs.ObservabilityCallback()
        pre_call_data = _kwargs("unused-at-pre-call", stream=False, request_id="rid-canonical")
        pre_call_data.pop("litellm_call_id")
        terminal_kwargs = _kwargs("terminal-call-id", stream=False, request_id="rid-canonical")

        await hook.async_pre_call_hook(None, None, pre_call_data, "chat_completion")
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 1
        await hook.async_log_success_event(terminal_kwargs, {}, _at(0), _at(1))

        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 0
        assert _value(obs, "litellm_requests_total", _labels(stream="false")) == 1
        assert not obs._REQUESTS
        assert "request:rid-canonical" in obs._FINISHED
        assert "call:terminal-call-id" not in obs._FINISHED

    asyncio.run(scenario())


def test_failure_cancellation_cleans_up_once_and_has_bounded_status_label(obs):
    async def scenario():
        hook = obs.ObservabilityCallback()
        kwargs = _kwargs("cancel-1", stream=True)
        kwargs["exception"] = asyncio.CancelledError()
        await hook.async_pre_call_hook(None, None, kwargs, "chat_completion")
        await hook.async_log_stream_event(kwargs, {}, _at(0), _at(1))
        await hook.async_log_failure_event(kwargs, SimpleNamespace(status_code=499), _at(0), _at(4))
        labels = _labels(stream="true")
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 0
        assert _value(obs, "litellm_requests_total", labels) == 1
        assert _value(obs, "litellm_errors_total", {**labels, "status_class": "cancelled"}) == 1
        assert _value(obs, "litellm_total_seconds_sum", labels) == 4
        assert _value(obs, "litellm_generation_seconds_sum", labels) == 3
        assert not obs._REQUESTS

        await hook.async_log_failure_event(kwargs, SimpleNamespace(status_code=499), _at(0), _at(5))
        assert _value(obs, "litellm_requests_total", labels) == 1
        assert _value(obs, "litellm_errors_total", {**labels, "status_class": "cancelled"}) == 1

    asyncio.run(scenario())


def test_stale_state_is_bounded_balanced_and_suppresses_late_terminal(obs, monkeypatch):
    async def scenario():
        clock = [0.0]
        monkeypatch.setattr(obs.time, "monotonic", lambda: clock[0])
        hook = obs.ObservabilityCallback()
        stale = _kwargs("stale-1", stream=True, request_id="rid-stale")
        await hook.async_pre_call_hook(None, None, stale, "chat_completion")
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 1

        clock[0] = obs._STALE_AFTER_SECONDS + 1
        current = _kwargs("current-1", stream=False, request_id="rid-current")
        await hook.async_pre_call_hook(None, None, current, "chat_completion")
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 1
        assert "call:stale-1" not in obs._REQUESTS

        await hook.async_log_success_event(stale, {}, _at(0), _at(999))
        assert _value(obs, "litellm_requests_total", _labels(stream="true")) == 0

        await hook.async_log_failure_event(current, SimpleNamespace(status_code=503), _at(0), _at(1))
        assert _value(obs, "litellm_active_requests", {"model": "routed-model"}) == 0
        assert _value(obs, "litellm_errors_total", {**_labels(stream="false"), "status_class": "5xx"}) == 1

    asyncio.run(scenario())


def test_metrics_listen_inside_container_and_compose_stays_loopback(obs, monkeypatch):
    seen = {}
    monkeypatch.setattr(obs, "start_http_server", lambda port, addr, registry: seen.update({
        "port": port, "addr": addr, "registry": registry,
    }))
    obs._METRICS = {}
    obs._REGISTRY = None
    obs._init_metrics()
    assert seen["port"] == obs._PORT
    assert seen["addr"] == "0.0.0.0"
    assert seen["registry"] is obs._REGISTRY

    compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    assert '"127.0.0.1:48400:48400"' in compose
