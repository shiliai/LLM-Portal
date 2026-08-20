#!/usr/bin/env bash
# private-llm 网关一键部署（VPS 侧，docker 组用户执行，日常无需 sudo；幂等可重复跑）。
# #7 容器化：7 服务全 compose——litellm/compat/postgres/mcp-hub/onboardd/console + wireguard sidecar
#   （host 网络，wg0 仍在宿主机 netns，站点路由模型不变）；onboardd/consoled 经 docker.sock
#   管理 wg peer / 重启 mcp-hub（挂 sock 的容器 ≈ 宿主机 root，仅管理面容器，见 runbook §7）。
# 对外入口 EDGE_MODE（.env）：standalone（默认）= 本栈 edge-nginx/edge-certbot 发布 80/443，
#   全新 VPS 零既有依赖；external = 注入既有 nginx 容器（需 EDGE_NGINX_CONTAINER /
#   NGINX_CONF_DIR / CERTBOT_DIR），流程同旧版；offload = TLS 由上游设备（防火墙反代等）
#   终结，本栈仅起 HTTP-only edge-nginx（nginx/private-llm-offload.conf），无证书环节，
#   外部 URL 通常带高位端口（.env 的 PUBLIC_BASE 须同步）。
# 步骤：[一次性迁移退役 systemd] → 引导 wg 密钥与配置（docker 执行，免 sudo）→ compose build/up
#       → LE 证书 → nginx 站点（渲染 nginx/private-llm.conf）→ 冒烟 + 收敛自检。
# 宿主机一次性前置（需 sudo，见 runbook §2）：放行 80/443/tcp、51820/udp（防火墙 + 云安全组）、
#   BBR sysctl（/etc/sysctl.d/99-private-llm-tunnel.conf）。
set -euo pipefail

cd "$(dirname "$0")"
source ./deploy-lib.sh
docker info >/dev/null 2>&1 || { echo "docker 不可用（用户需在 docker 组）"; exit 1; }
[ -f .env ] || { echo "missing .env (copy from .env.example)"; exit 1; }
set -a; . ./.env; set +a

DOMAIN=${DOMAIN:?DOMAIN}
WG_PORT=${WG_PORT:-51820}
WG_VPS_IP=${WG_VPS_IP:-10.77.0.1}
# WG_SUBNET（.env，如 10.78.0.0/24）→ 派生前缀传给 onboardd（站点 AllowedIPs/自检 ping；
# 换独立网段可让站点与另一套 10.77.0.0/24 网关双隧道并存）。
# 校验 /24 形状：onboardd 硬性假设三段前缀（拼 .0/24、站点 IP 从 .11 递增）
WG_SUBNET=${WG_SUBNET:-10.77.0.0/24}
if ! [[ "$WG_SUBNET" =~ ^([0-9]{1,3}\.){3}0/24$ ]]; then
  echo "WG_SUBNET 非法（expect x.y.z.0/24，实为 '$WG_SUBNET'——onboardd 按 /24 前缀语义生成站点配置）"; exit 1
fi
export WG_SUBNET_PREFIX="${WG_SUBNET%.*}"
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
# -i 必需：heredoc 走 stdin，缺 -i 时 sh -s 立即 EOF、bootstrap 体一行都不执行
#（exit 0 的静默失败；VPS 迁移自 systemd 时密钥已存在故从未暴露）
docker run --rm -i -v "$STATE_DIR":/state -v /etc/wireguard:/wg \
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
echo "-- external MCP registry backup gate"
MCP_REGISTRY_RECEIPT=$(docker run --rm -v "$ETC_DIR":/e alpine sh -ec '
  umask 077
  registry=/e/external-mcp.json
  mkdir -p /e/mcp-registry-backups
  if [ ! -e "$registry" ]; then
    printf "[]\n" > "$registry"
    chmod 600 "$registry"
    sync -f "$registry"; sync -f /e
    printf "created path=%s sha256=%s mode=%s inode=%s attestation=backup_verified\n" "$registry" "$(sha256sum "$registry" | awk "{print \$1}")" "$(stat -c %a "$registry")" "$(stat -c %i "$registry")"
    exit 0
  fi
  [ -f "$registry" ] && [ ! -L "$registry" ] || { echo "registry must be a regular non-symlink file" >&2; exit 1; }
  chmod 600 "$registry"
  stamp=$(date +%Y%m%d-%H%M%S)
  backup=/e/mcp-registry-backups/external-mcp.json.$stamp.bak
  cat "$registry" > "$backup"
  chmod 600 "$backup"
  [ "$(sha256sum "$registry" | awk "{print \$1}")" = "$(sha256sum "$backup" | awk "{print \$1}")" ] || { echo "backup SHA mismatch" >&2; exit 1; }
  sync -f "$backup"; sync -f "$registry"; sync -f /e
  printf "backup path=%s sha256=%s mode=%s owner=%s inode=%s attestation=backup_verified\n" "$backup" "$(sha256sum "$backup" | awk "{print \$1}")" "$(stat -c %a "$registry")" "$(stat -c %u:%g "$registry")" "$(stat -c %i "$registry")"
') || { echo "!! external MCP registry backup verification failed; aborting before deployment"; exit 1; }
echo "   $MCP_REGISTRY_RECEIPT"
[ "$(cat /proc/sys/net/ipv4/tcp_congestion_control)" = bbr ] \
  || echo "   note: 宿主机未启 BBR（一次性 sudo，见 runbook §2 前置）——跨境吞吐会显著劣化"

echo "== [3/7] compose 构建 + 启动（核心服务；一个 compose file 管全部，升级 = 重跑本步骤）"
EDGE_MODE=${EDGE_MODE:-standalone}           # standalone=自带 edge-nginx/certbot；external=注入既有 nginx 容器；offload=上游终结 TLS 的 HTTP-only 边缘
EDGE_NGINX_CONTAINER=${EDGE_NGINX_CONTAINER:-}
if [ "$EDGE_MODE" = external ]; then
  # 探测既有 nginx 容器所在网络并注入 compose（历史 .env 可能未写 NGINX_SHARED_NETWORK）
  [ -n "$EDGE_NGINX_CONTAINER" ] || { echo "EDGE_MODE=external 需在 .env 设 EDGE_NGINX_CONTAINER（既有 nginx 容器名）"; exit 1; }
  SHARED_NET=$(docker inspect "$EDGE_NGINX_CONTAINER" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -1)
  [ -n "$SHARED_NET" ] || { echo "cannot detect $EDGE_NGINX_CONTAINER network"; exit 1; }
  export NGINX_SHARED_NETWORK="$SHARED_NET"
  COMPOSE_PROFILES=""
else
  # standalone/offload：入口网络由本脚本创建；80 由 edge-nginx 发布，不依赖任何既有布局。
  # standalone 另加 tls profile 起 edge-certbot；offload 无证书环节（上游终结 TLS）
  SHARED_NET=${NGINX_SHARED_NETWORK:-private-llm-edge}
  docker network inspect "$SHARED_NET" >/dev/null 2>&1 || docker network create "$SHARED_NET"
  export NGINX_SHARED_NETWORK="$SHARED_NET"
  COMPOSE_PROFILES="--profile edge"
  [ "$EDGE_MODE" = standalone ] && COMPOSE_PROFILES="$COMPOSE_PROFILES --profile tls"
fi
# --profile 是 compose 全局 flag，须置于子命令前（up 之后挂 --profile 在部分版本报 unknown flag）
docker compose $COMPOSE_PROFILES up -d --build
sleep 3
docker compose ps

echo "== [4/7] edge certificate/site ($DOMAIN)"
EDGE_DIR=$STATE_DIR/edge
if [ "$EDGE_MODE" = external ]; then
  : "${NGINX_CONF_DIR:?EDGE_MODE=external 需在 .env 设 NGINX_CONF_DIR（既有 nginx 容器配置目录）}"
  : "${CERTBOT_DIR:?EDGE_MODE=external 需在 .env 设 CERTBOT_DIR（既有 certbot webroot/letsencrypt 目录）}"
  if [ ! -f "$CERTBOT_DIR/conf/live/$DOMAIN/fullchain.pem" ]; then
    docker run --rm -v "$CERTBOT_DIR/conf:/etc/letsencrypt" -v "$CERTBOT_DIR/www:/var/www/certbot" \
      certbot/certbot:v5.7.0 certonly --webroot -w /var/www/certbot --cert-name "$DOMAIN" -d "$DOMAIN" \
      --non-interactive --agree-tos --keep-until-expiring
    # 纳入既有续期任务（若宿主机 renew.sh 存在）
    if [ -f "$CERTBOT_DIR/renew.sh" ]; then
      LINE="certonly_webroot $DOMAIN -d $DOMAIN"
      grep -qF "$LINE" "$CERTBOT_DIR/renew.sh" || sed -i "s|^log \"Reloading $EDGE_NGINX_CONTAINER|$LINE\nlog \"Reloading $EDGE_NGINX_CONTAINER|" "$CERTBOT_DIR/renew.sh"
    fi
  fi
elif [ "$EDGE_MODE" = offload ]; then
  # offload：无证书环节（上游设备终结 TLS），仅渲染 HTTP-only 边缘并发布 80。
  # letsencrypt/www 目录仍建（edge-nginx 挂载点存在即可，内容为空）；nginx 树 chown 给
  # 执行用户——deploy.sh 以 docker 组用户写 conf，root 建的目录会 Permission denied
  docker run --rm -v "$EDGE_DIR":/edge alpine \
    sh -c 'mkdir -p /edge/nginx/conf.d /edge/letsencrypt /edge/www && chown -R '"$(id -u):$(id -g)"' /edge/nginx'
  sed -e "s|{{LITELLM_UPSTREAM}}|litellm:4000|g" -e "s|{{COMPAT_UPSTREAM}}|private-llm-compat:8400|g" \
      nginx/private-llm-offload.conf > "$EDGE_DIR/nginx/conf.d/private-llm.conf"
  docker compose --profile edge up -d edge-nginx
  docker exec private-llm-edge-nginx nginx -t
  docker exec private-llm-edge-nginx nginx -s reload
else
  # standalone：状态目录经 bootstrap 容器建（/var/lib 直建需 sudo）；首签走 80 端口 bootstrap 配置
  docker run --rm -v "$EDGE_DIR":/edge alpine \
    sh -c 'mkdir -p /edge/nginx/conf.d /edge/letsencrypt /edge/www && chown -R '"$(id -u):$(id -g)"' /edge/nginx'
  render_nginx() { sed -e "s|{{LITELLM_UPSTREAM}}|litellm:4000|g" -e "s|{{COMPAT_UPSTREAM}}|private-llm-compat:8400|g" \
      -e "s|{{DOMAIN}}|$DOMAIN|g" nginx/private-llm.conf; }
  if [ ! -f "$EDGE_DIR/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    render_nginx | sed -e '/listen 443 ssl/,$d' > "$EDGE_DIR/nginx/conf.d/private-llm.conf"   # 首签：仅 80 的 ACME+跳转
    docker compose --profile edge --profile tls up -d edge-nginx
    sleep 2
    docker compose run --rm --entrypoint certbot edge-certbot certonly --webroot -w /var/www/certbot \
      --cert-name "$DOMAIN" -d "$DOMAIN" --non-interactive --agree-tos --keep-until-expiring
  fi
  render_nginx > "$EDGE_DIR/nginx/conf.d/private-llm.conf"
  docker compose --profile edge --profile tls up -d edge-nginx
  docker exec private-llm-edge-nginx nginx -t
  docker exec private-llm-edge-nginx nginx -s reload
fi

echo "== [5/7] nginx site"
# 中间产物走 mktemp：/var/lib/private-llm 与历史 /tmp 固定名文件均为旧 root 部署所建，docker 组用户不可写
RENDERED=$(mktemp) && STRIPPED=$(mktemp) && trap 'rm -f "$RENDERED" "$STRIPPED"' EXIT
EDGE_TEMPLATE=nginx/private-llm.conf
[ "$EDGE_MODE" = offload ] && EDGE_TEMPLATE=nginx/private-llm-offload.conf
sed -e "s|{{LITELLM_UPSTREAM}}|litellm:4000|g" \
    -e "s|{{COMPAT_UPSTREAM}}|private-llm-compat:8400|g" \
    -e "s|{{DOMAIN}}|$DOMAIN|g" \
    "$EDGE_TEMPLATE" > "$RENDERED"
if [ "$EDGE_MODE" = external ]; then
  # 幂等：先移除旧块再追加。注意必须保留 inode 原地写（cat > / >>）——此文件被单文件 bind-mount 进
  # nginx 容器，sed -i 会换 inode，容器内仍是旧内容、reload 也不生效（2026-08-14 实测踩坑，需重启容器才恢复）
  NGINX_BACKUP="$NGINX_CONF_DIR/nginx.conf.backup-$(date +%Y%m%d-%H%M%S)"
  cp "$NGINX_CONF_DIR/nginx.conf" "$NGINX_BACKUP"
  awk '/# BEGIN private-llm/,/# END private-llm/{next}1' "$NGINX_CONF_DIR/nginx.conf" > "$STRIPPED"
  cat "$STRIPPED" > "$NGINX_CONF_DIR/nginx.conf"
  { echo "# BEGIN private-llm (managed by private-llm deploy.sh)"; cat "$RENDERED"; echo "# END private-llm"; } >> "$NGINX_CONF_DIR/nginx.conf"
  if ! docker exec "$EDGE_NGINX_CONTAINER" nginx -t 2>/dev/null; then
    echo "!! nginx config test failed; rendering diagnostics:"; docker exec "$EDGE_NGINX_CONTAINER" nginx -t || true
    # 回滚：原位恢复旧字节，保留单文件 bind mount 的 inode。
    restore_file_same_inode "$NGINX_BACKUP" "$NGINX_CONF_DIR/nginx.conf" || {
      echo "!! nginx backup restore failed: $NGINX_BACKUP"; exit 1;
    }
    if docker exec "$EDGE_NGINX_CONTAINER" nginx -t \
        && docker exec "$EDGE_NGINX_CONTAINER" nginx -s reload; then
      echo "(rolled back, existing sites intact)"
    else
      echo "!! restored nginx config failed validation/reload; manual recovery required"
    fi
    exit 1
  fi
  docker exec "$EDGE_NGINX_CONTAINER" nginx -s reload
else
  # standalone/offload 已在 [4/7] 渲染并 reload，此处仅校验
  diff -q "$RENDERED" "$EDGE_DIR/nginx/conf.d/private-llm.conf" >/dev/null || echo "   note: conf 与 [4/7] 渲染不一致（刚渲染过即无碍）"
  docker exec private-llm-edge-nginx nginx -t || { echo "!! edge-nginx 配置校验失败（$EDGE_DIR/nginx/conf.d/private-llm.conf）"; exit 1; }
fi

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
SMOKE_FAILURES=0
fail_smoke() { echo "!! $*"; SMOKE_FAILURES=$((SMOKE_FAILURES + 1)); }
http_code() { curl -sS -m 10 -o /dev/null -w '%{http_code}' "$@" || true; }
echo "-- wait for core services"
for _ in $(seq 1 30); do
  [ "$(http_code http://127.0.0.1:4000/health/liveliness)" = "200" ] \
    && [ "$(http_code http://127.0.0.1:8300/console/api/me)" = "401" ] && break
  sleep 2
done
echo "-- litellm health:"; [ "$(http_code http://127.0.0.1:4000/health/liveliness)" = "200" ] || fail_smoke "litellm health failed"
echo "-- compat-proxy:"; code=$(http_code -X POST http://127.0.0.1:8400/v1/chat/completions -H 'content-type: application/json' -d '{"model":"x","messages":[]}'); [ "$code" = "401" ] || fail_smoke "expect compat 401, got $code"
echo "-- onboardd:"; code=$(http_code "http://127.0.0.1:8100/onboard/install?token=x"); [ "$code" = "403" ] || fail_smoke "expect onboardd 403, got $code"
echo "-- mcp-hub:"; code=$(http_code -H 'authorization: Bearer invalid' http://127.0.0.1:8200/mcp/usage); [ "$code" = "401" ] || fail_smoke "expect mcp-hub 401, got $code"
echo "-- consoled:"; code=$(http_code http://127.0.0.1:8300/console/api/me); [ "$code" = "401" ] || fail_smoke "expect console 401, got $code"
echo "-- wireguard:"; docker exec private-llm-wireguard wg show wg0 | head -3 || fail_smoke "wireguard unavailable"
for container in litellm private-llm-compat private-llm-postgres private-llm-mcp-hub private-llm-onboardd private-llm-console private-llm-wireguard; do
  [ "$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)" = "running" ] \
    || fail_smoke "$container is not running"
done
echo "== [7b/7] 暴露面收敛检查（r6 allowlist）"
# offload 无本机 https 入口（TLS 在上游设备），经 80 端口本地校验同一路径分发
CHECK_BASE="https://$DOMAIN"
[ "$EDGE_MODE" = offload ] && CHECK_BASE="http://127.0.0.1"
for path in /ui /login /sso /openapi.json /key/generate /onboard/admin/list /spend/logs /team/list; do
  code=$(http_code "$CHECK_BASE$path")
  [ "$code" = "404" ] || fail_smoke "$path 未收敛（$code，应 404）"
done
for path in /v1/models /key/info; do
  code=$(http_code "$CHECK_BASE$path")
  [ "$code" = "401" ] || fail_smoke "$path 应保留但未带 Key 应 401（实际 $code）"
done
code=$(http_code "$CHECK_BASE/console/")
{ [ "$code" = "200" ] || [ "$code" = "302" ] || [ "$code" = "307" ]; } || fail_smoke "/console/ 应可达（实际 $code）"
[ "$SMOKE_FAILURES" -eq 0 ] || { echo "deployment smoke failed: $SMOKE_FAILURES check(s)"; exit 1; }
echo "   收敛检查完成"

echo "== done"
echo "next: site-add <name> --model <model>:<port> ...   # 然后把输出的命令拷到站点机器执行"
