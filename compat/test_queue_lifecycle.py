"""Live-view lifecycle checks only; no real inference or production traffic."""
import asyncio
import json
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

import compat_proxy as cp
from live_requests import LiveRequests, LiveRequestsMiddleware, read_snapshot


def scope(path="/v1/chat/completions"):
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json"),
                        (b"authorization", b"Bearer sk-lifecycle-private"),
                        (b"x-request-id", b"caller-duplicate-id"), (b"x-real-ip", b"192.0.2.9")],
            "client": ("127.0.0.1", 123), "server": ("localhost", 8400)}


async def invoke(app, path, *, stream=True, send=None):
    body = json.dumps({"model": "test-model", "stream": stream,
                       "messages": [{"role": "user", "content": "private prompt marker"}]}).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    messages = []

    async def collect(message):
        messages.append(message)
    await app(scope(path), receive, send or collect)
    return messages


async def until(check):
    async with asyncio.timeout(5):
        while not check():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages"])
def test_twelve_pending_streaming_and_completion(tmp_path, monkeypatch, path):
    async def run():
        tracker = LiveRequests(tmp_path / "live.db")
        monkeypatch.setattr(cp, "LIVE_REQUESTS", tracker)
        # Keep original capture independent of live observation.
        monkeypatch.setattr(cp, "schedule_capture", lambda **kw: None)
        entered, release_headers, release_body, end = [], asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release_body.wait()
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                await end.wait()
                yield b'data: [DONE]\n\n'

        async def upstream(request):
            entered.append(request)
            await release_headers.wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            monkeypatch.setattr(cp, "client", client)
            app = Starlette(routes=[Route(path, cp.compat_proxy, methods=["POST"])],
                            middleware=[Middleware(LiveRequestsMiddleware, tracker=tracker)])
            tasks = [asyncio.create_task(invoke(app, path)) for _ in range(12)]
            await until(lambda: len(entered) == 12)
            await tracker.flush()
            pending = read_snapshot(tracker.db_path)
            assert len(pending["requests"]) == 12
            assert {r["phase"] for r in pending["requests"]} == {"awaiting_response"}
            assert len({r["request_id"] for r in pending["requests"]}) == 12
            assert all(r["model"] == "test-model" and r["source_ip"] == "192.0.2.9" for r in pending["requests"])
            assert "sk-lifecycle-private" not in json.dumps(pending)
            assert "private prompt marker" not in json.dumps(pending)
            assert "caller-duplicate-id" not in json.dumps(pending)
            release_headers.set()
            # Headers / SSE prelude do not assert scheduler running.
            await asyncio.sleep(0.05)
            await tracker.flush()
            assert {r["phase"] for r in read_snapshot(tracker.db_path)["requests"]} == {"awaiting_response"}
            release_body.set()
            await until(lambda: all(r["phase"] == "receiving_response" for r in tracker.rows.values()))
            await tracker.flush()
            assert len(read_snapshot(tracker.db_path)["requests"]) == 12
            end.set()
            results = await asyncio.gather(*tasks)
            await tracker.flush()
            assert all(b"[DONE]" in b"".join(m.get("body", b"") for m in result) for result in results)
            assert read_snapshot(tracker.db_path)["requests"] == []
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["json", "rejected", "timeout", "cancel", "send_disconnect"])
def test_terminal_paths_remove_live_rows(tmp_path, monkeypatch, mode):
    async def run():
        tracker = LiveRequests(tmp_path / "live.db")
        monkeypatch.setattr(cp, "LIVE_REQUESTS", tracker)
        monkeypatch.setattr(cp, "schedule_capture", lambda **kw: None)
        entered = asyncio.Event()

        async def upstream(request):
            entered.set()
            if mode == "cancel":
                await asyncio.Event().wait()
            if mode == "timeout":
                raise httpx.ReadTimeout("test timeout")
            if mode == "rejected":
                class ErrorStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'{"error":"unauthorized"}'
                return httpx.Response(401, headers={"content-type": "application/json"}, stream=ErrorStream())
            return httpx.Response(200, json={"id": "test-completion", "ok": True})

        async def disconnected_send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            monkeypatch.setattr(cp, "client", client)
            app = Starlette(routes=[Route("/v1/chat/completions", cp.compat_proxy, methods=["POST"])],
                            middleware=[Middleware(LiveRequestsMiddleware, tracker=tracker)])
            task = asyncio.create_task(invoke(app, "/v1/chat/completions", stream=False,
                                             send=disconnected_send if mode == "send_disconnect" else None))
            await entered.wait()
            if mode == "cancel":
                task.cancel()
            if mode in {"cancel", "send_disconnect"}:
                with suppress(asyncio.CancelledError, OSError):
                    await task
            else:
                messages = await task
                code = messages[0]["status"]
                assert code == {"json": 200, "rejected": 401, "timeout": 502}[mode]
            await tracker.flush()
            assert read_snapshot(tracker.db_path)["available"]
            assert read_snapshot(tracker.db_path)["requests"] == []
    asyncio.run(run())


def test_crashed_worker_metadata_pruned_without_new_publisher(tmp_path):
    import sqlite3
    import time
    from live_requests import prune_expired
    tracker = LiveRequests(tmp_path / "live.db")

    async def create():
        await tracker.begin(scope())
        await tracker.flush()
    asyncio.run(create())
    with sqlite3.connect(tracker.db_path) as db:
        db.execute("UPDATE live_workers SET heartbeat=?", (time.time()-4000,))
    assert not read_snapshot(tracker.db_path)["stale_requests"]
    prune_expired(tracker.db_path)
    with sqlite3.connect(tracker.db_path) as db:
        assert db.execute("SELECT count(*) FROM live_workers").fetchone()[0] == 0


def test_worker_leases_and_failed_storage(tmp_path):
    async def run():
        first, second = LiveRequests(tmp_path / "live.db"), LiveRequests(tmp_path / "live.db")
        await first.begin(scope())
        await second.begin(scope())
        await first.flush()
        await second.flush()
        assert len(read_snapshot(first.db_path)["requests"]) == 2
        import time
        stale = read_snapshot(first.db_path, now=time.time() + 40)
        assert not stale["available"]
        assert stale["requests"] == [] and len(stale["stale_requests"]) == 2
        # Observation failure is fail-open; no writable volume required for forwarding.
        blocked = tmp_path / "not-directory"
        blocked.write_text("x")
        failed = LiveRequests(blocked / "live.db")
        await failed.begin(scope())
        await failed.flush()
        assert not failed.available
        assert not read_snapshot(failed.db_path)["available"]
    asyncio.run(run())


def test_busy_storage_and_unicode_never_block_inference(tmp_path, monkeypatch):
    async def run():
        import threading
        tracker = LiveRequests(tmp_path / "live.db")
        await tracker.start()
        original = tracker._publish
        entered, unblock = threading.Event(), threading.Event()

        def slow_storage(rows, overflow):
            entered.set()
            unblock.wait(5)
            original(rows, overflow)

        monkeypatch.setattr(tracker, "_publish", slow_storage)
        started, sent = [], []

        async def upstream_app(scope, receive, send):
            started.append(scope["live_request_id"])
            await tracker.describe(scope["live_request_id"], model="m\ud800", streaming=True)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first response", "more_body": False})

        async def send(message):
            sent.append(message)

        app = LiveRequestsMiddleware(upstream_app, tracker)
        try:
            # Hold publisher busy while actual inference and first body finish.
            first = await tracker.begin(scope())
            await tracker.describe(first, model="m\ud800", streaming=True)
            await until(entered.is_set)
            async with asyncio.timeout(0.5):
                await asyncio.gather(*(invoke(app, "/v1/chat/completions", send=send) for _ in range(12)))
            assert len(started) == 12
            assert sum(m.get("body") == b"first response" for m in sent) == 12
        finally:
            unblock.set()
        await tracker.flush()
        assert tracker.available
        assert read_snapshot(tracker.db_path)["requests"][0]["model"] == "m?"
        await tracker.stop()
        assert read_snapshot(tracker.db_path)["available"] is False
    asyncio.run(run())
