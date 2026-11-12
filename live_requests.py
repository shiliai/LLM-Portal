"""Metadata-only, best-effort live observation shared by compat and console.

One leased snapshot per compat worker, rather than a SQL write per token.
No request/response content, credentials or caller-controlled correlation IDs.
An HTTP response is not evidence of the inference scheduler's running state.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import sqlite3
import time
import uuid
from contextlib import suppress
from pathlib import Path

from starlette.requests import ClientDisconnect

HEARTBEAT_SECONDS = 5
STALE_SECONDS = 30
MAX_REQUESTS = 2000
MAX_PENDING_HISTORY = 2000
RETENTION_SECONDS = 3600
HISTORY_DAYS = 30
HISTORY_LIMIT = 50000
HISTORY_SECONDS = HISTORY_DAYS * 86400
MAX_RESPONSE_METADATA_BYTES = 256 * 1024


def _text(value: object, limit: int) -> str:
    return "".join(c for c in str(value) if c >= " " and c != "\x7f")[:limit].encode("utf-8", errors="replace").decode("utf-8")


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _history_schema(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS request_history_health (worker TEXT PRIMARY KEY, dropped INTEGER NOT NULL, updated_at REAL NOT NULL, started_at REAL NOT NULL DEFAULT 0)")
    if "started_at" not in {row[1] for row in db.execute("PRAGMA table_info(request_history_health)")}:
        db.execute("ALTER TABLE request_history_health ADD COLUMN started_at REAL NOT NULL DEFAULT 0")
    db.execute("CREATE TABLE IF NOT EXISTS request_history (request_id TEXT PRIMARY KEY, ended_at REAL NOT NULL, "
               "model TEXT NOT NULL, key_hash TEXT NOT NULL, source_ip TEXT NOT NULL, user_agent TEXT NOT NULL, "
               "outcome TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS request_history_time ON request_history(ended_at DESC,request_id DESC)")


def _store_history(db: sqlite3.Connection, row: dict) -> None:
    db.execute("INSERT INTO request_history VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
               "ended_at=excluded.ended_at,model=excluded.model,key_hash=excluded.key_hash,source_ip=excluded.source_ip, "
               "user_agent=excluded.user_agent,outcome=excluded.outcome,payload=excluded.payload "
               "WHERE request_history.outcome='unknown'",
               (row["request_id"], row["ended_at"], row.get("model", ""), row.get("key_hash", ""),
                row.get("source_ip", ""), row.get("user_agent", ""), row["outcome"], json.dumps(row, ensure_ascii=True)))


def _archive_stale(db: sqlite3.Connection, now: float) -> None:
    for heartbeat, payload in db.execute("SELECT heartbeat,payload FROM live_workers WHERE heartbeat < ?",
                                        (now - STALE_SECONDS,)).fetchall():
        for original in json.loads(payload):
            row = dict(original)
            row.update(outcome="unknown", phase="finished", ended_at=heartbeat,
                       elapsed_seconds=round(max(0, heartbeat - row["started_at"]), 1))
            _store_history(db, row)


def _prune_history(db: sqlite3.Connection, now: float) -> None:
    db.execute("DELETE FROM request_history_health WHERE updated_at < ?", (now - HISTORY_SECONDS,))
    db.execute("DELETE FROM request_history WHERE ended_at < ?", (now - HISTORY_SECONDS,))
    db.execute("DELETE FROM request_history WHERE request_id IN (SELECT request_id FROM request_history "
               "ORDER BY ended_at DESC,request_id DESC LIMIT -1 OFFSET ?)", (HISTORY_LIMIT,))


def read_history(db_path: str | Path, *, limit: int = 50, cursor: str = "", search: str = "",
                 outcome: str = "", models: list[str] | None = None, key_hashes: list[str] = (),
                 request_id: str = "") -> dict:
    result = {"available": False, "records": [], "next_cursor": "", "has_more": False,
              "retention_days": HISTORY_DAYS, "capacity": HISTORY_LIMIT, "dropped_records": 0, "dropped_scope": "reporting_workers_lifetime", "dropped_since": None}
    clauses, args = ["ended_at >= ?"], [time.time() - HISTORY_SECONDS]
    if cursor:
        try:
            stamp, rid = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if type(stamp) not in (float, int) or not 0 < stamp < 10**12 or not isinstance(rid, str):
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ValueError("invalid history cursor") from None
        clauses.append("(ended_at < ? OR (ended_at = ? AND request_id < ?))")
        args.extend([stamp, stamp, rid])
    if request_id:
        clauses.append("request_id=?"); args.append(request_id)
    if outcome:
        clauses.append("outcome=?"); args.append(outcome)
    if models is not None:
        clauses.append("model IN (" + ",".join("?" for _ in models) + ")" if models else "0")
        args.extend(models)
    if search:
        cols = ("request_id", "model", "source_ip", "user_agent", "key_hash")
        terms = ["instr(lower(" + col + "),?)>0" for col in cols]
        args.extend([search.lower()] * len(cols))
        if key_hashes:
            terms.append("key_hash IN (" + ",".join("?" for _ in key_hashes) + ")")
            args.extend(key_hashes)
        clauses.append("(" + " OR ".join(terms) + ")")
    try:
        with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
            result["dropped_records"], result["dropped_since"] = db.execute(
                "SELECT coalesce(sum(dropped),0),min(CASE WHEN dropped>0 THEN started_at END) "
                "FROM request_history_health WHERE updated_at>=?", (time.time()-HISTORY_SECONDS,)).fetchone()
            records = db.execute("SELECT payload FROM request_history WHERE " + " AND ".join(clauses) +
                                 " ORDER BY ended_at DESC,request_id DESC LIMIT ?", (*args, limit + 1)).fetchall()
        result["available"] = True
        result["has_more"] = len(records) > limit
        result["records"] = [json.loads(row[0]) for row in records[:limit]]
        if result["has_more"]:
            row = result["records"][-1]
            result["next_cursor"] = base64.urlsafe_b64encode(json.dumps([row["ended_at"], row["request_id"]]).encode()).decode().rstrip("=")
    except (sqlite3.Error, OSError):
        pass
    return result


class LiveRequests:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.worker = uuid.uuid4().hex
        self.started_at = time.time()
        self.rows: dict[str, dict] = {}
        self.overflow = 0
        self.finished: dict[str, dict] = {}
        self._publishing_ids: set[str] = set()
        self.history_dropped = 0
        self.available = False
        self._task = None
        self._publisher = None
        self._dirty = False
        self._closing = False

    def _publish(self, rows: list[dict], overflow: int, finished: list[dict]) -> None:
        # ASCII escaping also accepts legal JSON containing lone surrogates.
        payload = json.dumps(rows, ensure_ascii=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=0.1) as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("CREATE TABLE IF NOT EXISTS live_workers ("
                       "worker TEXT PRIMARY KEY, heartbeat REAL NOT NULL, payload TEXT NOT NULL, overflow INTEGER NOT NULL)")
            _history_schema(db)
            now = time.time()
            _archive_stale(db, now)
            for row in rows:
                db.execute("DELETE FROM request_history WHERE request_id=? AND outcome='unknown'", (row["request_id"],))
            for row in finished:
                _store_history(db, row)
            db.execute("INSERT OR REPLACE INTO request_history_health (worker,dropped,updated_at,started_at) VALUES (?,?,?,?)",
                       (self.worker, self.history_dropped, now, self.started_at))
            _prune_history(db, now)
            db.execute("INSERT OR REPLACE INTO live_workers VALUES (?,?,?,?)",
                       (self.worker, now, payload, overflow))
            db.execute("DELETE FROM live_workers WHERE heartbeat < ?", (now - RETENTION_SECONDS,))

    def changed(self) -> None:
        self._dirty = True
        if not self._closing and (self._publisher is None or self._publisher.done()):
            self._publisher = asyncio.create_task(self._publish_changes())

    async def _publish_changes(self) -> None:
        # Coalesce bursts, run disk/serialization in the background, and keep
        # one publisher so an older snapshot cannot resurrect finished rows.
        await asyncio.sleep(0)
        while self._dirty:
            self._dirty = False
            rows = [dict(row) for row in self.rows.values()]
            finished = list(self.finished.values())
            self._publishing_ids = {row["request_id"] for row in finished}
            try:
                await asyncio.to_thread(self._publish, rows, self.overflow, finished)
                for row in finished:
                    self.finished.pop(row["request_id"], None)
                self.available = True
            except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError):
                self.available = False
            finally:
                self._publishing_ids.clear()

    async def flush(self) -> None:
        # Used by startup/shutdown and tests only, never by inference paths.
        if self._publisher:
            await asyncio.shield(self._publisher)

    async def start(self) -> None:
        self._closing = False
        self.changed()
        await self.flush()
        self._task = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            self.changed()

    async def stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self.flush()
        for rid in list(self.rows):
            await self.finish(rid, outcome="unknown")
        self._dirty = True
        self._publisher = asyncio.create_task(self._publish_changes())
        await self.flush()
        self.rows.clear()
        self.overflow = 0
        self.available = False
        def remove_lease():
            with sqlite3.connect(self.db_path, timeout=0.1) as db:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("DELETE FROM live_workers WHERE worker=?", (self.worker,))
        with suppress(OSError, sqlite3.Error):
            await asyncio.to_thread(remove_lease)

    async def begin(self, scope: dict) -> str:
        rid = "live_" + uuid.uuid4().hex
        if len(self.rows) >= MAX_REQUESTS:
            self.overflow += 1
            self.changed()
            return rid
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin1")
        credential = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get(b"x-api-key", b"").decode("latin1").strip()
        key_hash = hashlib.sha256(credential.encode()).hexdigest() if credential else ""
        peer = (scope.get("client") or ("", 0))[0]
        # Edge nginx overwrites X-Real-IP; only retain literal IP metadata.
        candidate = headers.get(b"x-real-ip", b"").decode("latin1") or peer
        try:
            source = str(ipaddress.ip_address(candidate))
        except ValueError:
            source = ""
        now = time.time()
        self.rows[rid] = {"request_id": rid, "key_hash": key_hash,
                          "model": "", "source_ip": source, "endpoint": scope["path"],
                          "protocol": "anthropic" if scope["path"].startswith("/v1/messages") else "openai",
                          "phase": "receiving_request", "started_at": now,
                          "phase_since": now, "streaming": False,
                          "user_agent": _text(headers.get(b"user-agent", b"").decode("latin1"), 512),
                          "request_bytes": None, "forwarded_request_bytes": None,
                          "message_count": None, "tool_count": None, "max_output_tokens": None,
                          "input_tokens": None, "output_tokens": None,
                          "cache_read_input_tokens": None, "cache_creation_input_tokens": None,
                          "total_input_tokens": None, "usage_source": None,
                          "status_code": None, "response_bytes": 0, "first_response_at": None}
        self.changed()
        return rid

    async def describe(self, rid: str | None, *, model: object, streaming: bool,
                       body: dict | None = None, request_bytes: int | None = None) -> None:
        if rid in self.rows:
            self.rows[rid].update(model=model[:200].encode("utf-8", errors="replace").decode("utf-8") if isinstance(model, str) else "",
                                  streaming=streaming, phase="awaiting_response", phase_since=time.time())
            row = self.rows[rid]
            row["request_bytes"] = request_bytes
            if isinstance(body, dict):
                row["message_count"] = len(body["messages"]) if isinstance(body.get("messages"), list) else None
                row["tool_count"] = len(body.get("tools", [])) if isinstance(body.get("tools", []), list) else None
                row["max_output_tokens"] = _count(body.get("max_completion_tokens", body.get("max_tokens")))
                for key in ("temperature", "top_p"):
                    value = body.get(key)
                    if type(value) in (int, float) and -1e6 < value < 1e6:
                        row[key] = value
            self.changed()

    async def responding(self, rid: str) -> None:
        if rid in self.rows and self.rows[rid]["phase"] != "receiving_response":
            now = time.time()
            self.rows[rid].update(phase="receiving_response", phase_since=now, first_response_at=now)
            self.changed()

    def usage(self, rid: str, value: object) -> None:
        row = self.rows.get(rid)
        if not row or not isinstance(value, dict):
            return
        usage = value.get("usage")
        if not isinstance(usage, dict) and isinstance(value.get("message"), dict):
            usage = value["message"].get("usage")
        if not isinstance(usage, dict):
            return
        anthropic = row["protocol"] == "anthropic"
        for field, source in (("input_tokens", "input_tokens" if anthropic else "prompt_tokens"),
                              ("output_tokens", "output_tokens" if anthropic else "completion_tokens"),
                              ("cache_read_input_tokens", "cache_read_input_tokens"),
                              ("cache_creation_input_tokens", "cache_creation_input_tokens")):
            count = _count(usage.get(source))
            if count is not None:
                row[field] = count
        if not anthropic and isinstance(usage.get("prompt_tokens_details"), dict):
            row["cache_read_input_tokens"] = _count(usage["prompt_tokens_details"].get("cached_tokens"))
        if row["input_tokens"] is not None:
            row["total_input_tokens"] = row["input_tokens"] + ((row["cache_read_input_tokens"] or 0) +
                (row["cache_creation_input_tokens"] or 0) if anthropic else 0)
        if any(row[key] is not None for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")):
            row["usage_source"] = "backend_usage"
            self.changed()

    async def finish(self, rid: str, *, outcome: str = "completed") -> None:
        row = self.rows.pop(rid, None)
        if row is None:
            self.overflow = max(0, self.overflow - 1)
        else:
            now = time.time()
            row.update(outcome=outcome, ended_at=now, phase="finished",
                       elapsed_seconds=round(max(0, now - row["started_at"]), 1))
            self.finished[rid] = row
            if len(self.finished) > MAX_PENDING_HISTORY:
                dropped = next(key for key in self.finished if key not in self._publishing_ids)
                self.finished.pop(dropped)
                self.history_dropped += 1
        self.changed()


def prune_expired(db_path: str | Path) -> None:
    # Console runs this even if every compat worker has stopped. mode=rw
    # avoids creating a root-owned file before the unprivileged writer starts.
    with suppress(sqlite3.Error, OSError):
        with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=0.1) as db:
            db.execute("PRAGMA secure_delete=ON")
            _history_schema(db)
            now = time.time()
            _archive_stale(db, now)
            _prune_history(db, now)
            db.execute("DELETE FROM live_workers WHERE heartbeat < ?", (now - RETENTION_SECONDS,))


def read_snapshot(db_path: str | Path, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    result = {"available": False, "updated_at": None, "stale_workers": 0,
              "overflow": 0, "requests": [], "stale_requests": []}
    try:
        with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
            workers = db.execute("SELECT heartbeat,payload,overflow FROM live_workers").fetchall()
    except (sqlite3.Error, OSError):
        return result
    for heartbeat, payload, overflow in workers:
        if now - heartbeat > RETENTION_SECONDS:
            continue
        fresh = 0 <= now - heartbeat <= STALE_SECONDS
        result["available"] |= fresh
        result["updated_at"] = max(result["updated_at"] or 0, heartbeat)
        result["stale_workers"] += int(not fresh)
        if fresh:
            result["overflow"] += overflow
        for row in json.loads(payload):
            row.update(elapsed_seconds=round(max(0, now - row["started_at"]), 1),
                       phase_seconds=round(max(0, now - row["phase_since"]), 1))
            result["requests" if fresh else "stale_requests"].append(row)
    result["requests"].sort(key=lambda x: x["started_at"])
    result["stale_requests"].sort(key=lambda x: x["started_at"])
    return result


class LiveRequestsMiddleware:
    """Pure ASGI observation: cleanup spans the entire response, including SSE."""
    def __init__(self, app, tracker: LiveRequests):
        self.app, self.tracker = app, tracker

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") not in {
            "/v1/messages", "/v1/chat/completions"
        }:
            return await self.app(scope, receive, send)
        rid = await self.tracker.begin(scope)
        scope["live_request_id"] = rid

        observer = ResponseMetadata(self.tracker, rid)
        outcome = "completed"
        client_disconnected = False
        final_send_returned = False

        async def observed_receive():
            nonlocal client_disconnected
            message = await receive()
            # ASGI servers may report http.disconnect after a successful
            # final response body. That is normal response teardown.
            client_disconnected |= message["type"] == "http.disconnect" and not final_send_returned
            return message

        async def observed_send(message):
            nonlocal final_send_returned
            observer.observe(message)
            if message["type"] == "http.response.body" and message.get("body"):
                await self.tracker.responding(rid)
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                final_send_returned = True

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except (OSError, ClientDisconnect):
            outcome = "disconnected"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            observer.complete()
            if client_disconnected:
                outcome = "disconnected"
            if outcome == "completed":
                if observer.failed or observer.status is None or not 200 <= observer.status < 300:
                    outcome = "failed"
                elif not observer.body_complete or (observer.sse and not observer.stream_complete):
                    outcome = "interrupted"
            await self.tracker.finish(rid, outcome=outcome)


class ResponseMetadata:
    """Read only allowlisted usage numbers. Never retain a response body in history.

    Bounded, incremental parsing accepts SSE split across arbitrary ASGI chunks.
    It never modifies the forwarded bytes or requests extra backend tokenization.
    """
    def __init__(self, tracker: LiveRequests, rid: str):
        self.tracker, self.rid = tracker, rid
        self.status = None
        self.sse = False
        self.body_complete = self.stream_complete = self.failed = False
        self.buffer = bytearray()
        self.discard_line = False
        self.json_overflow = False
        self.data_lines = []
        self.event_bytes = 0

    def value(self, data: bytes) -> None:
        if data.strip() == b"[DONE]":
            self.stream_complete = True
            return
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            return
        if not isinstance(value, dict):
            return
        self.tracker.usage(self.rid, value)
        self.failed |= bool(value.get("error")) or value.get("type") == "error"
        self.stream_complete |= value.get("type") == "message_stop"

    def line(self, line: bytes) -> None:
        line = line.rstrip(b"\r")
        if not line:
            if self.data_lines:
                self.value(b"\n".join(self.data_lines))
            self.data_lines = []
            self.event_bytes = 0
        elif line.startswith(b"data:"):
            self.event_bytes += len(line)
            if self.event_bytes <= MAX_RESPONSE_METADATA_BYTES:
                self.data_lines.append(line[5:].lstrip(b" "))
            else:
                self.data_lines = []

    def observe(self, message: dict) -> None:
        row = self.tracker.rows.get(self.rid)
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.sse = b"text/event-stream" in dict(message.get("headers", [])).get(b"content-type", b"")
            if row is not None:
                row["status_code"] = self.status
            return
        if message["type"] != "http.response.body":
            return
        body = message.get("body", b"")
        if row is not None:
            row["response_bytes"] += len(body)
        self.body_complete |= not message.get("more_body", False)
        if not self.sse:
            if len(self.buffer) + len(body) <= MAX_RESPONSE_METADATA_BYTES and not self.json_overflow:
                self.buffer.extend(body)
            else:
                self.buffer.clear()
                self.json_overflow = True
            return
        # Split first, then bound each line, so a large chunk of small events
        # cannot hide a terminal usage event near its end.
        parts = body.split(b"\n")
        for index, part in enumerate(parts):
            if not self.discard_line:
                if len(self.buffer) + len(part) <= MAX_RESPONSE_METADATA_BYTES:
                    self.buffer.extend(part)
                else:
                    self.buffer.clear()
                    self.discard_line = True
                    self.data_lines = []
                    self.event_bytes = MAX_RESPONSE_METADATA_BYTES + 1
            if index < len(parts) - 1:
                if not self.discard_line:
                    self.line(bytes(self.buffer))
                self.buffer.clear()
                self.discard_line = False

    def complete(self) -> None:
        if self.sse:
            if self.buffer and not self.discard_line:
                self.line(bytes(self.buffer))
            self.line(b"")
        elif not self.json_overflow:
            self.value(bytes(self.buffer))
        self.buffer.clear()
        self.data_lines = []
