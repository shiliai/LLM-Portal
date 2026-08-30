"""Read-only, indexed queries for the Console usage screen."""
import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

_CST = timezone(timedelta(hours=8))

def window(days: int, now: Optional[datetime] = None):
    now = (now or datetime.now(timezone.utc)).astimezone(_CST)
    start_day = now.date() - timedelta(days=days - 1)
    start = datetime.combine(start_day, datetime.min.time(), _CST).astimezone(timezone.utc).replace(tzinfo=None)
    return start, now.astimezone(timezone.utc).replace(tzinfo=None)

def encode_cursor(start, request_id):
    return base64.urlsafe_b64encode(json.dumps([start.isoformat(), request_id]).encode()).decode().rstrip("=")

def decode_cursor(value):
    if not value: return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        start, request_id = json.loads(raw)
        return datetime.fromisoformat(start), str(request_id)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

async def connection():
    # Imported lazily: unit tests can exercise date/cursor behavior without a driver.
    import asyncpg
    import os
    return await asyncpg.connect(os.environ["USAGE_DATABASE_URL"], command_timeout=5)

_VALID_KEY = "(api_key = ('litellm_proxy_' || 'master' || '_key') OR api_key ~ '^[0-9a-f]{64}$')"

async def aggregate(days):
    start, end = window(days)
    conn = await connection()
    try:
        total = await conn.fetchrow(f'''SELECT count(*) requests,
          coalesce(sum(prompt_tokens),0) prompt_tokens, coalesce(sum(completion_tokens),0) completion_tokens,
          coalesce(sum(nullif(metadata #>> '{{usage_object,prompt_tokens_details,cached_tokens}}','')::bigint),0) cached_tokens,
          count(*) filter (where status='failure') failures,
          coalesce(round(avg(request_duration_ms))::bigint,0) avg_ms,
          coalesce(round(avg(extract(epoch from ("completionStartTime"-"startTime"))*1000) filter (where "completionStartTime" is not null))::bigint,0) avg_tft
          FROM "LiteLLM_SpendLogs" WHERE "startTime">=$1 AND "startTime"<$2 AND {_VALID_KEY}''', start, end)
        bucket = 'hour' if days == 1 else 'day'
        rows = await conn.fetch(f'''SELECT date_trunc('{bucket}', "startTime" AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Shanghai') b,
          count(*) reqs, coalesce(sum(prompt_tokens),0) "in", coalesce(sum(completion_tokens),0) out,
          coalesce(sum(nullif(metadata #>> '{{usage_object,prompt_tokens_details,cached_tokens}}','')::bigint),0) cache,
          coalesce(round(avg(extract(epoch from ("completionStartTime"-"startTime"))*1000) filter (where "completionStartTime" is not null))::bigint,0) avg_tft
          FROM "LiteLLM_SpendLogs" WHERE "startTime">=$1 AND "startTime"<$2 AND {_VALID_KEY} GROUP BY 1 ORDER BY 1''', start, end)
        by = await conn.fetch(f'''SELECT api_key, coalesce(nullif(model_group,''),model,'?') model, count(*) requests,
          coalesce(sum(prompt_tokens),0) prompt_tokens, coalesce(sum(completion_tokens),0) completion_tokens,
          coalesce(sum(nullif(metadata #>> '{{usage_object,prompt_tokens_details,cached_tokens}}','')::bigint),0) cached_tokens,
          coalesce(round(avg(request_duration_ms))::bigint,0) avg_ms FROM "LiteLLM_SpendLogs"
          WHERE "startTime">=$1 AND "startTime"<$2 AND {_VALID_KEY} GROUP BY 1,2 ORDER BY 3 DESC''', start, end)
        errors = await conn.fetch('''SELECT "startTime",api_key,coalesce(nullif(model_group,''),model,'?') model,
          left(coalesce(metadata->>'error_str',metadata->>'status_code','failure'),160) detail
          FROM "LiteLLM_SpendLogs" WHERE "startTime">=$1 AND "startTime"<$2 AND status='failure' ORDER BY "startTime" DESC LIMIT 10''', start,end)
        return dict(total), [dict(x) for x in rows], [dict(x) for x in by], [dict(x) for x in errors]
    finally: await conn.close()

async def logs(days, cursor, limit):
    start, end = window(days); cur = decode_cursor(cursor)
    where, args = ['"startTime">=$1','"startTime"<$2',_VALID_KEY], [start,end]
    if cur: where.append('("startTime",request_id)<($3,$4)'); args += list(cur)
    args.append(limit + 1); conn = await connection()
    try:
        sql = '''SELECT request_id,"startTime",api_key,coalesce(nullif(model_group,''),model,'?') model,call_type,
          coalesce(metadata #>> '{spend_logs_metadata,effort}',metadata #>> '{requester_metadata,effort}',metadata->>'effort','') effort,
          coalesce(prompt_tokens,0) prompt_tokens,coalesce(completion_tokens,0) completion_tokens,
          coalesce(nullif(metadata #>> '{usage_object,prompt_tokens_details,cached_tokens}','')::bigint,0) cached_tokens,
          coalesce(round(extract(epoch from ("completionStartTime"-"startTime"))*1000)::bigint,0) tft_ms,
          coalesce(request_duration_ms,0) duration_ms,status,session_id,requester_ip_address ip,
          left(coalesce(metadata->>'error_str',metadata->>'status_code',''),160) error
          FROM "LiteLLM_SpendLogs" WHERE ''' + ' AND '.join(where) + ' ORDER BY "startTime" DESC,request_id DESC LIMIT $' + str(len(args))
        rows = [dict(x) for x in await conn.fetch(sql,*args)]; more=len(rows)>limit; rows=rows[:limit]
        return rows, encode_cursor(rows[-1]['startTime'],rows[-1]['request_id']) if more else None
    finally: await conn.close()
