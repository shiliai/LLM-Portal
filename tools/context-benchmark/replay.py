#!/usr/bin/env python3
"""Replay redacted context snapshots against an OpenAI-compatible endpoint.

The runner is intentionally a measurement harness, not a production proxy.  It
accepts only collector output (``replay.body``), applies one deterministic
candidate strategy, and sends the resulting body to the configured endpoint.
Response bodies are parsed in memory for protocol and usage fields and are
never written to the report.  Captured tool output is passed to RTK through
stdin only; no captured command is ever executed.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

try:  # Running as a script from this directory.
    from evaluate import (
        PolicyConfig,
        TransformStats,
        VisitContext,
        _json_bytes,
        _tool_result_bytes,
        _transform,
    )
except ImportError:  # pragma: no cover - package import fallback
    from .evaluate import (
        PolicyConfig,
        TransformStats,
        VisitContext,
        _json_bytes,
        _tool_result_bytes,
        _transform,
    )


SCHEMA_VERSION = "context-benchmark/replay/v1"
DEFAULT_TIMEOUT_SECONDS = 180.0
STRATEGIES = ("off", "safe", "bounded", "rtk")


def _tool_result_count(body: dict[str, Any]) -> int:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    count = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            count += 1
        content = message.get("content")
        if isinstance(content, list):
            count += sum(
                1 for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            )
    return count


def _record_body(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    replay = record.get("replay")
    body = replay.get("body") if isinstance(replay, dict) else None
    if not isinstance(body, dict):
        raise ValueError("record has no replay body")
    return body


def _record_id(record: dict[str, Any], dataset: str, line_no: int) -> str:
    value = record.get("sample_id")
    if isinstance(value, str) and value:
        return value
    return "line-" + hashlib.sha256(f"{dataset}:{line_no}".encode()).hexdigest()[:12]


def _invalid_image_payload(body: dict[str, Any]) -> bool:
    """Return true when a replay snapshot contains an unusable image payload."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image_url":
                image_url = block.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if not isinstance(url, str) or url == "[IMAGE_OMITTED]":
                    return True
                if url.startswith("data:"):
                    payload = url.split(",", 1)[1] if "," in url else ""
                    try:
                        base64.b64decode(payload, validate=True)
                    except (ValueError, TypeError, base64.binascii.Error):
                        return True
            if block.get("type") == "image":
                source = block.get("source")
                data = source.get("data") if isinstance(source, dict) else None
                if not isinstance(data, str) or data == "[IMAGE_OMITTED]":
                    return True
                try:
                    base64.b64decode(data, validate=True)
                except (ValueError, TypeError, base64.binascii.Error):
                    return True
    return False


def _walk_tool_texts(value: Any, replace: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return replace(value)
    if isinstance(value, list):
        return [_walk_tool_texts(item, replace) for item in value]
    if isinstance(value, dict):
        return {key: _walk_tool_texts(child, replace) for key, child in value.items()}
    return value


def _replace_rtk_tool_results(body: dict[str, Any], replace: Callable[[str], str]) -> dict[str, Any]:
    """Replace text leaves under OpenAI ``role=tool`` and Anthropic blocks."""
    result = copy.deepcopy(body)
    messages = result.get("messages")
    if not isinstance(messages, list):
        return result
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            message["content"] = _walk_tool_texts(message.get("content", ""), replace)
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    block["content"] = _walk_tool_texts(block.get("content", ""), replace)
    return result


@dataclass
class Candidate:
    body: dict[str, Any]
    request_bytes: int
    raw_request_bytes: int
    tool_result_bytes: int
    raw_tool_result_bytes: int
    changed: bool = False
    fallback_never_worse: bool = False
    rule_hits: dict[str, int] = field(default_factory=dict)
    rtk_failures: int = 0
    rtk_cache_hits: int = 0


class RtkFilter:
    def __init__(self, executable: str, timeout_seconds: float):
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.cache: dict[str, tuple[str | None, str]] = {}
        self.calls = 0
        self.cache_hits = 0
        self.failures: Counter[str] = Counter()

    def apply(self, value: str) -> str:
        encoded = value.encode("utf-8")
        key = hashlib.sha256(encoded).hexdigest()
        cached = self.cache.get(key)
        if cached is not None:
            output, failure = cached
            self.cache_hits += 1
        else:
            self.calls += 1
            try:
                result = subprocess.run(
                    [self.executable, "--skip-env", "pipe"],
                    input=encoded,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=self.timeout_seconds,
                    shell=False,
                )
                if result.returncode != 0:
                    output, failure = None, "nonzero_exit"
                else:
                    output, failure = result.stdout.decode("utf-8"), ""
            except subprocess.TimeoutExpired:
                output, failure = None, "timeout"
            except (OSError, UnicodeDecodeError):
                output, failure = None, "exec_error"
            self.cache[key] = (output, failure)
        if failure:
            self.failures[failure] += 1
            return value
        assert output is not None
        # A filter that grows a result cannot improve the request.  Keep the
        # original text at the leaf level and let the request-level guard below
        # handle any remaining overhead.
        if len(output.encode("utf-8")) >= len(encoded):
            return value
        return output


def make_candidate(body: dict[str, Any], strategy: str, config: PolicyConfig,
                   rtk: RtkFilter | None = None) -> Candidate:
    raw_request_bytes = _json_bytes(body)
    raw_tool_bytes = _tool_result_bytes(body)
    if strategy in {"off", "raw"}:
        candidate = copy.deepcopy(body)
        return Candidate(candidate, raw_request_bytes, raw_request_bytes, raw_tool_bytes, raw_tool_bytes)
    if strategy in {"safe", "bounded"}:
        stats = TransformStats()
        candidate = _transform(copy.deepcopy(body), VisitContext(), strategy, config, stats, root=True)
        request_bytes = _json_bytes(candidate)
        if request_bytes >= raw_request_bytes:
            candidate = copy.deepcopy(body)
            request_bytes = raw_request_bytes
            return Candidate(
                candidate, request_bytes, raw_request_bytes, raw_tool_bytes, raw_tool_bytes,
                fallback_never_worse=True, rule_hits=stats.hits.as_dict(),
            )
        return Candidate(
            candidate, request_bytes, raw_request_bytes, _tool_result_bytes(candidate), raw_tool_bytes,
            changed=candidate != body, rule_hits=stats.hits.as_dict(),
        )
    if strategy != "rtk":
        raise ValueError(f"unknown strategy: {strategy}")
    if rtk is None:
        raise ValueError("rtk strategy requires an executable")
    failures_before = sum(rtk.failures.values())
    cache_hits_before = rtk.cache_hits
    candidate = _replace_rtk_tool_results(body, rtk.apply)
    request_bytes = _json_bytes(candidate)
    failures = sum(rtk.failures.values()) - failures_before
    cache_hits = rtk.cache_hits - cache_hits_before
    if request_bytes >= raw_request_bytes:
        candidate = copy.deepcopy(body)
        request_bytes = raw_request_bytes
        return Candidate(
            candidate, request_bytes, raw_request_bytes, raw_tool_bytes, raw_tool_bytes,
            fallback_never_worse=True, rtk_failures=failures, rtk_cache_hits=cache_hits,
        )
    return Candidate(
        candidate, request_bytes, raw_request_bytes, _tool_result_bytes(candidate), raw_tool_bytes,
        changed=candidate != body, rtk_failures=failures, rtk_cache_hits=cache_hits,
    )


def _endpoint(base_url: str, record: dict[str, Any]) -> str:
    replay = record.get("replay")
    path = replay.get("endpoint") if isinstance(replay, dict) else None
    if not isinstance(path, str) or not path.startswith("/"):
        path = "/v1/chat/completions"
    return base_url.rstrip("/") + path


def _usage(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get("usage")
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
        if isinstance(value.get(name), int):
            result[name] = value[name]
    return result


def _stream_result(response: Any, started: float) -> dict[str, Any]:
    first_event_ms: float | None = None
    done = False
    event_count = 0
    usage: dict[str, int] = {}
    finish_reasons: list[str] = []
    tool_ids: set[str] = set()
    tool_argument_fragments: dict[str, list[str]] = defaultdict(list)
    while True:
        line = response.readline()
        if not line:
            break
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        try:
            payload = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        event_count += 1
        if first_event_ms is None:
            first_event_ms = (time.perf_counter() - started) * 1000
        usage.update(_usage(payload))
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                finish_reasons.append(reason)
            delta = choice.get("delta")
            calls = delta.get("tool_calls") if isinstance(delta, dict) else None
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                index = str(call.get("index", call_id or "unknown"))
                if isinstance(call_id, str):
                    tool_ids.add(call_id)
                function = call.get("function")
                if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                    tool_argument_fragments[index].append(function["arguments"])
    valid_json = True
    for fragments in tool_argument_fragments.values():
        value = "".join(fragments)
        try:
            json.loads(value)
        except (ValueError, TypeError, json.JSONDecodeError):
            valid_json = False
    return {
        "first_event_ms": round(first_event_ms, 2) if first_event_ms is not None else None,
        "sse_complete": done,
        "event_count": event_count,
        "usage": usage,
        "finish_reason": finish_reasons[-1] if finish_reasons else None,
        "tool_call_count": len(tool_ids) if tool_ids else len(tool_argument_fragments),
        "tool_call_json_valid": valid_json if tool_argument_fragments else None,
    }


def send_request(url: str, body: dict[str, Any], api_key: str, timeout_seconds: float) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if body.get("stream") else "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            if body.get("stream"):
                result = _stream_result(response, started)
            else:
                raw = response.read()
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, TypeError, json.JSONDecodeError):
                    parsed = None
                result = {
                    "first_event_ms": None,
                    "sse_complete": None,
                    "event_count": 0,
                    "usage": _usage(parsed),
                    "finish_reason": (
                        parsed.get("choices", [{}])[0].get("finish_reason")
                        if isinstance(parsed, dict) and isinstance(parsed.get("choices"), list) and parsed["choices"]
                        and isinstance(parsed["choices"][0], dict) else None
                    ),
                    "tool_call_count": 0,
                    "tool_call_json_valid": None,
                    "response_json_valid": parsed is not None,
                }
            result["http_status"] = status
            result["status"] = "ok" if 200 <= status < 300 else "http_error"
    except urllib.error.HTTPError as exc:
        result = {"http_status": int(exc.code), "status": "http_error", "error_kind": "http_error"}
    except urllib.error.URLError as exc:
        result = {"http_status": None, "status": "error", "error_kind": "url_error"}
    except TimeoutError:
        result = {"http_status": None, "status": "error", "error_kind": "timeout"}
    except Exception as exc:  # Keep the report aggregate-only and continue the batch.
        result = {"http_status": None, "status": "error", "error_kind": type(exc).__name__}
    result["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    result.setdefault("first_event_ms", None)
    result.setdefault("sse_complete", None)
    result.setdefault("event_count", 0)
    result.setdefault("usage", {})
    result.setdefault("finish_reason", None)
    result.setdefault("tool_call_count", 0)
    result.setdefault("tool_call_json_valid", None)
    return result


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round((percentile / 100) * (len(values) - 1)))))
    return round(values[index], 2)


def _aggregate(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        groups[(attempt["dataset"], attempt["strategy"])].append(attempt)
    output: dict[str, Any] = {}
    for (dataset, strategy), values in sorted(groups.items()):
        successful = [value for value in values if value["status"] == "ok"]
        ttft = [value["first_event_ms"] for value in successful if value.get("first_event_ms") is not None]
        total = [value["total_ms"] for value in successful]
        prompt = [value["usage"].get("prompt_tokens") for value in successful if value["usage"].get("prompt_tokens") is not None]
        completion = [value["usage"].get("completion_tokens") for value in successful if value["usage"].get("completion_tokens") is not None]
        key = f"{dataset}:{strategy}"
        output[key] = {
            "records": len(values),
            "successful": len(successful),
            "failed": len(values) - len(successful),
            "skipped": sum(1 for value in values if value["status"] == "skipped"),
            "request_bytes": sum(value["request_bytes"] for value in values),
            "raw_request_bytes": sum(value["raw_request_bytes"] for value in values),
            "bytes_saved_vs_raw": sum(value["raw_request_bytes"] - value["request_bytes"] for value in values),
            "prompt_tokens_exact": {"count": len(prompt), "sum": sum(prompt), "avg": round(statistics.mean(prompt), 2) if prompt else None},
            "completion_tokens_exact": {"count": len(completion), "sum": sum(completion), "avg": round(statistics.mean(completion), 2) if completion else None},
            "ttft_ms": {"count": len(ttft), "p50": _percentile(ttft, 50), "p95": _percentile(ttft, 95)},
            "total_ms": {"count": len(total), "p50": _percentile(total, 50), "p95": _percentile(total, 95)},
            "sse_complete": sum(1 for value in successful if value.get("sse_complete") is True),
            "tool_call_json_invalid": sum(1 for value in successful if value.get("tool_call_json_valid") is False),
            "error_kinds": dict(sorted(Counter(value.get("error_kind", "") for value in values if value["status"] != "ok").items())),
        }
        if output[key]["raw_request_bytes"]:
            output[key]["percent_saved_vs_raw"] = round(output[key]["bytes_saved_vs_raw"] * 100 / output[key]["raw_request_bytes"], 3)
    return output


def replay(inputs: list[TextIO], base_url: str, api_key: str, strategies: list[str],
           config: PolicyConfig, timeout_seconds: float, limit_per_input: int,
           rtk_path: str | None) -> dict[str, Any]:
    records: list[tuple[str, int, dict[str, Any]]] = []
    files_seen = len(inputs)
    load_errors: list[dict[str, Any]] = []
    for source in inputs:
        dataset = Path(getattr(source, "name", "snapshot")).name
        count = 0
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            if limit_per_input and count >= limit_per_input:
                break
            try:
                record = json.loads(line)
                _record_body(record)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                load_errors.append({"dataset": dataset, "line": line_no, "error": type(exc).__name__})
                continue
            records.append((dataset, line_no, record))
            count += 1
    executable = None
    if rtk_path:
        candidate = Path(rtk_path).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate)
    else:
        executable = shutil.which("rtk")
    rtk = RtkFilter(executable, timeout_seconds) if executable else None
    attempts: list[dict[str, Any]] = []
    for dataset, line_no, record in records:
        body = _record_body(record)
        invalid_image = _invalid_image_payload(body)
        for strategy in strategies:
            started = time.perf_counter()
            if invalid_image:
                attempts.append({
                    "sample_id": _record_id(record, dataset, line_no),
                    "dataset": dataset,
                    "line": line_no,
                    "strategy": strategy,
                    "protocol": record.get("protocol", "unknown"),
                    "request_bytes": _json_bytes(body),
                    "raw_request_bytes": _json_bytes(body),
                    "tool_result_bytes": _tool_result_bytes(body),
                    "raw_tool_result_bytes": _tool_result_bytes(body),
                    "changed": False,
                    "fallback_never_worse": False,
                    "rule_hits": {},
                    "rtk_failures": 0,
                    "rtk_cache_hits": 0,
                    "status": "skipped",
                    "error_kind": "invalid_image_payload",
                    "http_status": None,
                    "first_event_ms": None,
                    "total_ms": round((time.perf_counter() - started) * 1000, 2),
                    "sse_complete": None,
                    "event_count": 0,
                    "usage": {},
                    "finish_reason": None,
                    "tool_call_count": 0,
                    "tool_call_json_valid": None,
                })
                continue
            try:
                candidate = make_candidate(body, strategy, config, rtk)
                if strategy == "rtk" and rtk is None:
                    raise ValueError("missing_rtk_executable")
                outcome = send_request(_endpoint(base_url, record), candidate.body, api_key, timeout_seconds)
                attempt = {
                    "sample_id": _record_id(record, dataset, line_no),
                    "dataset": dataset,
                    "line": line_no,
                    "strategy": strategy,
                    "protocol": record.get("protocol", "unknown"),
                    "request_bytes": candidate.request_bytes,
                    "raw_request_bytes": candidate.raw_request_bytes,
                    "tool_result_bytes": candidate.tool_result_bytes,
                    "raw_tool_result_bytes": candidate.raw_tool_result_bytes,
                    "changed": candidate.changed,
                    "fallback_never_worse": candidate.fallback_never_worse,
                    "rule_hits": candidate.rule_hits,
                    "rtk_failures": candidate.rtk_failures,
                    "rtk_cache_hits": candidate.rtk_cache_hits,
                    **outcome,
                }
            except Exception as exc:
                attempt = {
                    "sample_id": _record_id(record, dataset, line_no),
                    "dataset": dataset,
                    "line": line_no,
                    "strategy": strategy,
                    "protocol": record.get("protocol", "unknown"),
                    "request_bytes": 0,
                    "raw_request_bytes": _json_bytes(body),
                    "tool_result_bytes": 0,
                    "raw_tool_result_bytes": _tool_result_bytes(body),
                    "changed": False,
                    "fallback_never_worse": False,
                    "rule_hits": {},
                    "rtk_failures": 0,
                    "rtk_cache_hits": 0,
                    "status": "error",
                    "error_kind": str(exc) if str(exc) == "missing_rtk_executable" else type(exc).__name__,
                    "http_status": None,
                    "first_event_ms": None,
                    "total_ms": round((time.perf_counter() - started) * 1000, 2),
                    "sse_complete": None,
                    "event_count": 0,
                    "usage": {},
                    "finish_reason": None,
                    "tool_call_count": 0,
                    "tool_call_json_valid": None,
                }
            attempts.append(attempt)
    return {
        "schema_version": SCHEMA_VERSION,
        "execution": "actual_replay",
        "request": {"base_url": base_url, "strategies": strategies, "timeout_seconds": timeout_seconds},
        "inputs": {"files_seen": files_seen, "records_loaded": len(records), "load_errors": load_errors},
        "rtk": {
            "executable": executable,
            "calls": rtk.calls if rtk else 0,
            "cache_hits": rtk.cache_hits if rtk else 0,
            "failures": dict(sorted(rtk.failures.items())) if rtk else ({"missing_executable": 1} if "rtk" in strategies else {}),
        },
        "summary": _aggregate(attempts),
        "attempts": attempts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="OPF-redacted JSONL; repeat for datasets")
    parser.add_argument("--output", default="-", help="aggregate JSON report path, or - for stdout")
    parser.add_argument("--base-url", default=os.environ.get("REPLAY_BASE_URL", "http://127.0.0.1:4000"))
    parser.add_argument("--api-key", default=os.environ.get("REPLAY_API_KEY", ""), help="prefer REPLAY_API_KEY in the environment")
    parser.add_argument("--strategies", default="off,safe,bounded,rtk", help="comma-separated strategies")
    parser.add_argument("--rtk", help="RTK executable path; defaults to PATH lookup")
    parser.add_argument("--limit-per-input", type=int, default=0, help="pilot limit per file; 0 means all")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-tool-result-bytes", type=int, default=8192)
    parser.add_argument("--head-bytes", type=int, default=4096)
    parser.add_argument("--tail-bytes", type=int, default=4096)
    args = parser.parse_args(argv)
    strategies = [value.strip().lower() for value in args.strategies.split(",") if value.strip()]
    if not strategies or any(value not in STRATEGIES for value in strategies):
        parser.error(f"strategies must be drawn from {', '.join(STRATEGIES)}")
    if args.limit_per_input < 0 or args.timeout_seconds <= 0:
        parser.error("limit-per-input must be non-negative and timeout-seconds must be positive")
    config = PolicyConfig(max_tool_result_bytes=args.max_tool_result_bytes, head_bytes=args.head_bytes, tail_bytes=args.tail_bytes)
    sources: list[TextIO] = []
    try:
        for path in args.input:
            sources.append(Path(path).open(encoding="utf-8"))
        report = replay(sources, args.base_url, args.api_key, strategies, config, args.timeout_seconds, args.limit_per_input, args.rtk)
    finally:
        for source in sources:
            source.close()
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(output)
    else:
        Path(args.output).write_text(output, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
