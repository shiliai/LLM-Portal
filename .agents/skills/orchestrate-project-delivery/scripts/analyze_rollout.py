#!/usr/bin/env python3
"""Summarize Codex rollout JSONL without loading the full trace into model context."""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {number}: {exc.msg}") from exc
            if isinstance(value, dict) and value.get("timestamp"):
                events.append(value)
    if not events:
        raise ValueError("rollout contains no timestamped events")
    return events


def task_bounds(events: list[dict[str, Any]], all_events: bool) -> tuple[datetime, datetime]:
    if all_events:
        return parse_time(events[0]["timestamp"]), parse_time(events[-1]["timestamp"])
    starts = [
        event
        for event in events
        if event.get("type") == "event_msg" and event.get("payload", {}).get("type") == "task_started"
    ]
    if not starts:
        return parse_time(events[0]["timestamp"]), parse_time(events[-1]["timestamp"])
    start = parse_time(starts[0]["timestamp"])
    completes = [
        event
        for event in events
        if event.get("type") == "event_msg"
        and event.get("payload", {}).get("type") == "task_complete"
        and parse_time(event["timestamp"]) >= start
    ]
    end = parse_time(completes[0]["timestamp"]) if completes else parse_time(events[-1]["timestamp"])
    return start, end


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def tool_metrics(events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    calls: dict[str, tuple[datetime, str, str]] = {}
    durations: dict[str, list[float]] = defaultdict(list)
    longest: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload", {})
        payload_type = payload.get("type")
        call_id = payload.get("call_id")
        if payload_type in {"custom_tool_call", "function_call"} and call_id:
            raw_input = payload.get("input") or payload.get("arguments") or ""
            calls[call_id] = (parse_time(event["timestamp"]), payload.get("name", "unknown"), str(raw_input))
        elif payload_type in {"custom_tool_call_output", "function_call_output"} and call_id in calls:
            started, name, raw_input = calls[call_id]
            seconds = (parse_time(event["timestamp"]) - started).total_seconds()
            durations[name].append(seconds)
            longest.append(
                {
                    "name": name,
                    "seconds": round(seconds, 3),
                    "started_at": started.isoformat(),
                    "input_excerpt": raw_input.replace("\n", " ")[:240],
                }
            )
    summary: dict[str, Any] = {}
    for name, values in durations.items():
        summary[name] = {
            "count": len(values),
            "sum_seconds": round(sum(values), 3),
            "median_seconds": round(statistics.median(values), 3),
            "p95_seconds": round(percentile(values, 0.95), 3),
            "max_seconds": round(max(values), 3),
        }
    return summary, sorted(longest, key=lambda item: item["seconds"], reverse=True)[:20]


def token_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    token_events = [
        event
        for event in events
        if event.get("type") == "event_msg" and event.get("payload", {}).get("type") == "token_count"
    ]
    if not token_events:
        return {}
    usage = token_events[-1]["payload"].get("info", {}).get("total_token_usage", {})
    input_tokens = int(usage.get("input_tokens", 0))
    cached_tokens = int(usage.get("cached_input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    return {
        **usage,
        "effective_tokens": input_tokens - cached_tokens + output_tokens,
        "input_cache_rate": round(cached_tokens / input_tokens, 4) if input_tokens else 0,
    }


def compaction_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one canonical event per compaction across rollout schema variants."""
    compacted = [event for event in events if event.get("type") == "compacted"]
    if compacted:
        return compacted
    return [
        event
        for event in events
        if event.get("type") == "event_msg"
        and event.get("payload", {}).get("type") == "context_compacted"
    ]


def compaction_windows(events: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    compacted = compaction_events(events)
    compacted_times: list[datetime] = []
    compacted_payloads: dict[datetime, dict[str, Any]] = {}
    for event in compacted:
        when = parse_time(event["timestamp"])
        if start < when < end:
            compacted_times.append(when)
            compacted_payloads[when] = event.get("payload", {})
    compacted_times.sort()
    boundaries = [start, *compacted_times, end]
    latest_usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    windows: list[dict[str, Any]] = []
    for index in range(len(boundaries) - 1):
        lower, upper = boundaries[index], boundaries[index + 1]
        window_events = [
            event for event in events if lower <= parse_time(event["timestamp"]) < upper
        ]
        tokens = [
            event
            for event in window_events
            if event.get("type") == "event_msg" and event.get("payload", {}).get("type") == "token_count"
        ]
        effective_delta = 0
        if tokens:
            usage = tokens[-1]["payload"].get("info", {}).get("total_token_usage", {})
            current = {key: int(usage.get(key, 0)) for key in latest_usage}
            delta = {key: current[key] - latest_usage[key] for key in latest_usage}
            effective_delta = delta["input_tokens"] - delta["cached_input_tokens"] + delta["output_tokens"]
            latest_usage = current
        tools = Counter(
            event.get("payload", {}).get("name")
            for event in window_events
            if event.get("type") == "response_item"
            and event.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
        )
        payload = compacted_payloads.get(upper, {})
        windows.append(
            {
                "index": index + 1,
                "start": lower.isoformat(),
                "end": upper.isoformat(),
                "minutes": round((upper - lower).total_seconds() / 60, 2),
                "effective_tokens": effective_delta,
                "agent_messages": sum(
                    1
                    for event in window_events
                    if event.get("type") == "event_msg"
                    and event.get("payload", {}).get("type") == "agent_message"
                ),
                "agents_started": sum(
                    1
                    for event in window_events
                    if event.get("type") == "event_msg"
                    and event.get("payload", {}).get("type") == "sub_agent_activity"
                    and event.get("payload", {}).get("kind") == "started"
                ),
                "exec_calls": tools.get("exec", 0),
                "wait_agent_calls": tools.get("wait_agent", 0),
                "compaction_summary_chars": len(payload.get("message", "")),
                "replacement_history_chars": len(
                    json.dumps(payload.get("replacement_history", []), ensure_ascii=False)
                )
                if payload
                else 0,
            }
        )
    return windows


def child_metrics(events: list[dict[str, Any]], sessions_root: Path) -> dict[str, Any]:
    spawn_requests: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload", {})
        tool_name = payload.get("name")
        if (
            payload.get("type") != "function_call"
            or not isinstance(tool_name, str)
            or not (tool_name == "spawn_agent" or tool_name.endswith("__spawn_agent"))
        ):
            continue
        try:
            arguments = json.loads(payload.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        task_name = arguments.get("task_name")
        spawn_requests.append(
            {
                "tool_call_id": payload.get("call_id"),
                "tool_name": tool_name,
                "task_name": task_name.rsplit("/", 1)[-1]
                if isinstance(task_name, str) and task_name
                else None,
                "agent_type": arguments.get("agent_type"),
                "fork_turns": arguments.get("fork_turns"),
                "fork_context": arguments.get("fork_context"),
            }
        )

    starts = [
        event
        for event in events
        if event.get("type") == "event_msg"
        and event.get("payload", {}).get("type") == "sub_agent_activity"
        and event.get("payload", {}).get("kind") == "started"
    ]
    rows: list[dict[str, Any]] = []
    unused_spawn_indexes = set(range(len(spawn_requests)))
    for event in starts:
        payload = event["payload"]
        thread_id = payload.get("agent_thread_id")
        agent_path = payload.get("agent_path")
        task_name = agent_path.rsplit("/", 1)[-1] if isinstance(agent_path, str) else None
        matching_index = next(
            (
                index
                for index in unused_spawn_indexes
                if task_name and spawn_requests[index].get("task_name") == task_name
            ),
            None,
        )
        if matching_index is None:
            matching_index = min(unused_spawn_indexes) if unused_spawn_indexes else None
        spawn_request = spawn_requests[matching_index] if matching_index is not None else {}
        if matching_index is not None:
            unused_spawn_indexes.remove(matching_index)
        matches = glob.glob(str(sessions_root / "**" / f"*{thread_id}*.jsonl"), recursive=True)
        if not matches:
            rows.append(
                {
                    "task": payload.get("agent_path"),
                    "thread_id": thread_id,
                    "requested_agent_type": spawn_request.get("agent_type"),
                    "fork_turns": spawn_request.get("fork_turns"),
                    "fork_context": spawn_request.get("fork_context"),
                    "observed_agent_role": "missing",
                    "model": "missing",
                    "effort": "missing",
                }
            )
            continue
        child_events: list[dict[str, Any]] = []
        for match in matches:
            child_events.extend(load_events(Path(match)))
        child_events.sort(key=lambda item: item["timestamp"])
        context = next((item for item in child_events if item.get("type") == "turn_context"), {})
        session_meta = next((item for item in child_events if item.get("type") == "session_meta"), {})
        source = session_meta.get("payload", {}).get("source", {})
        thread_spawn = (
            source.get("subagent", {}).get("thread_spawn", {})
            if isinstance(source, dict)
            else {}
        )
        child_start, child_end = task_bounds(child_events, all_events=True)
        rows.append(
            {
                "task": agent_path,
                "thread_id": thread_id,
                "spawn_tool_call_id": spawn_request.get("tool_call_id"),
                "requested_agent_type": spawn_request.get("agent_type"),
                "fork_turns": spawn_request.get("fork_turns"),
                "fork_context": spawn_request.get("fork_context"),
                "observed_agent_role": thread_spawn.get("agent_role"),
                "model": context.get("payload", {}).get("model", "unknown"),
                "effort": context.get("payload", {}).get("effort", "unknown"),
                "minutes": round((child_end - child_start).total_seconds() / 60, 2),
                "tokens": token_metrics(child_events),
                "compactions": len(compaction_events(child_events)),
            }
        )
    return {
        "count": len(rows),
        "model_counts": dict(Counter(row["model"] for row in rows)),
        "effort_counts": dict(Counter(row["effort"] for row in rows)),
        "effective_tokens": sum(row.get("tokens", {}).get("effective_tokens", 0) for row in rows),
        "compactions": sum(row.get("compactions", 0) for row in rows),
        "tasks": rows,
    }


def analyze(path: Path, all_events: bool, include_children: bool, sessions_root: Path) -> dict[str, Any]:
    events = load_events(path)
    start, end = task_bounds(events, all_events)
    selected = [event for event in events if start <= parse_time(event["timestamp"]) <= end]
    event_types = Counter(event.get("type") for event in selected)
    message_types = Counter(
        event.get("payload", {}).get("type")
        for event in selected
        if event.get("type") == "event_msg"
    )
    tools, longest = tool_metrics(selected)
    result: dict[str, Any] = {
        "rollout": str(path),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_seconds": round((end - start).total_seconds(), 3),
        "event_counts": dict(event_types),
        "event_message_counts": dict(message_types),
        "tokens": token_metrics(selected),
        "tools": tools,
        "longest_tool_calls": longest,
        "compactions": len(compaction_events(selected)),
        "compaction_windows": compaction_windows(selected, start, end),
    }
    if include_children:
        result["children"] = child_metrics(selected, sessions_root)
    return result


def print_text(result: dict[str, Any]) -> None:
    print(f"Rollout: {result['rollout']}")
    print(f"Window: {result['start']} -> {result['end']}")
    print(f"Duration: {result['duration_seconds'] / 3600:.2f} hours")
    tokens = result.get("tokens", {})
    if tokens:
        print(
            "Tokens: "
            f"effective={tokens.get('effective_tokens')} "
            f"raw_total={tokens.get('total_tokens')} "
            f"cache_rate={tokens.get('input_cache_rate'):.2%}"
        )
    messages = result.get("event_message_counts", {})
    print(
        "Events: "
        f"agent_messages={messages.get('agent_message', 0)} "
        f"compactions={result['compactions']} "
        f"sub_agent_activity={messages.get('sub_agent_activity', 0)}"
    )
    print("Tools:")
    for name, metrics in sorted(
        result.get("tools", {}).items(), key=lambda item: item[1]["sum_seconds"], reverse=True
    ):
        print(
            f"- {name}: count={metrics['count']} sum={metrics['sum_seconds']}s "
            f"median={metrics['median_seconds']}s p95={metrics['p95_seconds']}s"
        )
    children = result.get("children")
    if children:
        print(
            f"Children: count={children['count']} effective_tokens={children['effective_tokens']} "
            f"compactions={children['compactions']} models={children['model_counts']} "
            f"efforts={children['effort_counts']}"
        )
    print("Compaction windows:")
    for window in result.get("compaction_windows", []):
        print(
            f"- {window['index']}: {window['minutes']}m, effective_tokens={window['effective_tokens']}, "
            f"exec={window['exec_calls']}, agents={window['agents_started']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rollout", type=Path)
    parser.add_argument("--all-events", action="store_true", help="analyze the full file instead of the first task")
    parser.add_argument("--include-children", action="store_true")
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = analyze(args.rollout, args.all_events, args.include_children, args.sessions_root)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_text(result)


if __name__ == "__main__":
    main()
