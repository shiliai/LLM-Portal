#!/usr/bin/env bash
# site-list：站点清单（名称/公钥/WG IP/模型/分组/状态，US-P8）。（在 VPS 上运行）
set -euo pipefail

ONBOARD=${ONBOARDD_URL:-http://127.0.0.1:8100}
ADMIN_TOKEN=$(grep -E '^ONBOARD_ADMIN_TOKEN=' /etc/private-llm/onboardd.env | cut -d= -f2-)

curl -fsS "$ONBOARD/onboard/admin/list" -H "x-admin-token: $ADMIN_TOKEN" \
  | python3 -c '
import json, sys
sites = json.load(sys.stdin)["sites"]
if not sites:
    print("(no sites registered)")
    raise SystemExit
for s in sites:
    row = "{:<16} {:<14} {:<11} models={} groups={} pubkey={}…".format(
        s["name"], s["wg_ip"], s["status"], s["models"], s["groups"], s["pubkey"][:16])
    print(row)'
