"""Deterministic, opt-in context optimization for tool-result text and image history.

The optimizer is deliberately independent of the benchmark tooling so the
compatibility proxy can run it without importing collector code.  It is off by
default and can be enabled only for SHA-256 key identities through environment
configuration.  A candidate is used only when its compact JSON body is
smaller than the input body; all other paths return the original body.

Image history eviction (issue #166, absorbing #98) runs in two tiers:

- guard (all keys): model groups configured with a max_images limit get their
  oldest images replaced by text placeholders only when the request would
  otherwise exceed the upstream limit and fail with a 400.  The compact-JSON
  never-worse guard does not apply -- this tier exists to rescue requests that
  are already doomed, and image removal is provably net-negative in bytes.
- canary (allowlisted keys, safe/bounded): evict down to keep_last so each
  kept image retains the backend's full per-image token budget.

The last user message's images are never touched unless leaving them would
still exceed max_images (survival beats the invariant).  Placeholders reuse
the ``[IMAGE_...]`` marker convention so text rules and replay tooling skip
them, and their bytes stay identical across rounds for the same session
index, keeping the eviction boundary monotonic for upstream prefix caches.
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
DEFAULT_IMAGE_KEEP_LAST = 8
MAX_IMAGE_LIMIT = 256

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
    image_limits: dict[str, int] = field(default_factory=dict)
    image_keep_last: int = DEFAULT_IMAGE_KEEP_LAST
    image_guard: bool = True

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
            image_limits=parse_image_limits(os.environ.get("CONTEXT_OPTIMIZATION_IMAGE_LIMITS", "")),
            image_keep_last=integer("CONTEXT_OPTIMIZATION_IMAGE_KEEP_LAST", DEFAULT_IMAGE_KEEP_LAST, 1, MAX_IMAGE_LIMIT),
            image_guard=parse_image_guard(os.environ.get("CONTEXT_OPTIMIZATION_IMAGE_GUARD", "on")),
        )


def parse_image_limits(raw: str) -> dict[str, int]:
    """`group:50,other:16,*:50` → mapping; malformed entries are dropped."""
    limits: dict[str, int] = {}
    for item in (raw or "").split(","):
        name, sep, value = item.strip().rpartition(":")
        name = name.strip()
        if not sep or not name:
            continue
        try:
            limit = int(value.strip())
        except ValueError:
            continue
        if 1 <= limit <= MAX_IMAGE_LIMIT:
            limits[name] = limit
    return limits


def parse_image_guard(raw: str | None) -> bool:
    value = (raw or "on").strip().lower()
    return value not in {"off", "0", "false", "no"}


def normalize_image_limits(value: Any) -> dict[str, int]:
    """Validate limits coming from the policy store (dict or JSON string)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict):
        return {}
    limits: dict[str, int] = {}
    for name, limit in value.items():
        if not isinstance(name, str) or not name or not isinstance(limit, int):
            continue
        if 1 <= limit <= MAX_IMAGE_LIMIT:
            limits[name] = limit
    return limits


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


# ── 图片历史裁剪（issue #166，吸收 #98）─────────────────────────────────────
# 覆盖 OpenAI `image_url` part 与 Anthropic `image` block（含 tool_result 内嵌），
# 对全部 role 计数；messages 顺序即会话时间顺序。

_IMAGE_PART_TYPES = {"image", "image_url"}


def _collect_image_refs(messages: list) -> list[tuple[int, int, int | None]]:
    """按会话顺序返回每个图片 part 的定位 (message_index, part_index, sub_index)。

    sub_index 非 None 表示 Anthropic tool_result block 内嵌的图片。纯遍历、
    不复制不序列化——未命中阈值的请求只付这一次扫描的成本。
    """
    refs: list[tuple[int, int, int | None]] = []
    for mi, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for pi, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            if part.get("type") in _IMAGE_PART_TYPES:
                refs.append((mi, pi, None))
            elif part.get("type") == "tool_result":
                inner = part.get("content")
                if isinstance(inner, list):
                    for si, sub in enumerate(inner):
                        if isinstance(sub, dict) and sub.get("type") in _IMAGE_PART_TYPES:
                            refs.append((mi, pi, si))
    return refs


def _image_placeholder(seq: int, keep_target: int) -> str:
    """`[IMAGE_` 前缀与 collector/replay 占位符约定对齐，文本规则会跳过它。

    N 用配置目标（keep_last / max_images）而非本轮实际保留数：kept 在保护与
    降级情形下跨轮浮动（9↔8），写进占位符会把全部历史占位符改写、在最早
    裁剪点打断上游 prefix cache（PR #167 评审 P2-2）。
    """
    return f"[IMAGE_EVICTED: 会话第 {seq} 张截图已移除，仅保留最近 {keep_target} 张]"


@dataclass
class ImageEviction:
    tier: str  # "guard"（全量 Key 限额守卫）| "canary"（白名单质量裁剪）
    model: str
    max_images: int
    target: int
    total: int
    pruned: int
    kept: int
    last_user_degraded: bool
    locations: list[tuple[int, int, int, int | None]]  # (会话序号, message, part, sub)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": True, "tier": self.tier, "model": self.model,
            "max_images": self.max_images, "target": self.target,
            "total": self.total, "pruned": self.pruned, "kept": self.kept,
            "last_user_degraded": self.last_user_degraded,
        }


def plan_image_eviction(body: dict[str, Any], config: OptimizerConfig, *, canary: bool) -> ImageEviction | None:
    """决定是否裁剪、裁哪些。纯函数：只读 body，不复制不修改。"""
    if not canary and not config.image_guard:
        return None
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    model = body.get("model")
    if not isinstance(model, str) or not model or not config.image_limits:
        return None
    max_images = config.image_limits.get(model) or config.image_limits.get("*")
    if not isinstance(max_images, int) or max_images < 1:
        return None
    refs = _collect_image_refs(messages)
    total = len(refs)
    if total <= max_images:
        return None
    last_user = -1
    for mi in range(len(messages) - 1, -1, -1):
        if isinstance(messages[mi], dict) and messages[mi].get("role") == "user":
            last_user = mi
            break
    target = min(config.image_keep_last, max_images) if canary else max_images
    evictable = [(seq, ref) for seq, ref in enumerate(refs, start=1) if ref[0] != last_user]  # 最旧在前
    protected = [(seq, ref) for seq, ref in enumerate(refs, start=1) if ref[0] == last_user]
    chosen = evictable[: max(0, total - target)]
    degraded = False
    # 不变量：最后一条 user 消息的图片不动——除非留着它们仍会超上限（必 400）
    still_over = (total - len(chosen)) - max_images
    if still_over > 0:
        degraded = True
        chosen = chosen + protected[:still_over]
    if not chosen:
        return None
    return ImageEviction(
        tier="canary" if canary else "guard", model=model, max_images=max_images,
        target=target, total=total, pruned=len(chosen), kept=total - len(chosen),
        last_user_degraded=degraded,
        locations=[(seq, *ref) for seq, ref in chosen],
    )


def apply_image_evictions(body: dict[str, Any], plan: ImageEviction) -> tuple[int, int]:
    """按计划把图片 part 原位替换为文字占位符；返回 (移除字节, 新增字节)。

    必须作用于 body 的深拷贝——定位下标基于与 plan 相同的结构，text 规则
    不增删 part，下标在拷贝上同样成立。cache_control 随占位符保留。
    """
    messages = body["messages"]
    removed = 0
    added = 0
    for seq, mi, pi, si in plan.locations:
        message = messages[mi]
        container = message["content"] if si is None else message["content"][pi]["content"]
        part = container[pi if si is None else si]
        replacement: dict[str, Any] = {"type": "text", "text": _image_placeholder(seq, plan.target)}
        if isinstance(part.get("cache_control"), (dict, str)):
            replacement["cache_control"] = part["cache_control"]
        removed += _json_bytes(part)
        added += _json_bytes(replacement)
        container[pi if si is None else si] = replacement
    return removed, added


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
                image_keep_last = policy.get("image_keep_last", DEFAULT_IMAGE_KEEP_LAST)
                try:
                    image_keep_last = min(max(int(image_keep_last), 1), MAX_IMAGE_LIMIT)
                except (TypeError, ValueError):
                    image_keep_last = DEFAULT_IMAGE_KEEP_LAST
                guard = policy.get("image_guard", True)
                if not isinstance(guard, bool):
                    guard = parse_image_guard(str(guard))
                return OptimizerConfig(
                    mode=mode,
                    key_hashes=key_hashes,
                    max_tool_result_bytes=int(policy.get("max_tool_result_bytes", DEFAULT_MAX_TOOL_RESULT_BYTES)),
                    repeat_min_lines=int(policy.get("repeat_min_lines", DEFAULT_REPEAT_MIN_LINES)),
                    head_bytes=int(policy.get("head_bytes", DEFAULT_HEAD_BYTES)),
                    tail_bytes=int(policy.get("tail_bytes", DEFAULT_TAIL_BYTES)),
                    image_limits=normalize_image_limits(policy.get("image_limits")),
                    image_keep_last=image_keep_last,
                    image_guard=guard,
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
        canary = mode != "off"
        try:
            image_plan = plan_image_eviction(body, active_config, canary=canary)
        except Exception:
            image_plan = None  # 图片规则绝不弄坏请求：计划失败按未命中处理
        if not canary:
            if image_plan is None:
                return body, {"mode": mode, "enabled": False, "changed": False, "key_hash": digest[:12],
                              "raw_bytes": None, "optimized_bytes": None, "bytes_saved": 0,
                              "tool_result_bytes_saved": 0, "fallback_never_worse": False,
                              "rule_hits": RuleHits().as_dict(), "image_evict": {"active": False}}
            # guard 档（全量 Key）：只救必 400 的请求，不做全量重序列化——
            # 图片→短占位符的差值用逐 part 算术精确计量。
            candidate = copy.deepcopy(body)
            try:
                removed, added = apply_image_evictions(candidate, image_plan)
            except Exception:
                return body, {"mode": mode, "enabled": False, "changed": False, "key_hash": digest[:12],
                              "raw_bytes": None, "optimized_bytes": None, "bytes_saved": 0,
                              "tool_result_bytes_saved": 0, "fallback_never_worse": False,
                              "rule_hits": RuleHits().as_dict(), "image_evict": {"active": False}}
            info = image_plan.as_dict()
            info.update(bytes_removed=removed, bytes_added=added)
            return candidate, {
                "mode": mode, "enabled": True, "changed": True, "key_hash": digest[:12],
                "raw_bytes": None, "optimized_bytes": None, "bytes_saved": max(removed - added, 0),
                "tool_result_bytes_saved": 0, "fallback_never_worse": False,
                "rule_hits": RuleHits().as_dict(), "image_evict": info,
            }
        raw_bytes = _json_bytes(body)
        raw_tool_bytes = self._tool_result_bytes(body)
        config = OptimizerConfig(mode=mode, key_hashes=active_config.key_hashes,
                                 max_tool_result_bytes=active_config.max_tool_result_bytes,
                                 repeat_min_lines=active_config.repeat_min_lines,
                                 head_bytes=active_config.head_bytes, tail_bytes=active_config.tail_bytes,
                                 image_limits=active_config.image_limits,
                                 image_keep_last=active_config.image_keep_last,
                                 image_guard=active_config.image_guard)
        stats = TransformStats()
        candidate = _transform(copy.deepcopy(body), VisitContext(), config, stats, root=True)
        optimized_bytes = _json_bytes(candidate)
        if optimized_bytes >= raw_bytes:
            candidate = body
            optimized_bytes = raw_bytes
            fallback = True
        else:
            fallback = False
        # 工具结果字节按文本候选计量：图片裁剪若作用于 tool 消息内，不应把
        # base64 字节混进文本压缩的节省口径。
        optimized_tool_bytes = self._tool_result_bytes(candidate)
        # 图片限额救援独立于文本 never-worse 回退（PR #167 评审 P2-1）：占位符可能
        # 比短 URL 图 part 大，候选总字节因此变大时只应回退文本变换——若在此恢复
        # 全部图片，51 张请求会重新回到上游 400。救援在回退判定之后作用于最终体，
        # 指标与 x-portal-images-pruned 因此始终与实发请求一致。
        image_info: dict[str, Any] = {"active": False}
        if image_plan is not None:
            try:
                evicted = copy.deepcopy(candidate)
                removed, added = apply_image_evictions(evicted, image_plan)
                candidate = evicted
                image_info = image_plan.as_dict()
                image_info.update(bytes_removed=removed, bytes_added=added)
            except Exception:
                image_info = {"active": False}
        return candidate, {
            "mode": mode, "enabled": True, "changed": candidate is not body,
            "key_hash": digest[:12], "raw_bytes": raw_bytes, "optimized_bytes": optimized_bytes,
            "bytes_saved": raw_bytes - optimized_bytes,
            "tool_result_bytes_saved": raw_tool_bytes - optimized_tool_bytes,
            "fallback_never_worse": fallback, "rule_hits": stats.hits.as_dict(),
            "image_evict": image_info,
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
