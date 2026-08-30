from datetime import datetime, timezone

from usage_db import decode_cursor, encode_cursor, window


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
