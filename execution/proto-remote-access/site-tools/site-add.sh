#!/usr/bin/env bash
# site-add：签发新站点的一次性接入命令（US-P7）。
# 用法：site-add <站点名> --model <对外名>:<端口>[:<上游模型名>] ... [--group <分组>] ...
#   对外名缺省等于上游模型名；分组缺省 default（可多个，provider↔分组多对多，US-P13）。
# 在 VPS 上运行；输出一行命令，拷到站点机器执行。
set -euo pipefail

ONBOARD=${ONBOARDD_URL:-http://127.0.0.1:8100}
# #7 容器化后 /etc/private-llm 不再放 env 文件：回退到源码目录 vps/.env
ENV_FILE=/etc/private-llm/onboardd.env
[ -f "$ENV_FILE" ] || ENV_FILE="$HOME/LLM-Portal/execution/proto-remote-access/vps/.env"
# 兼容旧布局（历史部署目录名）
[ -f "$ENV_FILE" ] || ENV_FILE="$HOME/LLM-Portal/vps/.env"
ADMIN_TOKEN=$(grep -E '^ONBOARD_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

SITE=""
MODELS=()
SITE_GROUPS=()   # 勿名 GROUPS：zsh 的特殊变量（组 ID 数组），会污染脚本
while [ $# -gt 0 ]; do
  case "$1" in
    --model)  MODELS+=("$2"); shift 2 ;;
    --group)  SITE_GROUPS+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) SITE="$1"; shift ;;
  esac
done
[ -n "$SITE" ] && [ ${#MODELS[@]} -gt 0 ] || { sed -n '2,6p' "$0"; exit 1; }

# --model name:port[:upstream] → JSON
MODELS_JSON=$(printf '%s\n' "${MODELS[@]}" | python3 -c '
import json, sys
models = []
for line in sys.stdin:
    parts = line.strip().split(":")
    if len(parts) < 2: sys.exit("bad --model, expect name:port[:upstream]")
    m = {"name": parts[0], "port": int(parts[1])}
    if len(parts) > 2: m["upstream_model"] = parts[2]
    models.append(m)
print(json.dumps(models))')
GROUPS_JSON=$(printf '%s\n' ${SITE_GROUPS[@]:-default} | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')

RESP=$(curl -fsS -X POST "$ONBOARD/onboard/admin/tokens" \
  -H "x-admin-token: $ADMIN_TOKEN" -H 'content-type: application/json' \
  -d "{\"site\":\"$SITE\",\"models\":$MODELS_JSON,\"groups\":$GROUPS_JSON}")
echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("expires_in:", d["expires_in"]); print(d["install_command"])'
