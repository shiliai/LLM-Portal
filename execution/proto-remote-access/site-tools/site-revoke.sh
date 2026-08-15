#!/usr/bin/env bash
# site-revoke：吊销站点（US-P8）——wg 摘 peer（隧道立断、无法重连）
# + LiteLLM 摘除该站点全部 deployment（路由池即刻摘除）+ 状态标记 revoked。
# 用法：site-revoke <站点名>   （在 VPS 上运行）
set -euo pipefail

ONBOARD=${ONBOARDD_URL:-http://127.0.0.1:8100}
# #7 容器化后 /etc/private-llm 不再放 env 文件：回退到源码目录 vps/.env
ENV_FILE=/etc/private-llm/onboardd.env
[ -f "$ENV_FILE" ] || ENV_FILE="$HOME/LLM-Portal/vps/.env"
ADMIN_TOKEN=$(grep -E '^ONBOARD_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

[ $# -eq 1 ] || { sed -n '2,5p' "$0"; exit 1; }
curl -fsS -X POST "$ONBOARD/onboard/admin/revoke" \
  -H "x-admin-token: $ADMIN_TOKEN" -H 'content-type: application/json' \
  -d "{\"site\":\"$1\"}" | python3 -m json.tool
