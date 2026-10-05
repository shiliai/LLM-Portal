"""Repair UTF-8-read-as-Latin-1 mojibake in gateway traffic (issue #162).

The private engines occasionally emit a multi-byte character spelled out as its
UTF-8 bytes decoded one byte per character ('—' arrives as 'â\x80\x94', '好' as
'å¥½').  Once such a reply lands in a client session the model imitates the
corruption in every later turn of that conversation, so a single hiccup poisons
the whole session and every gateway hop relays it faithfully.

The repair is deliberately conservative.  A rewrite applies only to a maximal
run of Latin-1 range characters (U+0080..U+00FF) whose Latin-1 bytes also form
valid UTF-8: legitimate text (ASCII, CJK, emoji, '·', '×', '°C', 'café', 'æøå')
never forms such a run, and a run that does not decode strictly is left exactly
as written.  Single Latin-1 characters never change (a lone lead or continuation
byte is not valid UTF-8), so the typical typographic characters stay put.
"""
from __future__ import annotations

import os
import re
from typing import Any

_RUN = re.compile(r"[\u0080-\u00ff]+")


def enabled() -> bool:
    """``MOJIBAKE_REPAIR``: on by default; 'off' disables every rewrite."""
    return os.environ.get("MOJIBAKE_REPAIR", "on").strip().lower() != "off"


def repair_text(value: str) -> tuple[str, int]:
    """Return (text, runs_rewritten) with byte-spelled UTF-8 runs restored."""

    if not value or not _RUN.search(value):
        return value, 0
    out: list[str] = []
    fixes = 0
    pos = 0
    for match in _RUN.finditer(value):
        run = match.group(0)
        out.append(value[pos:match.start()])
        pos = match.end()
        try:
            repaired = run.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            out.append(run)
            continue
        if repaired != run:
            fixes += 1
        out.append(repaired)
    out.append(value[pos:])
    return "".join(out), fixes


def _repair_block_content(content: Any) -> int:
    """Content blocks (OpenAI chat): repair the text parts of a list-shaped content."""

    if not isinstance(content, list):
        return 0
    fixes = 0
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            block["text"], n = repair_text(block["text"])
            fixes += n
    return fixes


def repair_reply(obj: Any) -> int:
    """Repair a parsed OpenAI completion payload (streaming chunk or final body).

    Covers ``choices[].delta`` and ``choices[].message`` (``content`` and
    ``reasoning_content``, string or block-list content).  Returns the number of
    rewritten mojibake runs; the payload is mutated in place.
    """

    if not isinstance(obj, dict):
        return 0
    fixes = 0
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for section in ("delta", "message"):
            payload = choice.get(section)
            if not isinstance(payload, dict):
                continue
            for field in ("content", "reasoning_content"):
                value = payload.get(field)
                if isinstance(value, str):
                    payload[field], n = repair_text(value)
                    fixes += n
                elif value is not None:
                    fixes += _repair_block_content(value)
    return fixes


def repair_history(body: Any) -> int:
    """Repair mojibake a session's history carries back to the engines.

    Only accumulated model output is rewritten — ``assistant`` and ``tool``
    messages (``content`` and ``reasoning_content``); what the user and the
    system prompt say is left untouched, so a client that is deliberately
    discussing mojibake keeps its own words.
    """

    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return 0
    fixes = 0
    for message in body["messages"]:
        if not isinstance(message, dict) or message.get("role") not in ("assistant", "tool"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"], n = repair_text(content)
            fixes += n
        else:
            fixes += _repair_block_content(content)
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            message["reasoning_content"], n = repair_text(reasoning)
            fixes += n
    return fixes


class StreamRepairer:
    """Cross-event repair for mojibake split across streaming deltas.

    The engines sometimes spell a corrupted character one token per event
    ('æ', '\x96', '\x87' as three separate deltas), so a single event holds an
    unrepairable fragment while the full run only exists across events.  The
    repairer keeps the trailing Latin-1 run of each field held back until the
    next event either completes it (repair, emit) or rules it out (emit as
    written).  At most ``MAX_HOLD`` characters are withheld per field, so the
    added latency is one event and the withheld tail is bounded.
    """

    MAX_HOLD = 8

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}

    def feed(self, field: str, piece: str) -> str:
        """Repair one delta's text for ``field``; returns what to emit now."""

        if not piece:
            return piece
        buf = self._pending.pop(field, "") + piece
        out: list[str] = []
        pos = 0
        for match in _RUN.finditer(buf):
            run = match.group(0)
            start, end = match.start(), match.end()
            out.append(buf[pos:start])
            try:
                out.append(run.encode("latin-1").decode("utf-8"))
            except (UnicodeEncodeError, UnicodeDecodeError):
                if end == len(buf):
                    self._pending[field] = run[-self.MAX_HOLD:]
                    return "".join(out)
                out.append(run)
            pos = end
        out.append(buf[pos:])
        return "".join(out)

    def pending(self) -> dict[str, str]:
        return dict(self._pending)

    def resolve(self) -> dict[str, str]:
        """Emit and clear everything still held (end of stream: no continuation can come)."""

        held, self._pending = self._pending, {}
        return held
