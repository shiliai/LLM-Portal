"""Read-only, indexed queries for the Console usage screen."""
import base64
import json
from decimal import Decimal
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
_CACHED = "coalesce(nullif(metadata #>> '{usage_object,prompt_tokens_details,cached_tokens}','')::bigint,nullif(metadata #>> '{usage_object,cache_read_input_tokens}','')::bigint,0)"
_ERROR = "coalesce(metadata #>> '{error_information,error_message}',metadata->>'error_str',metadata->>'status_code','failure')"

def _plain(row):
    return {key: int(value) if isinstance(value, Decimal) else value for key, value in dict(row).items()}

async def aggregate(days):
    start, end = window(days)
    bucket = 'hour' if days == 1 else 'day'
    conn = await connection()
    try:
        result = await conn.fetch(f'''WITH base AS MATERIALIZED (
          SELECT "startTime", "completionStartTime", api_key, coalesce(nullif(model_group,''),model,'?') model,
            coalesce(prompt_tokens,0) prompt_tokens, coalesce(completion_tokens,0) completion_tokens,
            coalesce(request_duration_ms,0) duration_ms, status, coalesce(portal_cached_tokens,0) cached_tokens,
            CASE WHEN status='failure' THEN {_ERROR} ELSE '' END error
          FROM "LiteLLM_SpendLogs" WHERE "startTime">=$1 AND "startTime"<$2 AND {_VALID_KEY}
        )
        SELECT 'total' kind, jsonb_build_object('requests',count(*),'prompt_tokens',coalesce(sum(prompt_tokens),0),'completion_tokens',coalesce(sum(completion_tokens),0),'cached_tokens',coalesce(sum(cached_tokens),0),'failures',count(*) filter(where status='failure'),'avg_ms',coalesce(round(avg(duration_ms))::bigint,0),'avg_tft',coalesce(round(avg(extract(epoch from ("completionStartTime"-"startTime"))*1000) filter(where "completionStartTime" is not null))::bigint,0)) payload FROM base
        UNION ALL SELECT 'buckets',coalesce(jsonb_agg(jsonb_build_object('b',b,'reqs',reqs,'in',i,'out',o,'cache',cache,'avg_tft',avg_tft) ORDER BY b),'[]'::jsonb) FROM (SELECT date_trunc('{bucket}',"startTime" AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Shanghai') b,count(*) reqs,sum(prompt_tokens) i,sum(completion_tokens) o,sum(cached_tokens) cache,coalesce(round(avg(extract(epoch from ("completionStartTime"-"startTime"))*1000) filter(where "completionStartTime" is not null))::bigint,0) avg_tft FROM base GROUP BY 1) x
        UNION ALL SELECT 'rows',coalesce(jsonb_agg(jsonb_build_object('api_key',api_key,'model',model,'requests',requests,'prompt_tokens',prompt_tokens,'completion_tokens',completion_tokens,'cached_tokens',cached_tokens,'avg_ms',avg_ms) ORDER BY requests DESC),'[]'::jsonb) FROM (SELECT api_key,model,count(*) requests,sum(prompt_tokens) prompt_tokens,sum(completion_tokens) completion_tokens,sum(cached_tokens) cached_tokens,coalesce(round(avg(duration_ms))::bigint,0) avg_ms FROM base GROUP BY 1,2) x
        UNION ALL SELECT 'errors',coalesce(jsonb_agg(jsonb_build_object('startTime',"startTime",'api_key',api_key,'model',model,'detail',left(error,160)) ORDER BY "startTime" DESC),'[]'::jsonb) FROM (SELECT * FROM base WHERE status='failure' ORDER BY "startTime" DESC LIMIT 10) x''', start, end)
        data = {row['kind']: json.loads(row['payload']) for row in result}
        return data['total'], data['buckets'], data['rows'], data['errors']
    finally: await conn.close()

async def logs(days, cursor, limit, key_suffix="", model=""):
    start, end = window(days); cur = decode_cursor(cursor)
    where, args = ['"startTime">=$1','"startTime"<$2',_VALID_KEY], [start,end]
    if key_suffix:
        args.append(key_suffix)
        where.append(f'right(api_key,4)=${len(args)}')
    if model:
        args.append(model)
        where.append(f"coalesce(nullif(model_group,''),model,'?')=${len(args)}")
    if cur:
        args += list(cur)
        where.append(f'("startTime",request_id)<(${len(args)-1},${len(args)})')
    args.append(limit + 1); conn = await connection()
    try:
        sql = f'''SELECT request_id,"startTime",api_key,coalesce(nullif(model_group,''),model,'?') model,call_type,
          coalesce(metadata #>> '{{spend_logs_metadata,effort}}',metadata #>> '{{requester_metadata,effort}}',metadata->>'effort','') effort,
          coalesce(prompt_tokens,0) prompt_tokens,coalesce(completion_tokens,0) completion_tokens,
          {_CACHED} cached_tokens,
          coalesce(round(extract(epoch from ("completionStartTime"-"startTime"))*1000)::bigint,0) tft_ms,
          coalesce(request_duration_ms,0) duration_ms,status,session_id,requester_ip_address ip,
          left(coalesce(metadata #>> '{{error_information,error_message}}',metadata->>'error_str',metadata->>'status_code',''),160) error
          FROM "LiteLLM_SpendLogs" WHERE ''' + ' AND '.join(where) + ' ORDER BY "startTime" DESC,request_id DESC LIMIT $' + str(len(args))
        rows = [_plain(x) for x in await conn.fetch(sql,*args)]; more=len(rows)>limit; rows=rows[:limit]
        return rows, encode_cursor(rows[-1]['startTime'],rows[-1]['request_id']) if more else None
    finally: await conn.close()
