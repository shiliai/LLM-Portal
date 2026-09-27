#!/usr/bin/env python3
"""Evaluate deterministic context compaction policies on redacted JSONL.

The evaluator reads the replay body produced by ``collect.py`` and emits only
aggregate measurements. It never writes an optimized request body to the
report. ``raw`` and ``off`` are identical baselines; ``safe`` applies only
low-risk text rules; ``bounded`` also limits oversized tool results.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, TextIO


SCHEMA_VERSION = "context-benchmark/evaluation/v1"
DEFAULT_MAX_TOOL_RESULT_BYTES = 8192
DEFAULT_REPEAT_MIN_LINES = 2
DEFAULT_HEAD_BYTES = 4096
DEFAULT_TAIL_BYTES = 4096

_ANSI_RE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|$)|\[[0-?]*[ -/]*[@-~])")
_CODE_FENCE_RE = re.compile(r"```|^\s*(?:#!|(?:const|let|var|def|class|import|from|SELECT|INSERT|UPDATE|DELETE|curl|git|npm|python|bash)\b)", re.MULTILINE)
_REPEAT_MARKER = " [repeated x{count}]"
_STRUCTURAL_KEYS = {
    "role", "type", "name", "id", "tool_use_id", "tool_call_id", "index",
    "model", "stream", "max_tokens", "temperature", "top_p", "stop",
    "tool_choice", "function", "parameters", "input_schema",
}


@dataclass
class RuleHits:
    ansi_sequences: int = 0
    repeated_groups: int = 0
    repeated_lines_removed: int = 0
    oversized_tool_results: int = 0
    tool_result_bytes_removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "ansi_sequences": self.ansi_sequences,
            "repeated_groups": self.repeated_groups,
            "repeated_lines_removed": self.repeated_lines_removed,
            "oversized_tool_results": self.oversized_tool_results,
            "tool_result_bytes_removed": self.tool_result_bytes_removed,
        }

    def add(self, other: "RuleHits") -> None:
        for key in self.as_dict():
            setattr(self, key, getattr(self, key) + getattr(other, key))


@dataclass(frozen=True)
class PolicyConfig:
    max_tool_result_bytes: int = DEFAULT_MAX_TOOL_RESULT_BYTES
    repeat_min_lines: int = DEFAULT_REPEAT_MIN_LINES
    head_bytes: int = DEFAULT_HEAD_BYTES
    tail_bytes: int = DEFAULT_TAIL_BYTES


@dataclass
class VisitContext:
    role: str | None = None
    protected: bool = False
    tool_result: bool = False
    tool_schema: bool = False


@dataclass
class TransformStats:
    hits: RuleHits = field(default_factory=RuleHits)


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _is_json_text(value: str) -> bool:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(parsed, (dict, list))


def _looks_like_code(value: str) -> bool:
    return bool(_CODE_FENCE_RE.search(value))


def _is_image_placeholder(value: str) -> bool:
    upper = value.upper()
    return upper.startswith("[IMAGE_") and upper.endswith("]")


def _strip_ansi(value: str, hits: RuleHits) -> str:
    matches = list(_ANSI_RE.finditer(value))
    if matches:
        hits.ansi_sequences += len(matches)
    return _ANSI_RE.sub("", value)


def _fold_repeated_lines(value: str, min_lines: int, hits: RuleHits) -> str:
    lines = value.splitlines(keepends=True)
    if len(lines) < min_lines:
        return value

    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if body and index + 1 < len(lines):
            end = index + 1
            while end < len(lines) and lines[end].rstrip("\r\n") == body:
                end += 1
            count = end - index
            if count >= min_lines:
                folded = body + _REPEAT_MARKER.format(count=count) + ending
                candidate = "".join(output) + folded
                original = "".join(output) + "".join(lines[index:end])
                if len(candidate.encode("utf-8")) < len(original.encode("utf-8")):
                    output.append(folded)
                    hits.repeated_groups += 1
                    hits.repeated_lines_removed += count - 1
                    index = end
                    continue
        output.append(line)
        index += 1
    return "".join(output)


def _take_utf8_bytes(value: str, limit: int, from_end: bool = False) -> str:
    if limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    if from_end:
        return encoded[-limit:].decode("utf-8", errors="ignore")
    return encoded[:limit].decode("utf-8", errors="ignore")


def _truncate_tool_result(value: str, config: PolicyConfig, hits: RuleHits) -> str:
    original_bytes = len(value.encode("utf-8"))
    if original_bytes <= config.max_tool_result_bytes:
        return value
    omitted = max(0, original_bytes - config.head_bytes - config.tail_bytes)
    marker = f"\n...[tool output truncated; {omitted} bytes omitted]...\n"
    candidate = _take_utf8_bytes(value, config.head_bytes) + marker + _take_utf8_bytes(value, config.tail_bytes, from_end=True)
    if len(candidate.encode("utf-8")) >= original_bytes:
        return value
    hits.oversized_tool_results += 1
    hits.tool_result_bytes_removed += original_bytes - len(candidate.encode("utf-8"))
    return candidate


def _next_context(value: dict[str, Any], context: VisitContext) -> VisitContext:
    role = value.get("role") if isinstance(value.get("role"), str) else context.role
    block_type = value.get("type")
    is_image = block_type in {"image", "image_url"}
    is_tool_use = block_type in {"tool_use", "tool_call"}
    is_tool_result = block_type == "tool_result"
    return VisitContext(
        role=role,
        protected=(context.protected or role in {"system", "developer"} or is_image or is_tool_use or context.tool_schema),
        tool_result=(context.tool_result or role == "tool" or is_tool_result),
        tool_schema=(context.tool_schema or is_image or is_tool_use),
    )


def _child_context(parent: VisitContext, key: str | int, root: bool = False) -> VisitContext:
    key_text = str(key)
    protected = parent.protected
    tool_schema = parent.tool_schema
    if root and key_text == "system":
        protected = True
    if key_text in {"tools", "functions", "tool_choice", "tool_calls"}:
        tool_schema = True
    if key_text in _STRUCTURAL_KEYS or (key_text in {"arguments", "input"} and parent.tool_schema):
        protected = True
    return VisitContext(
        role=parent.role,
        protected=protected,
        tool_result=parent.tool_result,
        tool_schema=tool_schema,
    )


def _transform_text(value: str, context: VisitContext, mode: str, config: PolicyConfig, stats: TransformStats) -> str:
    if mode in {"raw", "off"} or context.protected or _is_image_placeholder(value):
        return value
    # Keep the first iteration scoped to tool output. User/assistant prose can
    # carry formatting, code, or intentional repetition that is hard to judge
    # without a model-quality check.
    if not context.tool_result:
        return value
    if _is_json_text(value) or _looks_like_code(value):
        return value

    transformed = _strip_ansi(value, stats.hits)
    transformed = _fold_repeated_lines(transformed, config.repeat_min_lines, stats.hits)
    if mode == "bounded" and context.tool_result:
        transformed = _truncate_tool_result(transformed, config, stats.hits)
    return transformed


def _transform(value: Any, context: VisitContext, mode: str, config: PolicyConfig, stats: TransformStats, root: bool = False) -> Any:
    if isinstance(value, str):
        return _transform_text(value, context, mode, config, stats)
    if isinstance(value, list):
        return [_transform(item, context, mode, config, stats) for item in value]
    if isinstance(value, dict):
        current = _next_context(value, context)
        return {
            key: _transform(child, _child_context(current, key, root=root), mode, config, stats)
            for key, child in value.items()
        }
    return value


def _body_from_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    replay = record.get("replay")
    body = replay.get("body") if isinstance(replay, dict) else None
    if not isinstance(body, dict):
        raise ValueError("record has no replay body")
    return body


def _tool_result_contents(body: dict[str, Any]) -> Iterable[Any]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            yield message.get("content", "")
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    yield block.get("content", "")


def _tool_result_bytes(body: dict[str, Any]) -> int:
    return sum(_json_bytes(content) for content in _tool_result_contents(body))


def _tool_result_count(body: dict[str, Any]) -> int:
    return sum(1 for _ in _tool_result_contents(body))


def _tool_call_count(body: dict[str, Any]) -> int:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    count = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            count += len(calls)
        content = message.get("content")
        if isinstance(content, list):
            count += sum(1 for block in content if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_call"})
    return count


def _message_invariant(body: dict[str, Any]) -> tuple[Any, ...]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ()
    result: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            result.append(("non-object",))
            continue
        content = message.get("content")
        blocks: list[Any] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    blocks.append(("dict", block.get("type"), tuple(sorted(block.keys()))))
                else:
                    blocks.append((type(block).__name__,))
        else:
            blocks.append((type(content).__name__,))
        calls = message.get("tool_calls")
        call_shape = ()
        if isinstance(calls, list):
            call_shape = tuple(
                (
                    call.get("type") if isinstance(call, dict) else None,
                    call.get("id") if isinstance(call, dict) else None,
                    tuple(sorted(call.keys())) if isinstance(call, dict) else (),
                    tuple(sorted(call.get("function", {}).keys())) if isinstance(call, dict) and isinstance(call.get("function"), dict) else (),
                )
                for call in calls
            )
        result.append((message.get("role"), tuple(sorted(message.keys())), tuple(blocks), call_shape))
    return tuple(result)


def _protected_snapshot(body: dict[str, Any]) -> dict[str, Any]:
    """Capture values whose preservation is part of the safe policy contract."""
    values: dict[str, Any] = {"messages_shape": _message_invariant(body), "structural": _structural_values(body)}
    messages = body.get("messages")
    protected: list[Any] = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") in {"system", "developer"}:
                protected.append(("system", copy.deepcopy(message.get("content"))))
            calls = message.get("tool_calls")
            if calls is not None:
                protected.append(("tool_calls", copy.deepcopy(calls)))
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_call", "image", "image_url"}:
                        protected.append((block.get("type"), copy.deepcopy(block)))
    if "system" in body:
        protected.append(("top_level_system", copy.deepcopy(body["system"])))
    values["protected"] = protected
    return values


def _structural_values(body: dict[str, Any]) -> tuple[Any, ...]:
    values: list[Any] = []
    def visit(value: Any, path: tuple[Any, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in _STRUCTURAL_KEYS:
                    values.append((path + (key,), copy.deepcopy(child)))
                visit(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (index,))
    visit(body)
    return tuple(values)


def _invariants(before: dict[str, Any], after: dict[str, Any]) -> dict[str, bool]:
    before_protected = _protected_snapshot(before)
    after_protected = _protected_snapshot(after)
    return {
        "json_valid": True,
        "message_structure_unchanged": _message_invariant(before) == _message_invariant(after),
        "tool_call_structure_unchanged": before_protected["protected"] == after_protected["protected"],
        "structural_fields_unchanged": before_protected["structural"] == after_protected["structural"],
        "structured_json_unchanged": _protected_json_strings(before) == _protected_json_strings(after),
        "code_unchanged": _protected_code_strings(before) == _protected_code_strings(after),
        "system_unchanged": before_protected == after_protected,
        "image_placeholders_unchanged": _image_values(before) == _image_values(after),
    }


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)


def _protected_json_strings(body: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(value for value in _walk_strings(body) if _is_json_text(value)))


def _protected_code_strings(body: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(value for value in _walk_strings(body) if _looks_like_code(value)))


def _image_values(body: dict[str, Any]) -> tuple[Any, ...]:
    values: list[Any] = []
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") in {"image", "image_url"}:
                values.append(copy.deepcopy(value))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(body)
    return tuple(json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values)


def _evaluate_modes(body: dict[str, Any], config: PolicyConfig) -> dict[str, dict[str, Any]]:
    raw_bytes = _json_bytes(body)
    raw_tool_bytes = _tool_result_bytes(body)
    modes: dict[str, dict[str, Any]] = {}
    for mode in ("raw", "off", "safe", "bounded"):
        stats = TransformStats()
        candidate = _transform(copy.deepcopy(body), VisitContext(), mode, config, stats, root=True)
        candidate_bytes = _json_bytes(candidate)
        if mode in {"safe", "bounded"} and candidate_bytes >= raw_bytes:
            candidate = copy.deepcopy(body)
            candidate_bytes = raw_bytes
            stats = TransformStats()
        invariants = _invariants(body, candidate)
        modes[mode] = {
            "request_bytes": candidate_bytes,
            "bytes_saved_vs_raw": raw_bytes - candidate_bytes,
            "estimated_input_tokens_bytes_div_4": round(candidate_bytes / 4.0, 2),
            "estimated_input_tokens_saved_vs_raw": round((raw_bytes - candidate_bytes) / 4.0, 2),
            "message_count": len(body.get("messages", [])) if isinstance(body.get("messages"), list) else 0,
            "tool_call_count": _tool_call_count(candidate),
            "tool_result_count": _tool_result_count(candidate),
            "tool_result_bytes": _tool_result_bytes(candidate),
            "tool_result_bytes_saved_vs_raw": raw_tool_bytes - _tool_result_bytes(candidate),
            "rule_hits": stats.hits.as_dict(),
            "invariants": invariants,
            "invariants_passed": all(invariants.values()),
            "changed": candidate != body,
        }
    return modes


def _percent(numerator: int, denominator: int) -> float:
    return round((numerator * 100.0 / denominator), 3) if denominator else 0.0


def _sample_report(record: dict[str, Any], line_no: int, config: PolicyConfig, dataset: str) -> dict[str, Any]:
    body = _body_from_record(record)
    sample_id = record.get("sample_id")
    if not isinstance(sample_id, str):
        sample_id = "line-" + hashlib.sha256(str(line_no).encode()).hexdigest()[:12]
    modes = _evaluate_modes(body, config)
    return {
        "sample_id": sample_id,
        "dataset": dataset,
        "line": line_no,
        "protocol": record.get("protocol", "unknown"),
        "modes": modes,
        "safe_vs_raw_percent": _percent(modes["safe"]["bytes_saved_vs_raw"], modes["raw"]["request_bytes"]),
        "bounded_vs_raw_percent": _percent(modes["bounded"]["bytes_saved_vs_raw"], modes["raw"]["request_bytes"]),
    }


def _summary(samples: list[dict[str, Any]], config: PolicyConfig, files_seen: int, records_failed: int, errors: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, dict[str, Any]] = {}
    rule_hits: dict[str, dict[str, int]] = {}
    invariant_failures: Counter[str] = Counter()
    for mode in ("raw", "off", "safe", "bounded"):
        totals[mode] = {
            "request_bytes": 0,
            "bytes_saved_vs_raw": 0,
            "estimated_input_tokens_bytes_div_4": 0.0,
            "estimated_input_tokens_saved_vs_raw": 0.0,
            "message_count": 0,
            "tool_call_count": 0,
            "tool_result_count": 0,
            "tool_result_bytes": 0,
            "tool_result_bytes_saved_vs_raw": 0,
            "samples_changed": 0,
        }
        rule_hits[mode] = RuleHits().as_dict()
        for sample in samples:
            measurement = sample["modes"][mode]
            for key in totals[mode]:
                if key == "samples_changed":
                    totals[mode][key] += int(bool(measurement["changed"]))
                else:
                    totals[mode][key] += int(measurement[key])
            for key, value in measurement["rule_hits"].items():
                rule_hits[mode][key] += int(value)
            for name, passed in measurement["invariants"].items():
                if not passed:
                    invariant_failures[f"{mode}.{name}"] += 1
    raw_bytes = totals["raw"]["request_bytes"]
    for mode in totals:
        totals[mode]["percent_saved_vs_raw"] = _percent(totals[mode]["bytes_saved_vs_raw"], raw_bytes)
        totals[mode]["tool_result_percent_saved_vs_raw"] = _percent(totals[mode]["tool_result_bytes_saved_vs_raw"], totals["raw"]["tool_result_bytes"])
        totals[mode]["estimated_input_tokens_percent_saved_vs_raw"] = _percent(
            totals[mode]["estimated_input_tokens_saved_vs_raw"],
            totals["raw"]["estimated_input_tokens_bytes_div_4"],
        )
    return {
        "files_seen": files_seen,
        "records_evaluated": len(samples),
        "records_failed": records_failed,
        "totals": totals,
        "rule_hits": rule_hits,
        "invariant_failures": dict(sorted(invariant_failures.items())),
        "errors": errors,
        "config": {
            "max_tool_result_bytes": config.max_tool_result_bytes,
            "repeat_min_lines": config.repeat_min_lines,
            "head_bytes": config.head_bytes,
            "tail_bytes": config.tail_bytes,
        },
    }


def evaluate(inputs: list[TextIO], config: PolicyConfig) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    records_seen = 0
    for source_index, source in enumerate(inputs):
        source_name = getattr(source, "name", "")
        dataset = Path(source_name).name if source_name else f"source-{source_index}"
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            records_seen += 1
            try:
                record = json.loads(line)
                samples.append(_sample_report(record, line_no, config, dataset))
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                errors.append({"source": source_index, "line": line_no, "reason": type(exc).__name__})
    summary = _summary(samples, config, len(inputs), len(errors), errors)
    return {"schema_version": SCHEMA_VERSION, "summary": summary, "samples": samples}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="redacted JSONL path; repeat for multiple datasets")
    parser.add_argument("--output", default="-", help="aggregate report JSON path, or - for stdout")
    parser.add_argument("--max-tool-result-bytes", type=int, default=DEFAULT_MAX_TOOL_RESULT_BYTES)
    parser.add_argument("--repeat-min-lines", type=int, default=DEFAULT_REPEAT_MIN_LINES)
    parser.add_argument("--head-bytes", type=int, default=DEFAULT_HEAD_BYTES)
    parser.add_argument("--tail-bytes", type=int, default=DEFAULT_TAIL_BYTES)
    args = parser.parse_args(argv)
    if min(args.max_tool_result_bytes, args.repeat_min_lines, args.head_bytes, args.tail_bytes) <= 0:
        parser.error("all policy limits must be positive")

    config = PolicyConfig(args.max_tool_result_bytes, args.repeat_min_lines, args.head_bytes, args.tail_bytes)
    sources: list[TextIO] = []
    try:
        for input_path in args.input:
            sources.append(sys.stdin if input_path == "-" else Path(input_path).open(encoding="utf-8"))
        report = evaluate(sources, config)
    finally:
        for source in sources:
            if source is not sys.stdin:
                source.close()
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(output)
    else:
        Path(args.output).write_text(output, encoding="utf-8")
    summary = report["summary"]
    print(
        f"context-benchmark evaluated={summary['records_evaluated']} failed={summary['records_failed']} "
        f"bounded_saved={summary['totals']['bounded']['bytes_saved_vs_raw']} bytes",
        file=sys.stderr,
    )
    return 0 if summary["records_failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
