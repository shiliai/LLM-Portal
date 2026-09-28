"""Deterministic, opt-in context optimization for tool-result text.

The optimizer is deliberately independent of the benchmark tooling so the
compatibility proxy can run it without importing collector code.  It is off by
default and can be enabled only for SHA-256 key identities through environment
configuration.  A candidate is used only when its compact JSON body is
smaller than the input body; all other paths return the original body.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any


MODES = {"off", "safe", "bounded"}
DEFAULT_MAX_TOOL_RESULT_BYTES = 8192
DEFAULT_REPEAT_MIN_LINES = 2
DEFAULT_HEAD_BYTES = 4096
DEFAULT_TAIL_BYTES = 4096

_ANSI_RE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")
_CODE_RE = re.compile(
    r"```|^\s*(?:#!|(?:const|let|var|def|class|import|from|SELECT|INSERT|UPDATE|DELETE|curl|git|npm|python|bash)\b)",
    re.MULTILINE,
)
_STRUCTURAL_KEYS = {
    "role", "type", "name", "id", "tool_use_id", "tool_call_id", "index",
    "model", "stream", "max_tokens", "max_completion_tokens", "temperature",
    "top_p", "stop", "tool_choice", "function", "parameters", "input_schema",
    "cache_control", "service_tier", "disable_parallel_tool_use",
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


@dataclass(frozen=True)
class OptimizerConfig:
    mode: str = "off"
    key_hashes: frozenset[str] = frozenset()
    max_tool_result_bytes: int = DEFAULT_MAX_TOOL_RESULT_BYTES
    repeat_min_lines: int = DEFAULT_REPEAT_MIN_LINES
    head_bytes: int = DEFAULT_HEAD_BYTES
    tail_bytes: int = DEFAULT_TAIL_BYTES

    @classmethod
    def from_env(cls) -> "OptimizerConfig":
        mode = os.environ.get("CONTEXT_OPTIMIZATION_MODE", "off").strip().lower()
        if mode not in MODES:
            mode = "off"
        raw_keys = os.environ.get("CONTEXT_OPTIMIZATION_KEYS", "")
        key_hashes = frozenset(
            item.strip().lower() for item in raw_keys.split(",")
            if item.strip() == "*" or re.fullmatch(r"[0-9a-fA-F]{64}", item.strip())
        )

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(os.environ.get(name, default))
            except (TypeError, ValueError):
                value = default
            return min(max(value, minimum), maximum)

        return cls(
            mode=mode,
            key_hashes=key_hashes,
            max_tool_result_bytes=integer("CONTEXT_OPTIMIZATION_MAX_TOOL_RESULT_BYTES", 8192, 512, 1024 * 1024),
            repeat_min_lines=integer("CONTEXT_OPTIMIZATION_REPEAT_MIN_LINES", 2, 2, 20),
            head_bytes=integer("CONTEXT_OPTIMIZATION_HEAD_BYTES", 4096, 0, 1024 * 1024),
            tail_bytes=integer("CONTEXT_OPTIMIZATION_TAIL_BYTES", 4096, 0, 1024 * 1024),
        )


@dataclass(frozen=True)
class VisitContext:
    role: str | None = None
    protected: bool = False
    tool_result: bool = False
    tool_schema: bool = False


@dataclass
class TransformStats:
    hits: RuleHits = field(default_factory=RuleHits)


def key_hash(credential: str) -> str:
    return hashlib.sha256((credential or "").encode()).hexdigest()


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
    return bool(_CODE_RE.search(value))


def _is_image_placeholder(value: str) -> bool:
    upper = value.upper()
    return upper.startswith("[IMAGE_") and upper.endswith("]")


def _take_utf8(value: str, limit: int, from_end: bool = False) -> str:
    if limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    chunk = encoded[-limit:] if from_end else encoded[:limit]
    return chunk.decode("utf-8", errors="ignore")


def _strip_ansi(value: str, hits: RuleHits) -> str:
    matches = list(_ANSI_RE.finditer(value))
    hits.ansi_sequences += len(matches)
    return _ANSI_RE.sub("", value)


def _fold_repeated_lines(value: str, minimum: int, hits: RuleHits) -> str:
    lines = value.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        end = index + 1
        while body and end < len(lines) and lines[end].rstrip("\r\n") == body:
            end += 1
        count = end - index
        marker = f" [repeated x{count}]"
        folded = body + marker + ending
        original = "".join(lines[index:end])
        if body and count >= minimum and len(folded.encode("utf-8")) < len(original.encode("utf-8")):
            output.append(folded)
            hits.repeated_groups += 1
            hits.repeated_lines_removed += count - 1
            index = end
        else:
            output.append(line)
            index += 1
    return "".join(output)


def _truncate(value: str, config: OptimizerConfig, hits: RuleHits) -> str:
    original_bytes = len(value.encode("utf-8"))
    if original_bytes <= config.max_tool_result_bytes:
        return value
    omitted = max(0, original_bytes - config.head_bytes - config.tail_bytes)
    marker = f"\n...[tool output truncated; {omitted} bytes omitted]...\n"
    candidate = _take_utf8(value, config.head_bytes) + marker + _take_utf8(value, config.tail_bytes, True)
    if len(candidate.encode("utf-8")) >= original_bytes:
        return value
    hits.oversized_tool_results += 1
    hits.tool_result_bytes_removed += original_bytes - len(candidate.encode("utf-8"))
    return candidate


def _next_context(value: dict[str, Any], parent: VisitContext) -> VisitContext:
    role = value.get("role") if isinstance(value.get("role"), str) else parent.role
    block_type = value.get("type")
    return VisitContext(
        role=role,
        protected=(parent.protected or role in {"system", "developer"} or block_type in {"image", "image_url", "tool_use", "tool_call"} or parent.tool_schema),
        tool_result=(parent.tool_result or role == "tool" or block_type == "tool_result"),
        tool_schema=(parent.tool_schema or block_type in {"image", "image_url", "tool_use", "tool_call"}),
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
    return VisitContext(parent.role, protected, parent.tool_result, tool_schema)


def _transform_text(value: str, context: VisitContext, config: OptimizerConfig, stats: TransformStats) -> str:
    if context.protected or not context.tool_result or _is_image_placeholder(value):
        return value
    if _is_json_text(value) or _looks_like_code(value):
        return value
    transformed = _strip_ansi(value, stats.hits)
    transformed = _fold_repeated_lines(transformed, config.repeat_min_lines, stats.hits)
    if config.mode == "bounded":
        transformed = _truncate(transformed, config, stats.hits)
    return transformed


def _transform(value: Any, context: VisitContext, config: OptimizerConfig,
               stats: TransformStats, root: bool = False) -> Any:
    if isinstance(value, str):
        return _transform_text(value, context, config, stats)
    if isinstance(value, list):
        return [_transform(item, context, config, stats) for item in value]
    if isinstance(value, dict):
        current = _next_context(value, context)
        return {key: _transform(child, _child_context(current, key, root), config, stats)
                for key, child in value.items()}
    return value


class ContextOptimizer:
    def __init__(self, config: OptimizerConfig | None = None, policy_provider: Any | None = None):
        self.config = config or OptimizerConfig.from_env()
        self.policy_provider = policy_provider

    def _active_config(self) -> OptimizerConfig:
        if self.policy_provider is None:
            return self.config
        try:
            policy = self.policy_provider()
            if isinstance(policy, dict):
                mode = str(policy.get("mode", "off")).lower()
                if mode not in MODES:
                    mode = "off"
                key_hashes = frozenset(
                    str(value).lower() for value in policy.get("keys", [])
                    if str(value) == "*" or re.fullmatch(r"[0-9a-fA-F]{64}", str(value))
                )
                return OptimizerConfig(
                    mode=mode,
                    key_hashes=key_hashes,
                    max_tool_result_bytes=int(policy.get("max_tool_result_bytes", DEFAULT_MAX_TOOL_RESULT_BYTES)),
                    repeat_min_lines=int(policy.get("repeat_min_lines", DEFAULT_REPEAT_MIN_LINES)),
                    head_bytes=int(policy.get("head_bytes", DEFAULT_HEAD_BYTES)),
                    tail_bytes=int(policy.get("tail_bytes", DEFAULT_TAIL_BYTES)),
                )
        except Exception:
            return self.config
        return self.config

    def decision(self, credential: str, config: OptimizerConfig | None = None) -> tuple[str, str]:
        config = config or self._active_config()
        digest = key_hash(credential)
        enabled = "*" in config.key_hashes or digest in config.key_hashes
        return (config.mode if enabled else "off", digest)

    def transform(self, body: dict[str, Any], credential: str) -> tuple[dict[str, Any], dict[str, Any]]:
        active_config = self._active_config()
        mode, digest = self.decision(credential, active_config)
        if mode == "off":
            return body, {"mode": mode, "enabled": False, "changed": False, "key_hash": digest[:12],
                          "raw_bytes": None, "optimized_bytes": None, "bytes_saved": 0,
                          "tool_result_bytes_saved": 0, "fallback_never_worse": False, "rule_hits": RuleHits().as_dict()}
        raw_bytes = _json_bytes(body)
        raw_tool_bytes = self._tool_result_bytes(body)
        config = OptimizerConfig(mode=mode, key_hashes=active_config.key_hashes,
                                 max_tool_result_bytes=active_config.max_tool_result_bytes,
                                 repeat_min_lines=active_config.repeat_min_lines,
                                 head_bytes=active_config.head_bytes, tail_bytes=active_config.tail_bytes)
        stats = TransformStats()
        candidate = _transform(copy.deepcopy(body), VisitContext(), config, stats, root=True)
        optimized_bytes = _json_bytes(candidate)
        if optimized_bytes >= raw_bytes:
            candidate = body
            optimized_bytes = raw_bytes
            fallback = True
        else:
            fallback = False
        return candidate, {
            "mode": mode, "enabled": True, "changed": candidate is not body,
            "key_hash": digest[:12], "raw_bytes": raw_bytes, "optimized_bytes": optimized_bytes,
            "bytes_saved": raw_bytes - optimized_bytes,
            "tool_result_bytes_saved": raw_tool_bytes - self._tool_result_bytes(candidate),
            "fallback_never_worse": fallback, "rule_hits": stats.hits.as_dict(),
        }

    @staticmethod
    def _tool_result_bytes(body: dict[str, Any]) -> int:
        total = 0
        messages = body.get("messages")
        if not isinstance(messages, list):
            return 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "tool":
                total += _json_bytes(message.get("content", ""))
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        total += _json_bytes(block.get("content", ""))
        return total
