#!/bin/sh
# 双用途入口：
#   无参数 = sidecar 模式（compose 用）：up/adopt WG_IFACE + 前台等待并转发 SIGTERM → wg-quick down。
#   带参数 = 工具模式（deploy.sh 引导用）：exec "$@"，如 `docker run … image wg genkey`。
set -e
if [ "$#" -gt 0 ]; then exec "$@"; fi
WG_IFACE=${WG_IFACE:-wg0}
CONF=${WG_CONF:-/etc/wireguard/$WG_IFACE.conf}
[ -f "$CONF" ] || { echo "!! $CONF missing（先跑 deploy.sh 引导）"; exit 1; }
if ip link show "$WG_IFACE" >/dev/null 2>&1; then
  echo "$WG_IFACE 已存在（他处管理），沿用现有接口"
else
  wg-quick up "$CONF"
fi
term() { echo "wg-quick down $WG_IFACE"; wg-quick down "$CONF" 2>/dev/null || true; exit 0; }
trap term TERM INT
while :; do sleep 5 & wait $!; done
