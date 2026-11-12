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

        def slow_storage(rows, overflow, finished):
            entered.set()
            unblock.wait(5)
            original(rows, overflow, finished)

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


def test_history_persists_and_classifies_terminal_paths(tmp_path):
    from live_requests import read_history

    async def run():
        tracker = LiveRequests(tmp_path / 'live.db')
        await tracker.start()
        ids = []
        for status, mode in [(200, 'json'), (401, 'json'), (200, 'truncated'), (200, 'disconnect')]:
            async def app(s, receive, send):
                await tracker.describe(s['live_request_id'], model='unit', streaming=mode != 'json',
                                       body={'messages': [{'role': 'user', 'content': 'never persist this'}],
                                             'tools': [{}], 'max_tokens': 64}, request_bytes=123)
                await send({'type': 'http.response.start', 'status': status,
                            'headers': [(b'content-type', b'application/json' if mode == 'json' else b'text/event-stream')]})
                await send({'type': 'http.response.body', 'body': b'{"usage":{"prompt_tokens":12,"completion_tokens":3}}' if mode == 'json' else b'data: {"choices":[]}\n\n', 'more_body': False})

            async def send(message):
                if mode == 'disconnect' and message['type'] == 'http.response.body':
                    raise OSError('disconnected')
            with suppress(OSError):
                await invoke(LiveRequestsMiddleware(app, tracker), '/v1/chat/completions', send=send)
            await tracker.flush()
            ids = [r['request_id'] for r in read_history(tracker.db_path)['records']]
        records = read_history(tracker.db_path)['records']
        assert {r['outcome'] for r in records} == {'completed', 'failed', 'interrupted', 'disconnected'}
        completed = next(r for r in records if r['outcome'] == 'completed')
        assert completed['total_input_tokens'] == 12 and completed['output_tokens'] == 3
        assert completed['message_count'] == 1 and completed['tool_count'] == 1
        assert completed['max_output_tokens'] == 64 and completed['request_bytes'] == 123
        assert 'never persist this' not in json.dumps(records)
        assert 'sk-lifecycle-private' not in tracker.db_path.read_bytes().decode('latin1')
        await tracker.stop()
        restarted = LiveRequests(tracker.db_path)
        await restarted.start()
        assert {r['request_id'] for r in read_history(tracker.db_path)['records']} == set(ids)
        assert read_snapshot(tracker.db_path)['requests'] == []
        await restarted.stop()
    asyncio.run(run())


def test_anthropic_live_usage_split_sse_cache_and_no_byte_changes(tmp_path):
    from live_requests import read_history

    async def run():
        tracker = LiveRequests(tmp_path / 'live.db')
        release, started = asyncio.Event(), asyncio.Event()
        payload = (b'event: message_start\r\ndata: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0,"cache_read_input_tokens":20,"cache_creation_input_tokens":30}}}\r\n\r\n')
        ending = b'data: {"type":"message_delta","usage":{"output_tokens":8}}\n\ndata: {"type":"message_stop"}\n\n'
        sent = []

        async def app(s, receive, send):
            await tracker.describe(s['live_request_id'], model='anthropic-model', streaming=True)
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
            for offset in range(0, len(payload), 7):
                await send({'type': 'http.response.body', 'body': payload[offset:offset+7], 'more_body': True})
            started.set()
            await release.wait()
            await send({'type': 'http.response.body', 'body': ending, 'more_body': False})

        async def send(message):
            sent.append(message.get('body', b''))
        task = asyncio.create_task(invoke(LiveRequestsMiddleware(app, tracker), '/v1/messages', send=send))
        await started.wait(); await tracker.flush()
        row = read_snapshot(tracker.db_path)['requests'][0]
        assert row['total_input_tokens'] == 60 and row['input_tokens'] == 10
        assert row['output_tokens'] == 0 and row['usage_source'] == 'backend_usage'
        release.set(); await task; await tracker.flush()
        row = read_history(tracker.db_path)['records'][0]
        assert row['total_input_tokens'] == 60 and row['output_tokens'] == 8
        assert row['outcome'] == 'completed' and b''.join(sent) == payload + ending
    asyncio.run(run())


def test_history_crash_unknown_resume_dedup_pagination_and_retention(tmp_path):
    import sqlite3
    import time
    import live_requests as live

    async def run():
        tracker = LiveRequests(tmp_path / 'live.db')
        rid = await tracker.begin(scope())
        await tracker.describe(rid, model='model-a', streaming=True)
        await tracker.flush()
        with sqlite3.connect(tracker.db_path) as db:
            db.execute('UPDATE live_workers SET heartbeat=?', (time.time()-40,))
        live.prune_expired(tracker.db_path)
        assert live.read_history(tracker.db_path)['records'][0]['outcome'] == 'unknown'
        tracker.changed(); await tracker.flush()
        assert live.read_history(tracker.db_path)['records'] == []
        await tracker.finish(rid)
        for i in range(5):
            other = await tracker.begin(scope())
            await tracker.describe(other, model='model-b', streaming=False)
            await tracker.finish(other, outcome='failed')
        await tracker.flush()
        all_ids, cursor = [], ''
        while True:
            data = live.read_history(tracker.db_path, limit=2, cursor=cursor)
            all_ids.extend(r['request_id'] for r in data['records'])
            if not data['has_more']: break
            cursor = data['next_cursor']
        assert len(all_ids) == len(set(all_ids)) == 6
        assert len(live.read_history(tracker.db_path, models=['model-b'], outcome='failed')['records']) == 5
        assert len(live.read_history(tracker.db_path, search='model-a')['records']) == 1
        with pytest.raises(ValueError): live.read_history(tracker.db_path, cursor='not valid!')
        with sqlite3.connect(tracker.db_path) as db:
            db.execute('UPDATE request_history SET ended_at=? WHERE request_id=?', (time.time()-live.HISTORY_SECONDS-1, rid))
        live.prune_expired(tracker.db_path)
        assert len(live.read_history(tracker.db_path)['records']) == 5
        await tracker.stop()
    asyncio.run(run())


def test_large_sse_event_does_not_hide_later_usage_and_unknown_is_not_zero(tmp_path):
    from live_requests import ResponseMetadata, read_history, MAX_RESPONSE_METADATA_BYTES

    async def run():
        tracker = LiveRequests(tmp_path / 'live.db')
        rid = await tracker.begin(scope())
        obs = ResponseMetadata(tracker, rid)
        obs.observe({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
        obs.observe({'type':'http.response.body','body':b'data: '+b'x'*(MAX_RESPONSE_METADATA_BYTES+10)+b'\n\ndata: {"usage":{"prompt_tokens":0,"completion_tokens":4,"prompt_tokens_details":{"cached_tokens":0}}}\n\ndata: [DONE]\n\n','more_body':False})
        obs.complete()
        assert obs.stream_complete and tracker.rows[rid]['total_input_tokens'] == 0
        assert tracker.rows[rid]['output_tokens'] == 4
        unknown = await tracker.begin(scope())
        tracker.usage(unknown, {'usage': {'prompt_tokens': -1, 'completion_tokens': True}})
        assert tracker.rows[unknown]['total_input_tokens'] is None
        await tracker.finish(unknown);await tracker.finish(rid);await tracker.flush()
        assert len(read_history(tracker.db_path)['records']) == 2
    asyncio.run(run())


@pytest.mark.parametrize('spec', ['2.3', '2.4'])
def test_real_streaming_response_disconnect_history(tmp_path, spec):
    from starlette.responses import StreamingResponse
    from live_requests import read_history

    async def run():
        tracker = LiveRequests(tmp_path / 'live.db')
        started = asyncio.Event()

        async def chunks():
            yield b'data: {"choices":[]}\n\n'
            await asyncio.Event().wait()

        async def app(s, receive, send):
            await StreamingResponse(chunks(), media_type='text/event-stream')(s, receive, send)

        async def receive():
            await started.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            if message['type'] == 'http.response.body':
                started.set()
                if spec == '2.4': raise OSError('client closed socket')

        s = scope();s['asgi']['spec_version'] = spec
        from starlette.requests import ClientDisconnect
        with suppress(ClientDisconnect):
            await LiveRequestsMiddleware(app, tracker)(s, receive, send)
        await tracker.flush()
        assert read_history(tracker.db_path)['records'][0]['outcome'] == 'disconnected'
    asyncio.run(run())


def test_unknown_upgrade_refreshes_index_columns_and_redirect_is_failure(tmp_path):
    import sqlite3
    import time
    from live_requests import read_history, prune_expired

    async def run():
        tracker = LiveRequests(tmp_path / 'live.db')
        rid = await tracker.begin(scope())
        await tracker.flush()
        with sqlite3.connect(tracker.db_path) as db:
            db.execute('UPDATE live_workers SET heartbeat=?', (time.time()-40,))
        prune_expired(tracker.db_path)
        await tracker.describe(rid, model='late-model', streaming=False)
        await tracker.finish(rid)
        await tracker.flush()
        assert len(read_history(tracker.db_path, models=['late-model'])['records']) == 1
        assert len(read_history(tracker.db_path, search='late-model')['records']) == 1
        async def redirect(s, receive, send):
            await send({'type':'http.response.start','status':302,'headers':[]})
            await send({'type':'http.response.body','body':b'redirect','more_body':False})
        await invoke(LiveRequestsMiddleware(redirect, tracker), '/v1/chat/completions')
        await tracker.flush()
        row=next(r for r in read_history(tracker.db_path)['records'] if r['status_code']==302)
        assert row['outcome']=='failed'
    asyncio.run(run())


def test_history_retry_buffer_is_bounded_and_loss_is_visible(tmp_path, monkeypatch):
    import live_requests as live
    async def run():
        monkeypatch.setattr(live, 'MAX_PENDING_HISTORY', 2)
        tracker=live.LiveRequests(tmp_path/'live.db')
        for _ in range(4):
            rid=await tracker.begin(scope())
            await tracker.finish(rid)
        assert len(tracker.finished)==2 and tracker.history_dropped==2
        await tracker.flush()
        data=live.read_history(tracker.db_path)
        assert len(data['records'])==2 and data['dropped_records']==2
    asyncio.run(run())


def test_drop_warning_has_explicit_worker_lifetime_scope_after_retention(tmp_path, monkeypatch):
    import time
    import live_requests as live
    async def run():
        now=time.time()
        monkeypatch.setattr(live.time, 'time', lambda: now)
        monkeypatch.setattr(live, 'MAX_PENDING_HISTORY', 1)
        tracker=live.LiveRequests(tmp_path/'live.db')
        started=now
        for _ in range(2):
            rid=await tracker.begin(scope());await tracker.finish(rid)
        await tracker.flush()
        now += 31*86400
        tracker.changed();await tracker.flush()
        data=live.read_history(tracker.db_path)
        assert data['records']==[]
        assert data['dropped_records']==1
        assert data['dropped_scope']=='reporting_workers_lifetime'
        assert data['dropped_since']==started
    asyncio.run(run())


def test_disconnect_after_final_body_is_normal_response_teardown(tmp_path):
    from live_requests import read_history
    async def run():
        tracker=LiveRequests(tmp_path/'live.db')
        async def app(s, receive, send):
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            await send({'type':'http.response.body','body':b'data: [DONE]\n\n','more_body':False})
            await receive()
        async def receive(): return {'type':'http.disconnect'}
        async def send(message): pass
        await LiveRequestsMiddleware(app,tracker)(scope(),receive,send)
        await tracker.flush()
        assert read_history(tracker.db_path)['records'][0]['outcome']=='completed'
    asyncio.run(run())


def test_disconnect_cancels_pending_final_send_is_not_completed(tmp_path):
    from starlette.responses import StreamingResponse
    from live_requests import read_history
    async def run():
        tracker=LiveRequests(tmp_path/'live.db')
        final_sending=asyncio.Event()
        async def chunks(): yield b'data: [DONE]\n\n'
        async def app(s, receive, send):
            await StreamingResponse(chunks(),media_type='text/event-stream')(s,receive,send)
        async def receive():
            await final_sending.wait();return {'type':'http.disconnect'}
        async def send(message):
            if message['type']=='http.response.body' and not message.get('more_body',False):
                final_sending.set();await asyncio.Event().wait()
        s=scope();s['asgi']['spec_version']='2.3'
        await LiveRequestsMiddleware(app,tracker)(s,receive,send)
        await tracker.flush()
        assert read_history(tracker.db_path)['records'][0]['outcome']=='disconnected'
    asyncio.run(run())
