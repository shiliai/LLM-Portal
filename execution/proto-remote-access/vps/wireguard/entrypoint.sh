#!/bin/sh
# 双用途入口：
#   无参数 = sidecar 模式（compose 用）：up/adopt wg0 + 前台等待并转发 SIGTERM → wg-quick down。
#   带参数 = 工具模式（deploy.sh 引导用）：exec "$@"，如 `docker run … image wg genkey`。
set -e
if [ "$#" -gt 0 ]; then exec "$@"; fi
CONF=/etc/wireguard/wg0.conf
[ -f "$CONF" ] || { echo "!! $CONF missing（先跑 deploy.sh 引导）"; exit 1; }
if ip link show wg0 >/dev/null 2>&1; then
  echo "wg0 已存在（他处管理），沿用现有接口"
else
  wg-quick up "$CONF"
fi
term() { echo "wg-quick down wg0"; wg-quick down "$CONF" 2>/dev/null || true; exit 0; }
trap term TERM INT
while :; do sleep 5 & wait $!; done
