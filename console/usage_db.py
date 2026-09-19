"""Read-only, indexed queries for the Console usage screen."""
import base64
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Optional

_CST = timezone(timedelta(hours=8))

def window(days, now: Optional[datetime] = None, today: bool = False):
    """查询窗口（UTC naive，对齐库内 LiteLLM 写入的 UTC 墙钟）。
    days 可为小数（1h=1/24）；today=True 取上海日历日零点起。"""
    now = (now or datetime.now(timezone.utc)).astimezone(_CST)
    if today:
        start = datetime.combine(now.date(), datetime.min.time(), tzinfo=_CST).astimezone(timezone.utc).replace(tzinfo=None)
    elif days >= 1:
        start_day = (now - timedelta(days=days)).date() + timedelta(days=1)
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=_CST).astimezone(timezone.utc).replace(tzinfo=None)
    else:
        start = (now - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)
    end = now.astimezone(timezone.utc).replace(tzinfo=None)
    return start, end

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

def _epoch_floor(col: str, step: int) -> str:
    # startTime 为 UTC 墙钟（naive）；显式按 UTC 转 epoch 再向下取整到步长。
    # +28800（UTC+8）让 86400 步长的日桶对齐上海日界；子日步长（≤3600s）整除
    # 该偏移，网格不变。
    return f"to_timestamp(floor((extract(epoch from ({col}) AT TIME ZONE 'UTC') + 28800)/{step})*{step} - 28800)"

async def aggregate(start, end, step=3600, filters=None):
    """汇总：totals / 自步长桶 / 细粒度行(api_key×model×api_base×call_type) / 最近错误。
    filters: {api_bases:[], api_keys:[], model:'', call_types:[]}——在 SQL 侧过滤，
    保证 KPI、趋势桶与分布行来自同一批记录（issue #106 用量页联动语义）。"""
    where = [f'"startTime">=$1', '"startTime"<$2', _VALID_KEY]
    args: list = [start, end]
    f = filters or {}
    if f.get("model"):
        args.append(f["model"])
        where.append(f"coalesce(nullif(model_group,''),model,'?')=${len(args)}")
    if f.get("api_bases"):
        args.append(list(f["api_bases"]))
        where.append(f"coalesce(api_base,'')=ANY(${len(args)})")
    if f.get("api_keys"):
        args.append(list(f["api_keys"]))
        where.append(f"api_key=ANY(${len(args)})")
    if f.get("call_types"):
        args.append(list(f["call_types"]))
        where.append(f"lower(call_type)=ANY(${len(args)})")
    if f.get("key_suffix"):
        args.append(str(f["key_suffix"]))
        where.append(f"right(api_key,4)=${len(args)}")
    conn = await connection()
    try:
        result = await conn.fetch(f'''WITH base AS MATERIALIZED (
          SELECT "startTime", "completionStartTime", api_key, coalesce(nullif(model_group,''),model,'?') model,
            coalesce(api_base,'') api_base, lower(coalesce(call_type,'')) call_type,
            coalesce(prompt_tokens,0) prompt_tokens, coalesce(completion_tokens,0) completion_tokens,
            coalesce(request_duration_ms,0) duration_ms, status, coalesce(portal_cached_tokens,0) cached_tokens,
            CASE WHEN status='failure' THEN {_ERROR} ELSE '' END error
          FROM "LiteLLM_SpendLogs" WHERE {' AND '.join(where)}
        )
        SELECT 'total' kind, jsonb_build_object('requests',count(*),'prompt_tokens',coalesce(sum(prompt_tokens),0),'completion_tokens',coalesce(sum(completion_tokens),0),'cached_tokens',coalesce(sum(cached_tokens),0),'failures',count(*) filter(where status='failure'),'avg_ms',coalesce(round(avg(duration_ms))::bigint,0),'avg_tft',coalesce(round(avg(extract(epoch from ("completionStartTime"-"startTime"))*1000) filter(where "completionStartTime" is not null))::bigint,0)) payload FROM base
        UNION ALL SELECT 'buckets',coalesce(jsonb_agg(jsonb_build_object('b',b,'reqs',reqs,'in',i,'out',o,'cache',cache,'avg_tft',avg_tft) ORDER BY b),'[]'::jsonb) FROM (SELECT {_epoch_floor('"startTime"',step)} b,count(*) reqs,sum(prompt_tokens) i,sum(completion_tokens) o,sum(cached_tokens) cache,coalesce(round(avg(extract(epoch from ("completionStartTime"-"startTime"))*1000) filter(where "completionStartTime" is not null))::bigint,0) avg_tft FROM base GROUP BY 1) x
        UNION ALL SELECT 'rows',coalesce(jsonb_agg(jsonb_build_object('api_key',api_key,'model',model,'api_base',api_base,'call_type',call_type,'requests',requests,'failures',failures,'prompt_tokens',prompt_tokens,'completion_tokens',completion_tokens,'cached_tokens',cached_tokens,'avg_ms',avg_ms) ORDER BY requests DESC),'[]'::jsonb) FROM (SELECT api_key,model,api_base,call_type,count(*) requests,count(*) filter(where status='failure') failures,sum(prompt_tokens) prompt_tokens,sum(completion_tokens) completion_tokens,sum(cached_tokens) cached_tokens,coalesce(round(avg(duration_ms))::bigint,0) avg_ms FROM base GROUP BY 1,2,3,4) x
        UNION ALL SELECT 'errors',coalesce(jsonb_agg(jsonb_build_object('startTime',"startTime",'api_key',api_key,'model',model,'detail',left(error,160)) ORDER BY "startTime" DESC),'[]'::jsonb) FROM (SELECT * FROM base WHERE status='failure' ORDER BY "startTime" DESC LIMIT 10) x''', start, end, *args[2:])
        data = {row['kind']: json.loads(row['payload']) for row in result}
        return data['total'], data['buckets'], data['rows'], data['errors']
    finally: await conn.close()

async def logs(days, cursor, limit, key_suffix="", model="", filters=None, q="", status="",
               range_from=None, range_to=None):
    """逐请求明细（游标分页；issue #106 增补：节点 api_base / 端点 / 状态 / 搜索 / 自定义时间窗）。"""
    if range_from is not None or range_to is not None:
        wstart = range_from.astimezone(timezone.utc).replace(tzinfo=None) if range_from else datetime(1970,1,1)
        wend = range_to.astimezone(timezone.utc).replace(tzinfo=None) if range_to else datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        wstart, wend = window(days)
    cur = decode_cursor(cursor)
    where, args = ['"startTime">=$1','"startTime"<$2',_VALID_KEY], [wstart,wend]
    if key_suffix:
        args.append(key_suffix)
        where.append(f'right(api_key,4)=${len(args)}')
    if model:
        args.append(model)
        where.append(f"coalesce(nullif(model_group,''),model,'?')=${len(args)}")
    f = filters or {}
    if f.get("api_bases"):
        args.append(list(f["api_bases"]))
        where.append(f"coalesce(api_base,'')=ANY(${len(args)})")
    if f.get("api_keys"):
        args.append(list(f["api_keys"]))
        where.append(f"api_key=ANY(${len(args)})")
    if f.get("call_types"):
        args.append(list(f["call_types"]))
        where.append(f"lower(call_type)=ANY(${len(args)})")
    if status in ("ok", "failure"):
        # 库内取值为 success/failure；"ok" 语义即非失败
        where.append("status='failure'" if status == "failure" else "status<>'failure'")
    if q:
        args.append(f"%{q}%")
        w = f"${len(args)}"
        where.append(f"(request_id ILIKE {w} OR requester_ip_address ILIKE {w} OR model ILIKE {w} OR model_group ILIKE {w} OR coalesce(metadata #>> '{{error_information,error_message}}',metadata->>'error_str','') ILIKE {w})")
    if cur:
        args += list(cur)
        where.append(f'("startTime",request_id)<(${len(args)-1},${len(args)})')
    args.append(limit + 1); conn = await connection()
    try:
        sql = f'''SELECT request_id,"startTime",api_key,coalesce(nullif(model_group,''),model,'?') model,call_type,coalesce(api_base,'') api_base,
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
