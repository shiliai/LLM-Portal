#!/usr/bin/env python3
"""Benchmark the real RTK CLI against redacted tool-result replays.

This adapter intentionally invokes only ``rtk pipe``.  It sends captured tool
result text on stdin and never passes a captured command, shell fragment, or
sample body to a shell.  The output contains aggregate measurements only; no
tool-result text is persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO


SCHEMA_VERSION = "context-benchmark/rtk-evaluation/v1"
DEFAULT_TIMEOUT_SECONDS = 10.0


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _text_values(value: Any) -> Iterable[str]:
    """Yield only text leaves from a tool-result content value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)
    elif isinstance(value, dict):
        # Anthropic-style blocks commonly store text in ``text`` or ``content``.
        if isinstance(value.get("text"), str):
            yield value["text"]
        elif isinstance(value.get("content"), str):
            yield value["content"]


def _tool_result_texts(body: dict[str, Any]) -> Iterable[str]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            yield from _text_values(message.get("content", ""))
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    yield from _text_values(block.get("content", ""))


def _body_from_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    replay = record.get("replay")
    body = replay.get("body") if isinstance(replay, dict) else None
    if not isinstance(body, dict):
        raise ValueError("record has no replay body")
    return body


def _resolve_executable(path: str | None) -> str | None:
    if path:
        candidate = Path(path).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which("rtk")


@dataclass
class Stats:
    records: int = 0
    records_failed: int = 0
    tool_results: int = 0
    unique_tool_results: int = 0
    rtk_successes: int = 0
    rtk_failures: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    bytes_saved: int = 0
    timeouts: int = 0
    nonzero_exit: int = 0
    invalid_output: int = 0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "records": self.records,
            "records_failed": self.records_failed,
            "tool_results": self.tool_results,
            "unique_tool_results": self.unique_tool_results,
            "rtk_successes": self.rtk_successes,
            "rtk_failures": self.rtk_failures,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "bytes_saved": self.bytes_saved,
            "percent_saved": round(self.bytes_saved * 100.0 / self.input_bytes, 3) if self.input_bytes else 0.0,
            "timeouts": self.timeouts,
            "nonzero_exit": self.nonzero_exit,
            "invalid_output": self.invalid_output,
        }


def _version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    value = result.stdout.decode("utf-8", errors="replace").strip()
    return value[:120] if value else "unknown"


def _run_rtk(executable: str, text: str, timeout_seconds: float) -> tuple[str | None, str]:
    """Run the non-executing stdin filter and return (output, failure_kind)."""
    try:
        result = subprocess.run(
            [executable, "--skip-env", "pipe"],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "exec_error"
    if result.returncode != 0:
        return None, "nonzero_exit"
    try:
        return result.stdout.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "invalid_output"


def _empty_stats() -> Stats:
    return Stats()


def benchmark(inputs: list[TextIO], executable: str | None, timeout_seconds: float, requested_commit: str) -> dict[str, Any]:
    stats = _empty_stats()
    errors: Counter[str] = Counter()
    cache: dict[str, tuple[str | None, str]] = {}
    files_seen = len(inputs)

    if executable is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "engine": "rtk-cli",
            "execution": "blocked",
            "rtk": {"requested_commit": requested_commit, "executable": None, "version": "unavailable"},
            "summary": stats.as_dict(),
            "failure_reasons": {"missing_executable": 1},
            "files_seen": files_seen,
        }

    version = _version(executable)
    for source_index, source in enumerate(inputs):
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            stats.records += 1
            try:
                record = json.loads(line)
                body = _body_from_record(record)
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                stats.records_failed += 1
                errors[type(exc).__name__] += 1
                continue

            for text in _tool_result_texts(body):
                stats.tool_results += 1
                encoded = text.encode("utf-8")
                stats.input_bytes += len(encoded)
                key = hashlib.sha256(encoded).hexdigest()
                if key not in cache:
                    cache[key] = _run_rtk(executable, text, timeout_seconds)
                    stats.unique_tool_results += 1
                output, failure = cache[key]
                if failure:
                    stats.rtk_failures += 1
                    errors[failure] += 1
                    if failure == "timeout":
                        stats.timeouts += 1
                    elif failure == "nonzero_exit":
                        stats.nonzero_exit += 1
                    elif failure == "invalid_output":
                        stats.invalid_output += 1
                    continue
                assert output is not None
                stats.rtk_successes += 1
                output_bytes = len(output.encode("utf-8"))
                stats.output_bytes += output_bytes
                stats.bytes_saved += max(0, len(encoded) - output_bytes)

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": "rtk-cli",
        "execution": "actual_rtk",
        "rtk": {
            "requested_commit": requested_commit,
            "executable": executable,
            "version": version,
            "command": ["rtk", "--skip-env", "pipe"],
            "input_contract": "tool-result text on stdin; captured commands are never executed",
        },
        "summary": stats.as_dict(),
        "failure_reasons": dict(sorted(errors.items())),
        "files_seen": files_seen,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="redacted JSONL path; repeat for multiple datasets")
    parser.add_argument("--output", default="-", help="aggregate JSON path, or - for stdout")
    parser.add_argument("--rtk", help="path to the RTK executable; defaults to PATH lookup")
    parser.add_argument("--rtk-commit", default="c75f159", help="source commit being evaluated")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    sources: list[TextIO] = []
    try:
        for input_path in args.input:
            sources.append(sys.stdin if input_path == "-" else Path(input_path).open(encoding="utf-8"))
        report = benchmark(sources, _resolve_executable(args.rtk), args.timeout_seconds, args.rtk_commit)
    finally:
        for source in sources:
            if source is not sys.stdin:
                source.close()
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(output)
    else:
        Path(args.output).write_text(output, encoding="utf-8")
    return 0 if report["execution"] == "actual_rtk" and report["summary"]["rtk_failures"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
