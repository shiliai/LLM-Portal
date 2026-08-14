#!/usr/bin/env bash
# private-llm 网关一键部署（VPS 侧，root 执行；幂等可重复跑）。
# 步骤：wireguard wg0 → ufw → LE 证书 → nginx 站点（并入 nginx-sub2api，带备份回滚）
#       → litellm+postgres compose → mcp-hub/onboardd systemd → site-add 等 CLI。
# 设计等价物：设计文档 §9「Caddy」角色由既有 nginx+certbot 承担（本机 443 已被其占用）。
set -euo pipefail

cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -f .env ] || { echo "missing .env (copy from .env.example)"; exit 1; }
set -a; . ./.env; set +a

DOMAIN=${DOMAIN:?DOMAIN}
WG_PORT=${WG_PORT:-51820}
WG_VPS_IP=${WG_VPS_IP:-10.77.0.1}
STATE_DIR=/var/lib/private-llm
ETC_DIR=/etc/private-llm
APP_DIR=/opt/private-llm
mkdir -p "$STATE_DIR" "$ETC_DIR" "$APP_DIR"

echo "== [1/8] wireguard"
apt-get install -y -qq wireguard-tools >/dev/null
if [ ! -f "$STATE_DIR/wireguard-private.key" ]; then
  (umask 077; wg genkey | tee "$STATE_DIR/wireguard-private.key" | wg pubkey > "$STATE_DIR/wireguard-public.key")
fi
if [ ! -f /etc/wireguard/wg0.conf ]; then
  cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = ${WG_VPS_IP}/24
ListenPort = ${WG_PORT}
MTU = 1280
PrivateKey = $(cat "$STATE_DIR/wireguard-private.key")
EOF
  chmod 600 /etc/wireguard/wg0.conf
fi
systemctl enable --now wg-quick@wg0 >/dev/null 2>&1 || systemctl restart wg-quick@wg0
ufw allow ${WG_PORT}/udp comment 'private-llm wireguard' >/dev/null
# 跨境隧道传输调优（实测晚高峰 10-25% 丢包，CUBIC 吞吐坍塌至 ~4KB/s）：
# BBR 对随机丢包鲁棒；wg MTU=1280 避开大 UDP 包高丢弃率（3-6 倍吞吐）
modprobe tcp_bbr 2>/dev/null || true
cat > /etc/sysctl.d/99-private-llm-tunnel.conf <<EOF
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq
net.core.rmem_max = 7500000
net.core.wmem_max = 7500000
EOF
sysctl --system >/dev/null 2>&1 || true

echo "== [2/8] letsencrypt certificate ($DOMAIN)"
NGINX_CONF_DIR=REDACTED-HOME/docker/nginx
CERTBOT_DIR=REDACTED-HOME/docker/certbot
LE_DIR="$CERTBOT_DIR/conf/live/$DOMAIN"
if [ ! -f "$LE_DIR/fullchain.pem" ]; then
  docker run --rm -v "$CERTBOT_DIR/conf:/etc/letsencrypt" -v "$CERTBOT_DIR/www:/var/www/certbot" \
    certbot/certbot certonly --webroot -w /var/www/certbot --cert-name "$DOMAIN" -d "$DOMAIN" \
    --non-interactive --agree-tos --keep-until-expiring
  # 纳入每日续期任务
  LINE="certonly_webroot $DOMAIN -d $DOMAIN"
  grep -qF "$LINE" "$CERTBOT_DIR/renew.sh" || sed -i "s|^log \"Reloading nginx-sub2api|$LINE\nlog \"Reloading nginx-sub2api|" "$CERTBOT_DIR/renew.sh"
fi

echo "== [3/8] nginx site (into nginx-sub2api)"
SHARED_NET=$(docker inspect nginx-sub2api --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -1)
[ -n "$SHARED_NET" ] || { echo "cannot detect nginx-sub2api network"; exit 1; }
HOST_GW=$(docker network inspect "$SHARED_NET" --format '{{(index .IPAM.Config 0).Gateway}}')
SHARED_SUBNET=$(docker network inspect "$SHARED_NET" --format '{{(index .IPAM.Config 0).Subnet}}')
# nginx 容器反代到宿主机上的 onboardd/mcp-hub：仅放行共享网段（公网仍被 INPUT DROP 拦截）
ufw allow from "$SHARED_SUBNET" to any port 8100 proto tcp comment 'private-llm onboardd (docker net only)' >/dev/null
ufw allow from "$SHARED_SUBNET" to any port 8200 proto tcp comment 'private-llm mcp-hub (docker net only)' >/dev/null
sed -e "s|{{HOST_GATEWAY}}|${HOST_GW}|g" -e "s|{{LITELLM_UPSTREAM}}|litellm:4000|g" \
    nginx/private-llm.conf > "$STATE_DIR/nginx-private-llm.rendered.conf"
cp "$NGINX_CONF_DIR/nginx.conf" "$NGINX_CONF_DIR/nginx.conf.backup-$(date +%Y%m%d-%H%M%S)"
# 幂等：先移除旧块再追加
sed -i '/# BEGIN private-llm/,/# END private-llm/d' "$NGINX_CONF_DIR/nginx.conf"
{ echo "# BEGIN private-llm (managed by private-llm deploy.sh)"; cat "$STATE_DIR/nginx-private-llm.rendered.conf"; echo "# END private-llm"; } >> "$NGINX_CONF_DIR/nginx.conf"
if ! docker exec nginx-sub2api nginx -t 2>/dev/null; then
  echo "!! nginx config test failed; rendering diagnostics:"; docker exec nginx-sub2api nginx -t || true
  # 回滚：删除刚追加的块
  sed -i '/# BEGIN private-llm/,/# END private-llm/d' "$NGINX_CONF_DIR/nginx.conf"
  docker exec nginx-sub2api nginx -t && docker exec nginx-sub2api nginx -s reload && echo "(rolled back, existing sites intact)" || echo "!! manual fix needed in $NGINX_CONF_DIR/nginx.conf"
  exit 1
fi
docker exec nginx-sub2api nginx -s reload

echo "== [4/8] litellm + postgres (compose, shared network: $SHARED_NET)"
NGINX_SHARED_NETWORK="$SHARED_NET" docker compose up -d
sleep 5
docker compose ps

echo "== [5/8] systemd services (mcp-hub, onboardd)"
apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
for svc in mcp-hub onboardd; do
  [ -d "$APP_DIR/$svc" ] || mkdir -p "$APP_DIR/$svc"
  cp -r "../$svc/." "$APP_DIR/$svc/"
  python3 -m venv "$APP_DIR/venvs/$svc"
  "$APP_DIR/venvs/$svc/bin/pip" install -q -r "$APP_DIR/$svc/requirements.txt"
  cp "../$svc/$svc.service" /etc/systemd/system/
done
# 服务环境文件（含密钥，0600，不入库）
UMASK_OLD=$(umask); umask 077
cat > "$ETC_DIR/onboardd.env" <<EOF
DOMAIN=$DOMAIN
LITELLM_MASTER_KEY=$LITELLM_MASTER_KEY
ONBOARD_ADMIN_TOKEN=$ONBOARD_ADMIN_TOKEN
EOF
cat > "$ETC_DIR/mcp-hub.env" <<EOF
PUBLIC_BASE=https://$DOMAIN
MCP_VISION_MODEL=${MCP_VISION_MODEL:-qwen3.6-35b-fp8}
EOF
umask "$UMASK_OLD"
systemctl daemon-reload
systemctl enable --now mcp-hub onboardd
systemctl restart mcp-hub onboardd

echo "== [6/8] admin CLI (site-add / site-revoke / site-list)"
for tool in site-add site-revoke site-list; do
  cp "../site-tools/$tool.sh" "/usr/local/bin/$tool"
  chmod +x "/usr/local/bin/$tool"
done
[ -f "$ETC_DIR/external-mcp.json" ] || echo '[]' > "$ETC_DIR/external-mcp.json"

echo "== [7/8] smoke"
echo "-- litellm health:"; curl -sf -m 5 http://127.0.0.1:4000/health/liveliness && echo || echo "!! litellm not up (docker compose logs litellm)"
echo "-- onboardd:"; curl -sf -m 5 -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8100/onboard/install?token=x" | sed 's/^/   (expect 403): /'
echo "-- mcp-hub:"; curl -sf -m 5 -o /dev/null -w '%{http_code}\n' -H 'authorization: Bearer invalid' http://127.0.0.1:8200/mcp/usage | sed 's/^/   (expect 401): /'
echo "-- https entrypoint:"; curl -sf -m 10 -o /dev/null -w '%{http_code}\n' "https://$DOMAIN/health/liveliness" | sed 's/^/   (expect 200): /'

echo "== [8/8] done"
echo "next: site-add <name> --model <model>:<port> ...   # 然后把输出的命令拷到站点机器执行"
