#!/usr/bin/env python3
"""Collect redacted context benchmark samples from JSONL fixtures.

The collector never writes the input body, OPF spans, or credentials. It keeps
text in memory only long enough to send it to OPF and emits a replayable body
with redacted message content plus aggregate metadata.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


SCHEMA_VERSION = "context-benchmark/v1"
DEFAULT_OPF_URL = os.environ.get("OPF_URL", "http://192.168.88.75:8765")
MAX_TEXT_BYTES = 256 * 1024
MAX_BATCH = 100
REDACTION_RETRIES = 2
STRUCTURAL_KEYS = {
    "role", "type", "name", "id", "tool_use_id", "tool_call_id", "index",
    "model", "stream", "max_tokens", "temperature", "top_p", "stop",
    "tool_choice", "function", "parameters", "input_schema",
    "disable_parallel_tool_use", "cache_control", "service_tier",
}
TOP_LEVEL_ENVELOPE_KEYS = {
    "headers", "authorization", "api_key", "apikey", "request_id", "session_id",
    "trace_id", "client_ip", "ip", "host", "user", "metadata",
}
class CollectionError(RuntimeError):
    """A safe, non-content-bearing collection error."""


@dataclass
class TextSlot:
    path: tuple[Any, ...]
    value: str


def _safe_endpoint(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    return parsed.path or (value if value.startswith("/") else "")


def _protocol(endpoint: str, body: dict[str, Any]) -> str:
    if endpoint.endswith("/messages") or endpoint.endswith("/messages/count_tokens"):
        return "anthropic_messages"
    if "system" in body and "messages" in body and "max_tokens" in body and "tools" not in body:
        return "anthropic_messages"
    return "openai_chat"


def _unwrap_record(record: Any) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Return (body, safe metadata source, endpoint) without retaining headers."""
    if not isinstance(record, dict):
        raise CollectionError("record must be a JSON object")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if isinstance(record.get("request"), dict):
        request = record["request"]
        body = request.get("body") if isinstance(request.get("body"), dict) else request
        endpoint = _safe_endpoint(request.get("url") or request.get("endpoint"))
    else:
        body = record.get("body") if isinstance(record.get("body"), dict) else record
        endpoint = _safe_endpoint(record.get("endpoint") or record.get("url"))
    if not isinstance(body, dict):
        raise CollectionError("request body must be an object")
    return copy.deepcopy(body), metadata, endpoint


def _strip_top_level_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """Drop request identity/envelope keys without touching nested tool data."""
    return {
        key: value
        for key, value in body.items()
        if str(key).lower() not in TOP_LEVEL_ENVELOPE_KEYS
    }


def _content_type(value: Any) -> str:
    if isinstance(value, str):
        return "text"
    if not isinstance(value, dict):
        return "unknown"
    block_type = value.get("type")
    if block_type in {"image", "image_url"}:
        return "image"
    if block_type in {"tool_result", "tool_use", "tool_call"}:
        return str(block_type)
    if block_type in {"text", "input_text", "output_text"}:
        return "text"
    return str(block_type or "object")


def _walk_text_slots(value: Any, path: tuple[Any, ...] = (), key: str | None = None) -> Iterable[TextSlot]:
    """Find text leaves in messages and tool definitions.

    Binary image payloads are omitted later and never sent to OPF. Structural
    identifiers remain untouched; textual content, descriptions, and tool
    arguments are redacted in memory.
    """
    if isinstance(value, str):
        if key in {"data", "url"} and path and any(part == "source" for part in path if isinstance(part, str)):
            return
        if key in STRUCTURAL_KEYS:
            return
        yield TextSlot(path, value)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk_text_slots(item, path + (i,), key)
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key in {"data", "url"} and value.get("type") in {"image", "image_url"}:
                continue
            yield from _walk_text_slots(child, path + (child_key,), child_key)


def _get_path(value: Any, path: tuple[Any, ...]) -> Any:
    for part in path:
        value = value[part]
    return value


def _set_path(value: Any, path: tuple[Any, ...], replacement: str) -> None:
    parent = _get_path(value, path[:-1])
    parent[path[-1]] = replacement


def _replace_images(value: Any, path: tuple[Any, ...] = (), counter: Counter | None = None) -> None:
    if isinstance(value, list):
        for i, item in enumerate(value):
            _replace_images(item, path + (i,), counter)
    elif isinstance(value, dict):
        if value.get("type") in {"image", "image_url"}:
            if counter is not None:
                counter["image_blocks"] += 1
            for key in ("data", "url"):
                if key in value:
                    value[key] = "[IMAGE_OMITTED]"
            source = value.get("source")
            if isinstance(source, dict):
                for key in ("data", "url"):
                    if key in source:
                        source[key] = "[IMAGE_OMITTED]"
            image_url = value.get("image_url")
            if isinstance(image_url, dict) and "url" in image_url:
                image_url["url"] = "[IMAGE_OMITTED]"
        else:
            for key, child in value.items():
                _replace_images(child, path + (key,), counter)


def _safe_metadata(metadata: dict[str, Any], body: dict[str, Any], endpoint: str) -> dict[str, Any]:
    """Whitelist benchmark dimensions and discard IDs, addresses, and headers."""
    out: dict[str, Any] = {}
    fields = {
        "client": ("client", "client_name"),
        "client_version": ("client_version",),
        "portal_version": ("portal_version",),
        "litellm_version": ("litellm_version",),
        "upstream_version": ("upstream_version",),
        "deployment_label": ("deployment_label",),
        "context_window": ("context_window",),
        "strategy_version": ("strategy_version",),
        "config_version": ("config_version",),
        "cache_state": ("cache_state",),
        "task_label": ("task_label",),
    }
    for target, names in fields.items():
        for name in names:
            candidate = metadata.get(name)
            if isinstance(candidate, (str, int, float, bool)):
                out[target] = candidate
                break
    if isinstance(body.get("model"), str):
        out["model"] = body["model"]
    if endpoint:
        out["endpoint"] = endpoint
    return out


class OpfClient:
    def __init__(self, base_url: str, timeout: float = 20.0, retries: int = REDACTION_RETRIES):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.info: dict[str, Any] = {}

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, bytes, str]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json", "accept": "application/json, text/plain"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(), response.headers.get("content-type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("content-type", "")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CollectionError(f"OPF transport error: {type(exc).__name__}") from exc

    def fetch_info(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for path, keep in (("/health", ("ready", "version", "device")), ("/model-info", ("version", "decode_backend", "decode_mode", "output_mode"))):
            request = urllib.request.Request(self.base_url + path, headers={"accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    value = json.loads(response.read())
                if isinstance(value, dict):
                    result[path.lstrip("/").replace("-", "_")] = {key: value.get(key) for key in keep if key in value}
            except Exception as exc:
                result[path.lstrip("/").replace("-", "_")] = {"status": "unavailable", "error": type(exc).__name__}
        self.info = result
        return result

    @staticmethod
    def _parse_batch(raw: bytes, content_type: str, count: int) -> list[str]:
        try:
            decoded = json.loads(raw) if "json" in content_type or raw[:1] in (b"[", b"{") else raw.decode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CollectionError("OPF returned invalid response") from exc
        items = decoded if isinstance(decoded, list) else decoded.get("results", decoded.get("texts", [])) if isinstance(decoded, dict) else []
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list) or len(items) != count:
            raise CollectionError("OPF returned an unexpected batch shape")
        out: list[str] = []
        for item in items:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and isinstance(item.get("redacted_text"), str):
                out.append(item["redacted_text"])
            else:
                raise CollectionError("OPF batch item has no redacted text")
        return out

    def redact(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        for text in texts:
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise CollectionError("OPF local size guard: text exceeds 256KB")
        out: list[str] = []
        for start in range(0, len(texts), MAX_BATCH):
            batch = texts[start:start + MAX_BATCH]
            attempts = 0
            while True:
                status, raw, content_type = self._post("/redact/batch", {"texts": batch})
                if status == 200:
                    out.extend(self._parse_batch(raw, content_type, len(batch)))
                    break
                if status == 503 and attempts < self.retries:
                    attempts += 1
                    time.sleep(0.2 * attempts)
                    continue
                if status == 413:
                    raise CollectionError("OPF rejected payload: 413")
                if status == 422:
                    raise CollectionError("OPF rejected payload: 422")
                if status == 503:
                    raise CollectionError("OPF unavailable after retries: 503")
                raise CollectionError(f"OPF request failed: HTTP {status}")
        return out


def _aggregate(body: dict[str, Any], redacted_body: dict[str, Any], slots: list[TextSlot], replacements: list[str]) -> dict[str, Any]:
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    roles = Counter(m.get("role", "unknown") for m in messages if isinstance(m, dict))
    types = Counter()
    tool_result_count = 0
    tool_chars = 0
    max_tool_bytes = 0
    cache_control = False
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        parts = content if isinstance(content, list) else [content]
        for part in parts:
            types[_content_type(part)] += 1
            if isinstance(part, dict) and part.get("type") == "tool_result":
                tool_result_count += 1
                raw = json.dumps(part.get("content", ""), ensure_ascii=False)
                size = len(raw.encode("utf-8"))
                tool_chars += len(raw)
                max_tool_bytes = max(max_tool_bytes, size)
            if isinstance(part, dict) and part.get("cache_control"):
                cache_control = True
    raw_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    safe_bytes = len(json.dumps(redacted_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return {
        "message_count": len(messages),
        "role_counts": dict(sorted(roles.items())),
        "tool_result_count": tool_result_count,
        "tool_result_chars": tool_chars,
        "max_tool_result_bytes": max_tool_bytes,
        "content_type_counts": dict(sorted(types.items())),
        "text_slot_count": len(slots),
        "redacted_slot_count": sum(a != b for a, b in zip((s.value for s in slots), replacements)),
        "has_cache_control": cache_control,
        "long_session": len(messages) >= 12,
        "raw_request_bytes": raw_bytes,
        "redacted_request_bytes": safe_bytes,
        "request_bytes_saved": raw_bytes - safe_bytes,
        "text_bytes": sum(len(s.value.encode("utf-8")) for s in slots),
        "redacted_text_bytes": sum(len(v.encode("utf-8")) for v in replacements),
    }


def collect_record(record: Any, client: OpfClient | None, index: int, assume_input_redacted: bool) -> dict[str, Any]:
    body, metadata, endpoint = _unwrap_record(record)
    protocol = _protocol(endpoint, body)
    replay_body = _strip_top_level_envelope(body)
    slots = list(_walk_text_slots(replay_body))
    redacted = copy.deepcopy(replay_body)
    image_counts = Counter()
    _replace_images(redacted, counter=image_counts)
    if assume_input_redacted:
        replacements = [slot.value for slot in slots]
        redaction_status = "assumed_input_redacted"
        opf_info: dict[str, Any] = {}
    else:
        if client is None:
            raise CollectionError("OPF client is required unless --assume-input-redacted is set")
        replacements = client.redact([slot.value for slot in slots])
        for slot, replacement in zip(slots, replacements):
            _set_path(redacted, slot.path, replacement)
        redaction_status = "opf"
        opf_info = client.info
    aggregate = _aggregate(body, redacted, slots, replacements)
    aggregate["image_blocks"] = image_counts["image_blocks"]
    safe_meta = _safe_metadata(metadata, body, endpoint)
    canonical = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    sample_id = "ctx-" + hashlib.sha256(f"{index}:".encode() + canonical.encode()).hexdigest()[:16]
    return {
        "schema_version": SCHEMA_VERSION,
        "sample_id": sample_id,
        "protocol": protocol,
        "redaction": {"status": redaction_status, "opf": opf_info},
        "metadata": safe_meta,
        "aggregate": aggregate,
        "replay": {"endpoint": endpoint, "body": redacted},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="JSONL/NDJSON input path, or - for stdin")
    parser.add_argument("--output", default="-", help="redacted JSONL output path, or - for stdout")
    parser.add_argument("--manifest", default="", help="aggregate manifest JSON path; omitted means stderr summary only")
    parser.add_argument("--opf-url", default=DEFAULT_OPF_URL)
    parser.add_argument("--assume-input-redacted", action="store_true", help="synthetic fixtures only; skip OPF")
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args(argv)

    source = sys.stdin if args.input == "-" else Path(args.input).open(encoding="utf-8")
    target = sys.stdout if args.output == "-" else Path(args.output).open("w", encoding="utf-8")
    client = None if args.assume_input_redacted else OpfClient(args.opf_url)
    if client is not None:
        client.fetch_info()
    total = passed = failed = 0
    sums = Counter()
    try:
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            if args.max_records and total >= args.max_records:
                break
            total += 1
            try:
                record = json.loads(line)
                output = collect_record(record, client, line_no, args.assume_input_redacted)
                target.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
                target.flush()
                passed += 1
                for key in (
                    "message_count", "tool_result_count", "tool_result_chars", "max_tool_result_bytes",
                    "text_slot_count", "redacted_slot_count", "raw_request_bytes", "redacted_request_bytes",
                    "request_bytes_saved", "text_bytes", "redacted_text_bytes", "image_blocks",
                ):
                    sums[key] += int(output["aggregate"].get(key, 0) or 0)
            except (json.JSONDecodeError, CollectionError, KeyError, TypeError, ValueError) as exc:
                failed += 1
                print(f"context-benchmark record={line_no} status=error reason={type(exc).__name__}", file=sys.stderr)
                if not args.continue_on_error:
                    return 2
    finally:
        if source is not sys.stdin:
            source.close()
        if target is not sys.stdout:
            target.close()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "records_seen": total,
        "records_written": passed,
        "records_failed": failed,
        "aggregate_sums": dict(sorted(sums.items())),
        "redaction": {
            "mode": "assumed_input_redacted" if args.assume_input_redacted else "opf",
            "opf": client.info if client is not None else {},
        },
    }
    if args.manifest:
        Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"context-benchmark records={total} passed={passed} failed={failed}", file=sys.stderr)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
