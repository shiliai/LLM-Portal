#!/usr/bin/env bash
# issue #149 验收探针：litellm 流式转发必须保留 usage 缓存明细（prompt_tokens_details.cached_tokens）。
# 背景：v1.96.2 在流式 chunk 转换层剥离缓存字段并重算 token（上游发什么都丢）；
#       v1.103.2 起完整透传。本探针对真实 litellm 发同前缀流式请求，检查最终 usage chunk。
# 用法（网关机上）:
#   ./probe_stream_cache_usage.sh --key sk-xxx --model openai/qwen3.8-27b-mtp2 [--base http://127.0.0.1:4000] [--rounds 2] [--db]
#   --db    追加检查最新 SpendLogs 行的 usage_object 是否含缓存明细（须在 vps/ 目录、可 docker compose exec postgres）
# 退出码: 0=通过（至少一轮最终 chunk 含缓存明细） 1=失败 2=用法错误
set -euo pipefail

BASE=http://127.0.0.1:4000
KEY=${LITELLM_MASTER_KEY:-}
MODEL=
ROUNDS=2
CHECK_DB=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE=$2; shift 2;;
    --key) KEY=$2; shift 2;;
    --model) MODEL=$2; shift 2;;
    --rounds) ROUNDS=$2; shift 2;;
    --db) CHECK_DB=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$KEY" ] || { echo "--key (或环境变量 LITELLM_MASTER_KEY) 必填" >&2; exit 2; }
[ -n "$MODEL" ] || { echo "--model 必填（选一个带前缀缓存的上游模型，如 workstation Strata）" >&2; exit 2; }

# 固定长前缀（~2k tokens），第二轮起应命中 prompt cache；探针只验证「字段是否被剥离」，
# cached_tokens>0 是缓存命中的加分项，非通过条件（冷缓存/上游不支持时可为 0）。
PREFIX=$(python3 -c 'print(" ".join("Fact %d: the quick brown fox jumps over the lazy dog number %d." % (i, i) for i in range(160)))')

PARSE_PROG='
import json, sys
usage = None
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line.startswith("data:"):
        continue
    payload = line[5:].strip()
    if payload == "[DONE]":
        continue
    try:
        chunk = json.loads(payload)
    except ValueError:
        continue
    if isinstance(chunk.get("usage"), dict):
        usage = chunk["usage"]
if usage is None:
    print("MISSING_USAGE")
else:
    ptd = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    vals = [v for v in (ptd, usage.get("cache_read_input_tokens"), usage.get("prompt_cache_hit_tokens")) if isinstance(v, int)]
    if not vals:
        print("STRIPPED")
    else:
        print("OK cached=%d prompt=%s" % (max(vals), usage.get("prompt_tokens")))
'

PASS=0
for r in $(seq 1 "$ROUNDS"); do
  BODY=$(python3 -c 'import json,sys;print(json.dumps({"model":sys.argv[1],"messages":[{"role":"system","content":sys.argv[2]},{"role":"user","content":"Reply with the single word: ok"}],"stream":True,"stream_options":{"include_usage":True},"max_tokens":8}))' "$MODEL" "$PREFIX")
  RAW=$(curl -sS -m 60 -N "$BASE/v1/chat/completions" \
    -H "authorization: Bearer $KEY" -H 'content-type: application/json' -d "$BODY")
  RESULT=$(printf '%s' "$RAW" | python3 -c "$PARSE_PROG")
  echo "round $r: $RESULT"
  case "$RESULT" in OK*) PASS=$((PASS+1));; esac
done

if [ "$CHECK_DB" = 1 ]; then
  echo "-- SpendLogs 最新行 usage_object（缓存明细应落库）:"
  docker compose exec -T postgres psql -U litellm -d litellm -t -A -c \
    "select metadata->'usage_object' from \"LiteLLM_SpendLogs\" order by endTime desc limit 1"
fi

[ "$PASS" -gt 0 ] && { echo "PASS: 流式 usage 缓存明细未被剥离（$PASS/$ROUNDS 轮）"; exit 0; }
echo "FAIL: 所有轮次最终 chunk 均无缓存明细（字段被剥离或上游未返回）"
exit 1
