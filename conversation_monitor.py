"""Portal conversation capture runtime shared by compat and console.

The gateway owns original request/response capture.  Collectors consume the
bounded SSE stream and perform redaction/OPF outside Portal.  This module is
deliberately dependency-light so the compat and console images can share the
same SQLite volume.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

MODES = {"off", "stream", "persist"}
DEFAULT_TTL_DAYS = 14
DEFAULT_CAPACITY = 50_000
REPLAY_LIMIT = 2048
CLIENT_QUEUE_LIMIT = 256
DEFAULT_MAX_CAPTURE_BYTES = 4 * 1024 * 1024


def _db_path() -> Path:
    configured = os.environ.get("CONVERSATION_MONITOR_DB", "")
    if configured:
        return Path(configured)
    configured_dir = os.environ.get("CONVERSATION_MONITOR_DATA", "")
    if configured_dir:
        return Path(configured_dir) / "monitor.db"
    # Production compose sets an explicit shared volume.  Keep local/test
    # imports isolated from privileged system paths when it does not.
    state_dir = os.environ.get("CONSOLE_DATA", "")
    return (Path(state_dir) / "conversation-monitor.db") if state_dir else Path("/tmp/private-llm-conversation-monitor.db")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def key_identity(credential: str) -> tuple[str, str]:
    """Return a stable policy key hash and a masked display reference."""
    credential = credential or ""
    digest = hashlib.sha256(credential.encode()).hexdigest()
    return digest, f"…{credential[-4:]}" if credential else "…"


def extract_credential(headers: Any) -> str:
    auth = headers.get("authorization", "") if headers is not None else ""
    if isinstance(auth, str) and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    value = headers.get("x-api-key", "") if headers is not None else ""
    return value.strip() if isinstance(value, str) else ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _parse_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw.decode("utf-8", errors="replace")


def _upstream_id(response: Any) -> str:
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return response["id"][:256]
    if isinstance(response, str):
        for line in response.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                value = json.loads(line[5:].strip())
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                return value["id"][:256]
    return ""


def _safe_path(path: str) -> str:
    return path if isinstance(path, str) and len(path) <= 128 else ""


def max_capture_bytes() -> int:
    try:
        value = int(os.environ.get("CONVERSATION_MONITOR_MAX_CAPTURE_BYTES", DEFAULT_MAX_CAPTURE_BYTES))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_CAPTURE_BYTES
    return min(max(value, 64 * 1024), 64 * 1024 * 1024)


class ConversationMonitor:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else _db_path()
        self._policy = {"mode": "off", "keys": [], "ttl_days": DEFAULT_TTL_DAYS,
                        "capacity": DEFAULT_CAPACITY, "version": 1, "updated_at": _utc_now()}
        self._policy_loaded = False
        # This object is imported before an event loop exists by both service
        # processes and by Python 3.9 test loaders.  Keep lifecycle state
        # independent of any particular asyncio loop.
        self._policy_lock = threading.RLock()
        self._subscribers: set[asyncio.Queue] = set()
        self._replay: deque[dict] = deque(maxlen=REPLAY_LIMIT)
        self._dropped: dict[str, int] = {}
        self._deleted: dict[str, int] = {}
        self._captured = 0
        self._started = time.monotonic()
        self._event_counter = int(time.time() * 1000)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversation_policy (
                    id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT NOT NULL,
                    keys_json TEXT NOT NULL, ttl_days INTEGER NOT NULL,
                    capacity INTEGER NOT NULL, version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    key_hash TEXT NOT NULL,
                    key_ref TEXT NOT NULL,
                    model TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    capture_mode TEXT NOT NULL,
                    content_mode TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversation_events_created ON conversation_events(created_ts DESC);
                CREATE INDEX IF NOT EXISTS conversation_events_filters ON conversation_events(key_hash, model, protocol, status);
            """)
            row = conn.execute("SELECT mode, keys_json, ttl_days, capacity, version, updated_at "
                               "FROM conversation_policy WHERE id=1").fetchone()
            if row is None:
                conn.execute("INSERT INTO conversation_policy VALUES (1,?,?,?,?,?,?)",
                             ("off", "[]", DEFAULT_TTL_DAYS, DEFAULT_CAPACITY, 1, self._policy["updated_at"]))
            else:
                try:
                    keys = json.loads(row[1])
                except (ValueError, TypeError):
                    keys = []
                self._policy = {"mode": row[0] if row[0] in MODES else "off",
                                "keys": keys if isinstance(keys, list) else [],
                                "ttl_days": int(row[2]), "capacity": int(row[3]),
                                "version": int(row[4]), "updated_at": row[5]}
            self._policy_loaded = True

    def policy(self) -> dict:
        self._refresh_policy()
        return dict(self._policy, keys=list(self._policy["keys"]))

    def _refresh_policy(self) -> None:
        """Refresh the small policy row so console and compat share updates."""
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT mode, keys_json, ttl_days, capacity, version, updated_at "
                                   "FROM conversation_policy WHERE id=1").fetchone()
            if row is None or int(row[4]) <= int(self._policy.get("version", 0)):
                return
            try:
                keys = json.loads(row[1])
            except (ValueError, TypeError):
                keys = []
            self._policy = {"mode": row[0] if row[0] in MODES else "off",
                            "keys": keys if isinstance(keys, list) else [],
                            "ttl_days": int(row[2]), "capacity": int(row[3]),
                            "version": int(row[4]), "updated_at": row[5]}
        except Exception:
            self._drop("policy_error")

    def _allowed(self, key_hash: str) -> bool:
        keys = self._policy.get("keys", [])
        return key_hash in keys or "*" in keys

    def decision(self, credential: str) -> dict:
        self._refresh_policy()
        key_hash, key_ref = key_identity(credential)
        mode = self._policy["mode"] if self._allowed(key_hash) else "off"
        return {"mode": mode, "key_hash": key_hash, "key_ref": key_ref}

    async def update_policy(self, *, mode: str, keys: list[str], ttl_days: int,
                            capacity: int) -> dict:
        mode = str(mode).lower()
        if mode not in MODES:
            raise ValueError("mode must be off, stream, or persist")
        if not isinstance(keys, list) or any(not isinstance(x, str) or len(x) > 128 for x in keys):
            raise ValueError("keys must be a list of key hashes")
        ttl_days = int(ttl_days)
        capacity = int(capacity)
        if ttl_days not in (7, 14, 30):
            raise ValueError("ttl_days must be 7, 14, or 30")
        if capacity not in (10_000, 50_000, 100_000):
            raise ValueError("capacity must be 10000, 50000, or 100000")
        with self._policy_lock:
            version = int(self._policy["version"]) + 1
            updated = _utc_now()
            next_policy = {"mode": mode, "keys": sorted(set(keys)), "ttl_days": ttl_days,
                           "capacity": capacity, "version": version, "updated_at": updated}
            try:
                with self._connect() as conn:
                    conn.execute("UPDATE conversation_policy SET mode=?, keys_json=?, ttl_days=?, capacity=?, version=?, updated_at=? WHERE id=1",
                                 (mode, _json(next_policy["keys"]), ttl_days, capacity, version, updated))
            except Exception:
                self._drop("policy_error")
                raise
            self._policy = next_policy
            event = self._event("policy.changed", {
                "version": version, "mode": mode, "keys": next_policy["keys"],
                "ttl_days": ttl_days, "capacity": capacity,
            }, request_id="policy")
            self._publish(event)
            return self.policy()

    def _drop(self, reason: str) -> None:
        self._dropped[reason] = self._dropped.get(reason, 0) + 1

    def _event(self, event_type: str, data: dict, *, request_id: str) -> dict:
        self._event_counter += 1
        seq = self._event_counter
        event_id = f"evt_{seq}"
        return {"id": event_id, "event": event_type, "request_id": request_id,
                "created_at": _utc_now(), "data": data}

    def _publish(self, event: dict) -> None:
        self._replay.append(event)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._drop("sse_queue_full")

    async def capture(self, *, request_id: str, request_raw: bytes, response_raw: bytes,
                      headers: Any, path: str, status_code: int, model: str,
                      protocol: str, started: float, credential: str,
                      response_content_type: str = "", response_truncated: bool = False) -> None:
        """Capture after response completion; all errors are intentionally isolated."""
        try:
            decision = self.decision(credential)
            mode = decision["mode"]
            if mode == "off":
                return
            req = _parse_body(request_raw)
            response = _parse_body(response_raw)
            upstream_id = _upstream_id(response)
            usage: dict = {}
            tool_calls: list = []
            if isinstance(response, dict):
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                choices = response.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        msg = choice.get("message") if isinstance(choice, dict) else None
                        calls = msg.get("tool_calls") if isinstance(msg, dict) else None
                        if isinstance(calls, list):
                            tool_calls.extend(calls)
            key_hash, key_ref = key_identity(credential)
            created = _utc_now()
            event_data = {
                "request_id": request_id,
                "created_at": created,
                "key_ref": key_ref,
                "model": _safe_path(model),
                "protocol": protocol,
                "status": "ok" if 200 <= int(status_code) < 400 else "error",
                "capture_mode": mode,
                "content_mode": "original",
                "payload": {
                    "request": req, "response": response, "tool_calls": tool_calls,
                    "usage": usage, "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                    "endpoint": _safe_path(path),
                    "response_content_type": response_content_type[:120],
                    "capture_truncated": bool(response_truncated),
                    "upstream_request_id": upstream_id,
                    "retention_ttl_days": int(self._policy["ttl_days"]),
                },
            }
            event = self._event("conversation.capture", event_data, request_id=request_id)
            event_data["id"] = event["id"]
            event_data["event"] = event["event"]
            self._captured += 1
            self._publish(event)
            if mode == "persist":
                persisted = await asyncio.to_thread(self._persist, event, key_hash,
                                                    ttl_days=self._policy["ttl_days"],
                                                    capacity=self._policy["capacity"])
                if not persisted:
                    self._publish(self._event("conversation.drop", {
                        "reason": "persist_error", "request_id": request_id,
                    }, request_id=request_id))
        except Exception:
            self._drop("capture_error")

    def _persist(self, event: dict, key_hash: str, *, ttl_days: int, capacity: int) -> bool:
        try:
            created_ts = time.time()
            data = event["data"]
            with self._connect() as conn:
                conn.execute("INSERT OR IGNORE INTO conversation_events "
                             "(event_id,request_id,created_at,created_ts,key_hash,key_ref,model,protocol,status,capture_mode,content_mode,payload_json) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (event["id"], event["request_id"], event["created_at"], created_ts, key_hash,
                              data["key_ref"], data["model"], data["protocol"], data["status"],
                              data["capture_mode"], data["content_mode"], _json(data["payload"])))
                cutoff = time.time() - ttl_days * 86400
                ttl_deleted = conn.execute("DELETE FROM conversation_events WHERE created_ts < ?", (cutoff,)).rowcount
                capacity_deleted = conn.execute("DELETE FROM conversation_events WHERE seq IN "
                                                "(SELECT seq FROM conversation_events ORDER BY seq DESC LIMIT -1 OFFSET ?)",
                                                (capacity,)).rowcount
                if ttl_deleted: self._deleted["ttl"] = self._deleted.get("ttl", 0) + ttl_deleted
                if capacity_deleted: self._deleted["capacity"] = self._deleted.get("capacity", 0) + capacity_deleted
            return True
        except Exception:
            self._drop("persist_error")
            return False

    async def list_records(self, *, limit: int = 50, cursor: str = "", key: str = "",
                           model: str = "", protocol: str = "", status: str = "",
                           from_ts: float | None = None, to_ts: float | None = None) -> dict:
        limit = min(max(int(limit), 1), 200)
        conditions, args = [], []
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
                if isinstance(decoded, list) and len(decoded) == 2:
                    created_ts, event_id = float(decoded[0]), str(decoded[1])
                    args += [created_ts, created_ts, event_id]
                    conditions.append("(created_ts < ? OR (created_ts = ? AND event_id < ?))")
                else:
                    args.append(float(decoded)); conditions.append("created_ts < ?")
            except (ValueError, TypeError):
                return {"records": [], "next_cursor": None, "error": "invalid cursor"}
        if key:
            conditions.append("(key_hash=? OR key_ref=?)"); args += [key, key]
        if model: conditions.append("model=?"); args.append(model[:128])
        if protocol: conditions.append("protocol=?"); args.append(protocol[:64])
        if status: conditions.append("status=?"); args.append(status[:16])
        if from_ts is not None: conditions.append("created_ts>=?"); args.append(from_ts)
        if to_ts is not None: conditions.append("created_ts<=?"); args.append(to_ts)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = await asyncio.to_thread(self._query, where, args, limit)
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([rows[-1]["created_ts"], rows[-1]["id"]], separators=(",", ":")).encode()
            ).decode().rstrip("=")
        for row in rows:
            row.pop("payload", None)
            row.pop("key_hash", None)
            row.pop("created_ts", None)
        return {"records": rows, "next_cursor": next_cursor, "has_more": bool(next_cursor)}

    def _query(self, where: str, args: list, limit: int) -> list[dict]:
        with self._connect() as conn:
            records = []
            for row in conn.execute("SELECT seq,event_id,request_id,created_at,created_ts,key_hash,key_ref,model,protocol,status,capture_mode,content_mode,payload_json "
                                    "FROM conversation_events" + where + " ORDER BY created_ts DESC, seq DESC LIMIT ?", (*args, limit + 1)):
                records.append({"id": row[1], "request_id": row[2], "created_at": row[3], "created_ts": row[4],
                                "key_hash": row[5], "key_ref": row[6], "model": row[7], "protocol": row[8],
                                "status": row[9], "capture_mode": row[10], "content_mode": row[11],
                                "payload": json.loads(row[12])})
            return records

    async def detail(self, request_id: str) -> dict | None:
        row = await asyncio.to_thread(self._detail, request_id)
        if row is not None:
            row.pop("key_hash", None); row.pop("created_ts", None)
        return row

    def _detail(self, request_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT event_id,request_id,created_at,created_ts,key_hash,key_ref,model,protocol,status,capture_mode,content_mode,payload_json "
                               "FROM conversation_events WHERE request_id=? OR json_extract(payload_json, '$.upstream_request_id')=? "
                               "ORDER BY seq DESC LIMIT 1", (request_id, request_id)).fetchone()
            if row is None: return None
            return {"id": row[0], "request_id": row[1], "created_at": row[2], "created_ts": row[3],
                    "key_hash": row[4], "key_ref": row[5], "model": row[6], "protocol": row[7],
                    "status": row[8], "capture_mode": row[9], "content_mode": row[10], "payload": json.loads(row[11])}

    async def summary(self) -> dict:
        self._refresh_policy()
        today = datetime.now(timezone.utc).date().isoformat()
        persisted = await asyncio.to_thread(self._count_since, today)
        return {"capture_mode": self._policy["mode"], "captured": self._captured,
                "original": self._captured, "persisted": persisted,
                "dropped": sum(self._dropped.values()), "dropped_by_reason": dict(self._dropped),
                "deleted": dict(self._deleted),
                "capacity": self._policy["capacity"], "ttl_days": self._policy["ttl_days"],
                "sse_clients": len(self._subscribers), "last_event_id": self._replay[-1]["id"] if self._replay else None}

    def _count_since(self, day: str) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM conversation_events WHERE created_at >= ?", (day,)).fetchone()[0])

    def authorize_sse(self, headers: Any) -> bool:
        expected = os.environ.get("CONVERSATION_MONITOR_SSE_TOKEN", "").strip()
        supplied = headers.get("x-conversation-monitor-token", "")
        auth = headers.get("authorization", "")
        if isinstance(auth, str) and auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        return bool(expected) and isinstance(supplied, str) and hashlib.sha256(supplied.encode()).digest() == hashlib.sha256(expected.encode()).digest()

    async def sse(self, last_event_id: str = "") -> AsyncIterator[bytes]:
        last_seq = 0
        try: last_seq = int(str(last_event_id).removeprefix("evt_"))
        except (TypeError, ValueError): pass
        backlog = [event for event in self._replay if int(event["id"].removeprefix("evt_")) > last_seq]
        if last_event_id and not backlog and self._replay and last_seq < int(self._replay[0]["id"].removeprefix("evt_")):
            yield b"event: reset\ndata: {\"reason\":\"replay_unavailable\"}\n\n"
        queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_LIMIT)
        self._subscribers.add(queue)
        try:
            for event in backlog:
                yield self._format_sse(event)
            while True:
                try: event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                yield self._format_sse(event)
        finally:
            self._subscribers.discard(queue)

    @staticmethod
    def _format_sse(event: dict) -> bytes:
        return (f"id: {event['id']}\nevent: {event['event']}\ndata: "
                + _json(event["data"]) + "\n\n").encode("utf-8")


MONITOR = ConversationMonitor()


def schedule_capture(**kwargs: Any) -> None:
    """Schedule capture without ever joining the model response path."""
    try:
        task = asyncio.create_task(MONITOR.capture(**kwargs))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:
        MONITOR._drop("capture_no_loop")
