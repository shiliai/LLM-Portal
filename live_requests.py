"""Metadata-only, best-effort live observation shared by compat and console.

One leased snapshot per compat worker, rather than a SQL write per token.
No request/response content, credentials or caller-controlled correlation IDs.
An HTTP response is not evidence of the inference scheduler's running state.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import sqlite3
import time
import uuid
from contextlib import suppress
from pathlib import Path

HEARTBEAT_SECONDS = 5
STALE_SECONDS = 30
MAX_REQUESTS = 2000
RETENTION_SECONDS = 3600


class LiveRequests:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.worker = uuid.uuid4().hex
        self.rows: dict[str, dict] = {}
        self.overflow = 0
        self.available = False
        self._task = None
        self._publisher = None
        self._dirty = False
        self._closing = False

    def _publish(self, rows: list[dict], overflow: int) -> None:
        # ASCII escaping also accepts legal JSON containing lone surrogates.
        payload = json.dumps(rows, ensure_ascii=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=0.1) as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("CREATE TABLE IF NOT EXISTS live_workers ("
                       "worker TEXT PRIMARY KEY, heartbeat REAL NOT NULL, payload TEXT NOT NULL, overflow INTEGER NOT NULL)")
            now = time.time()
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
            try:
                await asyncio.to_thread(self._publish, rows, self.overflow)
                self.available = True
            except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError):
                self.available = False

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
                          "phase_since": now, "streaming": False}
        self.changed()
        return rid

    async def describe(self, rid: str | None, *, model: object, streaming: bool) -> None:
        if rid in self.rows:
            self.rows[rid].update(model=model[:200].encode("utf-8", errors="replace").decode("utf-8") if isinstance(model, str) else "",
                                  streaming=streaming, phase="awaiting_response", phase_since=time.time())
            self.changed()

    async def responding(self, rid: str) -> None:
        if rid in self.rows and self.rows[rid]["phase"] != "receiving_response":
            self.rows[rid].update(phase="receiving_response", phase_since=time.time())
            self.changed()

    async def finish(self, rid: str) -> None:
        if self.rows.pop(rid, None) is None:
            self.overflow = max(0, self.overflow - 1)
        self.changed()


def prune_expired(db_path: str | Path) -> None:
    # Console runs this even if every compat worker has stopped. mode=rw
    # avoids creating a root-owned file before the unprivileged writer starts.
    with suppress(sqlite3.Error, OSError):
        with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True, timeout=0.1) as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM live_workers WHERE heartbeat < ?", (time.time() - RETENTION_SECONDS,))


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

        async def observed_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                await self.tracker.responding(rid)
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            await self.tracker.finish(rid)
