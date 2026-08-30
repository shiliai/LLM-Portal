from datetime import datetime, timezone

from usage_db import decode_cursor, encode_cursor, window
import usage_db
import asyncio


def test_cst_today_starts_at_cst_midnight():
    start, end = window(1, datetime(2026, 8, 30, 0, 30, tzinfo=timezone.utc))
    assert start == datetime(2026, 8, 29, 16, 0)
    assert end == datetime(2026, 8, 30, 0, 30)


def test_cst_multi_day_window_uses_natural_days():
    start, _ = window(7, datetime(2026, 8, 30, 16, 1, tzinfo=timezone.utc))
    assert start == datetime(2026, 8, 24, 16, 0)


def test_cursor_is_stable_and_rejects_invalid_input():
    value = encode_cursor(datetime(2026, 8, 30, 1, 2, 3), "req-123")
    assert decode_cursor(value) == (datetime(2026, 8, 30, 1, 2, 3), "req-123")
    assert decode_cursor("not-a-cursor") is None


def test_sql_preserves_anthropic_cache_and_litellm_failure_semantics():
    assert "cache_read_input_tokens" in usage_db._CACHED
    assert "error_information,error_message" in usage_db._ERROR


def test_logs_uses_keyset_cursor_and_page_limit(monkeypatch):
    seen = {}
    class Conn:
        async def fetch(self, sql, *args):
            seen["sql"], seen["args"] = sql, args
            return []
        async def close(self): pass
    async def conn(): return Conn()
    monkeypatch.setattr(usage_db, "connection", conn)
    cursor = encode_cursor(datetime(2026, 8, 30, 1, 2, 3), "req-123")
    assert asyncio.run(usage_db.logs(7, cursor, 20)) == ([], None)
    assert '("startTime",request_id)<($3,$4)' in seen["sql"]
    assert seen["args"][-1] == 21
