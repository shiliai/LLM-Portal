#!/usr/bin/env bash
# private-llm 网关一键部署（VPS 侧，docker 组用户执行，日常无需 sudo；幂等可重复跑）。
# #7 容器化：7 服务全 compose——litellm/compat/postgres/mcp-hub/onboardd/console + wireguard sidecar
#   （host 网络，wg0 仍在宿主机 netns，站点路由模型不变）；onboardd/consoled 经 docker.sock
#   管理 wg peer / 重启 mcp-hub（挂 sock 的容器 ≈ 宿主机 root，仅管理面容器，见 runbook §7）。
# 步骤：[一次性迁移退役 systemd] → 引导 wg 密钥与配置（docker 执行，免 sudo）→ compose build/up
#       → LE 证书（首次）→ nginx server 块注入既有 nginx-sub2api（备份 + 回滚）→ 冒烟 + 收敛自检。
# 宿主机一次性前置（需 sudo，见 runbook §2）：ufw allow 51820/udp（+ 云安全组 443/tcp、51820/udp）、
#   BBR sysctl（/etc/sysctl.d/99-private-llm-tunnel.conf）。
set -euo pipefail

cd "$(dirname "$0")"
docker info >/dev/null 2>&1 || { echo "docker 不可用（用户需在 docker 组）"; exit 1; }
[ -f .env ] || { echo "missing .env (copy from .env.example)"; exit 1; }
set -a; . ./.env; set +a

DOMAIN=${DOMAIN:?DOMAIN}
WG_PORT=${WG_PORT:-51820}
WG_VPS_IP=${WG_VPS_IP:-10.77.0.1}
STATE_DIR=/var/lib/private-llm
ETC_DIR=/etc/private-llm

echo "== [1/7] 一次性迁移：退役宿主机 systemd 部署（容器接管；仅首次需要 sudo）"
for unit in console mcp-hub onboardd; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    if sudo -n systemctl disable --now "$unit" 2>/dev/null; then
      echo "   $unit.service 已停用"
    else
      echo "   !! 请手动执行：sudo systemctl disable --now $unit"
      echo "      （不执行会与容器的 127.0.0.1 端口发布冲突，导致容器启动失败）"
    fi
  fi
done
if systemctl is-active --quiet wg-quick@wg0 2>/dev/null; then
  if sudo -n systemctl disable --now wg-quick@wg0 2>/dev/null; then
    echo "   wg-quick@wg0 已停用（隧道短暂中断，直至步骤 3 容器拉起）"
  else
    echo "   !! wg0 仍由 systemd 管理，容器无法接管："
    echo "      sudo systemctl disable --now wg-quick@wg0 后重跑本脚本"
    exit 1
  fi
fi

echo "== [2/7] 引导 WireGuard 密钥/配置与状态目录（docker 执行，宿主机免 sudo；已存在则跳过）"
docker compose build wireguard >/dev/null
docker run --rm -v "$STATE_DIR":/state -v /etc/wireguard:/wg \
  -e WG_VPS_IP -e WG_PORT private-llm-wireguard sh -es <<'BOOTSTRAP'
umask 077
[ -f /state/wireguard-private.key ] || wg genkey > /state/wireguard-private.key
wg pubkey < /state/wireguard-private.key > /state/wireguard-public.key
if [ ! -f /wg/wg0.conf ]; then
  # MTU=1280：跨境 UDP 链路对大包丢弃率高（晚高峰 10-25% 丢包），1280 实测吞吐 3-6 倍于默认 1420
  cat > /wg/wg0.conf <<CONF
[Interface]
Address = ${WG_VPS_IP:?}/24
ListenPort = ${WG_PORT:?}
MTU = 1280
PrivateKey = $(cat /state/wireguard-private.key)
CONF
  echo "   已生成 /etc/wireguard/wg0.conf"
fi
BOOTSTRAP
docker run --rm -v "$ETC_DIR":/e private-llm-wireguard \
  sh -c '[ -f /e/external-mcp.json ] || echo "[]" > /e/external-mcp.json'
[ "$(cat /proc/sys/net/ipv4/tcp_congestion_control)" = bbr ] \
  || echo "   note: 宿主机未启 BBR（一次性 sudo，见 runbook §2 前置）——跨境吞吐会显著劣化"

echo "== [3/7] compose 构建 + 启动（6 服务；一个 compose file 管全部，升级 = 重跑本步骤）"
# 探测 nginx-sub2api 所在网络并注入 compose（历史 .env 可能未写 NGINX_SHARED_NETWORK，
# 旧版 deploy.sh 即为动态探测 + 行内传参；此处置于 up 之前，build 不依赖网络）
SHARED_NET=$(docker inspect nginx-sub2api --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -1)
[ -n "$SHARED_NET" ] || { echo "cannot detect nginx-sub2api network"; exit 1; }
export NGINX_SHARED_NETWORK="$SHARED_NET"
docker compose up -d --build
sleep 3
docker compose ps

echo "== [4/7] letsencrypt certificate ($DOMAIN)"
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

echo "== [5/7] nginx site (into nginx-sub2api)"
# 中间产物走 mktemp：/var/lib/private-llm 与历史 /tmp 固定名文件均为旧 root 部署所建，docker 组用户不可写
RENDERED=$(mktemp) && STRIPPED=$(mktemp) && trap 'rm -f "$RENDERED" "$STRIPPED"' EXIT
sed -e "s|{{LITELLM_UPSTREAM}}|litellm:4000|g" \
    -e "s|{{COMPAT_UPSTREAM}}|private-llm-compat:8400|g" \
    nginx/private-llm.conf > "$RENDERED"
cp "$NGINX_CONF_DIR/nginx.conf" "$NGINX_CONF_DIR/nginx.conf.backup-$(date +%Y%m%d-%H%M%S)"
# 幂等：先移除旧块再追加。注意必须保留 inode 原地写（cat > / >>）——此文件被单文件 bind-mount 进
# nginx 容器，sed -i 会换 inode，容器内仍是旧内容、reload 也不生效（2026-08-14 实测踩坑，需重启容器才恢复）
awk '/# BEGIN private-llm/,/# END private-llm/{next}1' "$NGINX_CONF_DIR/nginx.conf" > "$STRIPPED"
cat "$STRIPPED" > "$NGINX_CONF_DIR/nginx.conf"
{ echo "# BEGIN private-llm (managed by private-llm deploy.sh)"; cat "$RENDERED"; echo "# END private-llm"; } >> "$NGINX_CONF_DIR/nginx.conf"
if ! docker exec nginx-sub2api nginx -t 2>/dev/null; then
  echo "!! nginx config test failed; rendering diagnostics:"; docker exec nginx-sub2api nginx -t || true
  # 回滚：删除刚追加的块
  sed -i '/# BEGIN private-llm/,/# END private-llm/d' "$NGINX_CONF_DIR/nginx.conf"
  docker exec nginx-sub2api nginx -t && docker exec nginx-sub2api nginx -s reload && echo "(rolled back, existing sites intact)" || echo "!! manual fix needed in $NGINX_CONF_DIR/nginx.conf"
  exit 1
fi
docker exec nginx-sub2api nginx -s reload

echo "== [6/7] site CLI (site-add / site-revoke / site-list)"
# 用户级安装（免 sudo）；调用 127.0.0.1:8100 回环 onboardd，与容器化部署兼容
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
for tool in site-add site-revoke site-list; do
  cp "../site-tools/$tool.sh" "$BIN_DIR/$tool"
  chmod +x "$BIN_DIR/$tool"
done
case ":$PATH:" in *":$BIN_DIR:"*) : ;; *) echo "   note: $BIN_DIR 不在 PATH，请加入 ~/.profile" ;; esac

echo "== [7/7] smoke"
echo "-- litellm health:"; curl -sf -m 5 http://127.0.0.1:4000/health/liveliness && echo || echo "!! litellm not up (docker compose logs litellm)"
echo "-- compat-proxy:"; code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8400/v1/chat/completions -H 'content-type: application/json' -d '{"model":"x","messages":[]}'); [ "$code" = "401" ] && echo "   ok (no-key 401 passthrough)" || echo "!! expect 401 via compat, got $code (docker compose logs compat)"
echo "-- onboardd:"; curl -s -m 5 -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8100/onboard/install?token=x" | sed 's/^/   (expect 403): /'
echo "-- mcp-hub:"; curl -s -m 5 -o /dev/null -w '%{http_code}\n' -H 'authorization: Bearer invalid' http://127.0.0.1:8200/mcp/usage | sed 's/^/   (expect 401): /'
echo "-- consoled:"; curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8300/console/api/me | sed 's/^/   (expect 401): /'
echo "-- wireguard:"; docker exec private-llm-wireguard wg show wg0 | head -3
echo "== [7b/7] 暴露面收敛检查（r6 allowlist）"
for path in /ui /login /sso /openapi.json /key/generate /onboard/admin/list /spend/logs /team/list; do
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$DOMAIN$path")
  [ "$code" = "404" ] || echo "!! $path 未收敛（$code，应 404）"
done
for path in /v1/models /key/info; do
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$DOMAIN$path")
  [ "$code" = "401" ] || echo "!! $path 应保留但未带 Key 应 401（实际 $code）"
done
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$DOMAIN/console/")
{ [ "$code" = "200" ] || [ "$code" = "302" ] || [ "$code" = "307" ]; } || echo "!! /console/ 应可达（实际 $code）"
echo "   收敛检查完成（无 !! 即全过）"

echo "== done"
echo "next: site-add <name> --model <model>:<port> ...   # 然后把输出的命令拷到站点机器执行"
