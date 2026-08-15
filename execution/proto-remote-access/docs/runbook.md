# Runbook：private-llm 网关部署与运维

> 交付物：`execution/proto-remote-access/`（设计文档 §10）
> 设计：`docs/superpowers/specs/2026-08-14-remote-model-access-prototype-design.md`（proto-r6）

## 1. 拓扑与端口

| 组件 | 位置 | 端口 | 说明 |
|---|---|---|---|
| nginx（既有 `nginx-sub2api` 容器） | 公网 | 443/tcp | 单入口（r6 allowlist）：`/`→主页、`/v1/*`+`= /key/info`+`= /health/liveliness`→litellm、`/mcp*`→mcp-hub:8200、`/onboard/install\|register\|confirm`→onboardd:8100、`/console`→consoled:8300；**其余一律 404**（LiteLLM `/ui`/`/login`/全部管理 API、`/onboard/admin/*` 不对公网）；SSE 不缓冲；本机仍可直查 127.0.0.1:4000 |
| LiteLLM Proxy（容器） | 回环 | 127.0.0.1:4000 | 双协议 API（`/v1/chat/completions` + `/v1/messages`）、别名、least-busy 分流、虚拟 Key、用量记账——r6 起退居底层引擎，管理面经控制台 |
| Postgres（容器） | compose 内 | 无主机端口 | LiteLLM Key/用量/deployment 存储（`STORE_MODEL_IN_DB=True`） |
| mcp-hub（容器 `private-llm-mcp-hub`） | 本机回环 | 127.0.0.1:8200 | `/mcp`（Streamable HTTP）+ `/mcp/upload` + `/mcp/files/*` + `/mcp/usage` + 主页托管；nginx 经共享网络容器名反代（#7） |
| onboardd（容器 `private-llm-onboardd`） | 本机回环 | 127.0.0.1:8100 | 站点注册：install/register/confirm + admin API（admin/* 公网 404；site-tools CLI 走本机回环）；经 docker.sock 管 wg peer（#7） |
| **consoled（容器 `private-llm-console`）** | 本机回环 | 127.0.0.1:8300 | **管理控制台后端（r6）**：`/console/` 页 + `/console/api/*`；会话双角色（管理员=邮箱登录 9 页 / 用户 Key=仅我的用量）；聚合 LiteLLM 管理 API（容器名回环）+ onboardd + wg show（经 docker.sock）+ mcp-hub 状态 |
| WireGuard wg0（wireguard sidecar 容器，host 网络 + NET_ADMIN） | 公网 | 51820/udp | wg0 仍留在**宿主机**网络命名空间（10.77.0.1/24，站点从 .11 递增；litellm 容器→站点路由模型不变）；容器起=wg-quick up、停=down；peer 增删 = onboardd/consoled 经 docker.sock `docker exec … wg set`，持久化 `/etc/wireguard/wg0.conf` |

与设计的差异（环境适配，其余逐字落地）：
1. **Caddy → 既有 nginx + certbot**：VPS 443 已由 nginx-sub2api 服务多域名，追加 server block 而非另起入口；SSE 用 `proxy_buffering off` 等价 Caddy `flush_interval -1`。
2. **qwen 实际模型名 `qwen3.6-35b-fp8`**（llama.cpp 实报）；基线口径名 `qwen3.6-35b-a3` 作别名并存。
3. **US-P13 分组 tag 语义按 LiteLLM 1.96.2 实测校准**：① `enable_tag_filtering` 路由器级配置实测未生效，钩子改为每请求强制注入 `enable_tag_filtering=True`；② 带 `default` tag 的 deployment 会被实现当作「tag 无匹配时的兜底池」，与基线「组内无部署→报错」冲突——deployment 一律**不打 default tag**（default 组 = 隐式全量池），绑组 Key 由钩子注入组 tag、未绑组 Key 由钩子清空 tags（顺带清除客户端伪造的 `x-litellm-tags`）。Key 的分组仍存 `metadata.group`（default 视同未绑）。
4. **wstunnel 过渡通道已移除**：部署当日因云安全组未放行 51820/udp 临时用 wstunnel（UDP-over-WS 走 443）打通，后被腾讯云主机安全标记为 Risktool（Linux.Risktool.Wstunell.Agow），按安全策略双端移除（服务/二进制/nginx 路径/uffw 规则全部清除），恢复设计原方案的直连 WG UDP。
5. **隧道传输调优（2026-08-14 晚，TFT 优化，issue #6）**：跨境 wg 隧道晚高峰实测 10~43% 丢包，内层 CUBIC 把随机丢包当拥塞，40KB 请求要 8-12s、400KB 要 80-97s（等效 ~4KB/s）。修复 = **四端 BBR**（VPS 宿主、gb10 宿主、litellm 容器 netns 经 compose `sysctls`、客户端工作站）+ **wg MTU 1280**（两端 wg0.conf 持久化；大 UDP 包丢弃率高，吞吐 3-6 倍于默认 1420）+ TCP 缓冲调大（容器 `tcp_rmem/wmem` 16MB，宿主 `rmem/wmem_max` 7.5MB）。已落入 deploy.sh / install.sh 模板 / wg0.conf 模板。
   **效果（当晚 23:45 实测，隧道丢包 43% 的最差窗口）**：隧道 100KB ~20s→1.06s（最优）；短请求 keep-alive 开销 0.58s ≈ 2 RTT + LiteLLM ~0.2s（达标）；8K-token（79KB）热请求增量 **+1.11s**；32K-token（318KB）增量 **+2.09s**（低于 3s 物理约束判定线，高于 0.7s 理想线）。**结论**：网关自身开销已达「≈1-2 RTT」目标；大 prompt 残余增量由跨境链路丢包/带宽决定（错峰显著更好）。基准脚本固化 `docs/bench-gateway.py`（部署机 `~/private-llm-bench/bench.py` 同源）。注意：`tcp_congestion_control` 为 per-netns，客户端工作站也须单独开启 BBR。
6. **网关主页 + API 面收敛**（2026-08-14 评审意见「根路径是 Swagger，所有 API 都暴露了」）：根路径 `/` 由 mcp-hub 托管静态主页 `mcp-hub/homepage.html`（对外脸面：BASE URL、模型清单、快速开始、MCP 用法，带在线状态灯）；`/openapi.json`、`/redoc`、`/health` 在 nginx 层对公网返回 404（`/health/liveliness` 保留供主页状态灯）。
7. **r6 自写控制台 + LiteLLM 退居底层 + 全量收敛**（2026-08-14 晚，proto-r6/US-P14）：新增 consoled:8300（`/console/` 9 页，双角色登录：master key=管理员 8 页 / 用户虚拟 Key=仅「我的用量」；会话在内存，consoled 重启即全员下线）；nginx 改 allowlist（见拓扑表，LiteLLM `/ui`、`/login`、全部管理 API、`/onboard/admin/*` 公网 404，应急通道 `ssh -L 4000:127.0.0.1:4000`）；静态别名 claude-opus-5、qwen3.6-35b-a3 一次性迁移为 DB deployment（config.yaml 只剩设置骨架，T3/T11 复验过）；分组改写 = 对站点 deployment 先 `/model/new`（带新 tags）再 `/model/delete`（实测 tags 经 /model/new 写入、/model/info 完整回显；不依赖 `/model/update` 的 tags 透传）。
   **部署坑（重要）**：nginx.conf 被单文件 bind-mount 进 nginx-sub2api 容器，`sed -i` 会换 inode → 容器内仍是旧内容、reload 无效（2026-08-14 实测：改完 reload 行为不变，需重启容器才恢复）；deploy.sh 已改为 `cat >` 原地写（保留 inode），此后 reload 即生效。
   **静态页面门禁（2026-08-15 评审意见「对外尽量少暴露内部信息」）**：consoled 的静态服务带会话门禁——未登录仅放行 `login.html`、`admin-login.html`、`assets/portal.css`、`favicon.ico`，其余页面与 portal.js 一律 302 跳登录（页面源码、内部组件名/策略文案不可匿名抓取，页面存在性亦不可探测）；dashboard 攻击面卡已去内部组件枚举，细节只留 runbook。
   **管理员独立登录（2026-08-15）**：新增 `/console/admin-login.html`——邮箱（`ADMIN_EMAIL`）+ 密码（`ADMIN_PASSWORD`）+ 可选 TOTP 2FA（`ADMIN_TOTP_SECRET`，RFC 6238/SHA1/6 位/30s，±1 步漂移容错 + 同码防重放），凭据均在 `vps/.env`、由 deploy.sh 注入 `/etc/private-llm/console.env`；登录成功签发与用户登录同一套 `pll_session`（内存会话 + HMAC cookie，8h）。配置 ADMIN_EMAIL 后 master key 不再作为网页登录（未配置则保留旧行为兜底）；管理员登录不依赖 LiteLLM 可达。
   **LiteLLM 1.96.2 管理面实测语义**（consoled 依赖）：`/key/list` 需 `return_full_object=true` 且 `size≤100`（超限 422）；禁用字段是 `blocked`（`/key/block`/`/key/unblock`，payload `{"key":"<sha256哈希>"}`，与 `/key/delete` 的 `{"keys":[…]}` 形状不同）；blocked Key 在鉴权层直接 401（API 与 mcp-hub 的 `/key/info` 验真同步生效，无需额外代码）；`/key/info` 自查不返回 blocked 字段；`/spend/logs` 的 `api_key`/`start_date` 过滤参数实测不可靠 → consoled 全量拉取本地聚合；错误行 = `status=="failure"`（鉴权失败 401 不入日志，控制台错误表已注明口径）。
8. **全量容器化（2026-08-15，issue #7）**：consoled/onboardd/mcp-hub 从宿主机 systemd 迁入 compose——连同 litellm/postgres/wireguard sidecar，**一个 `vps/docker-compose.yml` 管 6 服务**，`docker compose up -d --build` 即部署/升级；deploy.sh 去 root 化（docker 组用户可跑，日常零 sudo）。nginx 上游从宿主机网关 IP 改共享网络容器名（`private-llm-console:8300` 等）。wireguard sidecar = host 网络 + NET_ADMIN，wg0 仍留宿主机 netns（路由模型不变，litellm 容器内 BBR 由 compose `sysctls` 设置）；consoled/onboardd 挂 docker.sock 执行 `docker exec private-llm-wireguard wg …` 与 `docker restart private-llm-mcp-hub`——**挂 sock 的容器 ≈ 宿主机 root**，仅给这两个管理面容器（有意取舍）；命令前缀 env 可覆写（`WG_EXEC`/`MCP_RESTART_CMD`，默认保留宿主机直跑语义）。127.0.0.1:4000/8100/8200/8300 端口发布仅供本机 CLI/冒烟/调试。状态全落 `/var/lib/private-llm`（bind mount，容器重建不丢，wg 密钥保留）。一次性迁移：deploy.sh 自动停用旧 systemd 单元（该步需 sudo；wg-quick 迁移瞬间隧道短暂中断）、旧 ufw 8100/8200/8300 规则作废（可手动清理）。宿主机一次性前置（sudo）：`ufw allow 51820/udp`、BBR sysctl。
   **本地集成实测（2026-08-15）**：4 镜像本地构建 + 4 容器栈（wireguard 用隔离 netns 冒烟）——admin 容器内登录、console→docker.sock→wg sidecar 的 `wg show`/`wg set peer` 链路、`/mcp/register` 触发 `docker restart private-llm-mcp-hub`（容器 StartedAt 实变）、external-mcp.json 跨容器共享写读、LiteLLM 缺席容错，全部通过。

## 2. VPS 部署（一次性）

前置：DNS A 记录已指向 VPS；云安全组放行 443/tcp 与 51820/udp；`ssh vps-tencent-tokyo` 可登；VPS 用户在 docker 组；**一次性 sudo**（#7 容器化后仅剩这些）：`sudo ufw allow 51820/udp`、BBR sysctl（`/etc/sysctl.d/99-private-llm-tunnel.conf`，已配置的 VPS 跳过）。

```bash
# 本地：同步代码上 VPS
rsync -av --exclude .env execution/proto-remote-access/ vps-tencent-tokyo:~/private-llm-src/

# VPS：
cd ~/private-llm-src/vps
cp .env.example .env && vi .env       # LITELLM_MASTER_KEY=sk-$(openssl rand -hex 16)、ADMIN_* 等
./deploy.sh                            # docker 组用户执行，无需 sudo
```

deploy.sh 幂等；首次在存量部署上运行会自动停用旧 systemd 单元（该步若非免密 sudo 会提示手动执行；wg-quick 切换瞬间隧道短暂中断）；nginx 改动带备份与 `nginx -t` 失败自动回滚（不影响 sub2api 等既有站点）。

### 建用户 Key（C3：管理员创建分发）

```bash
# 首选：控制台 https://<域名>/console/ →「用户 Key」页（管理员登录，一次性展示全文）
# CLI 等价（默认组；#7 容器化后 env 只在 vps/.env）：
source ~/private-llm-src/vps/.env
curl -s http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'content-type: application/json' \
  -d '{"key_alias":"pi-local","metadata":{"group":"default"}}'
# 绑定分组（US-P13）：先把站点划入分组（控制台「分组」页或站点页），再把 Key 的 metadata.group 设为组名
```

## 3. 站点接入（US-P7/P8）

```bash
# VPS 上签发（例：gb10，注册两个模型端口）
site-add gb10 --model deepseek-v4-flash-0731:8890 --model qwen3.6-35b-fp8:8004 --group default
# 输出：curl -fsSL "https://private-llm.onlyservice.io/onboard/install?token=..." | sudo bash

# 站点机器（如 ssh gb10）执行上面一行；自检全绿即自动注册进路由池
# token 一次性、15 分钟过期；二次使用 → 403

site-list          # 站点清单（名称/公钥/WG IP/模型/分组/状态）
site-revoke gb10   # 吊销：wg 摘 peer + LiteLLM 摘 deployment + 状态标记
```

install.sh 在站点侧：装 wireguard-tools → `wg genkey`（私钥不出机）→ 公钥注册 → 写 `/etc/wireguard/wg0.conf` → `systemctl enable --now wg-quick@wg0`（自启自愈）→ 自检（ping 10.77.0.1 + 各模型端口 `/v1/models`）→ confirm 注册 deployment。

## 4. 客户端接入

统一：`base_url = https://private-llm.onlyservice.io`，`key = sk-<用户虚拟Key>`（管理员分发）。

| 客户端 | 配置 |
|---|---|
| OpenAI SDK | `base_url=https://private-llm.onlyservice.io/v1`，model 直选 `deepseek-v4-flash-0731` / `qwen3.6-35b-fp8`（或别名 `claude-opus-5`、`qwen3.6-35b-a3`） |
| Claude Code | `ANTHROPIC_BASE_URL=https://private-llm.onlyservice.io`，`ANTHROPIC_AUTH_TOKEN=sk-…`，默认模型名 `claude-opus-5`（别名已配） |
| Pi（badlogic/pi-mono） | `pi` 设置里 provider：baseUrl `https://private-llm.onlyservice.io/v1`，apiKey `sk-…`，model 填对外名；MCP 需先 `pi install npm:pi-mcp-adapter` |
| MCP 客户端（Streamable HTTP + Bearer） | URL `https://private-llm.onlyservice.io/mcp`，头 `Authorization: Bearer sk-…`；工具名 `[a-z0-9_]`（`analyze_image`、外部 MCP 前缀如 `zhipu_*`） |

本地图片两步式：`POST /mcp/upload`（multipart `file=`，同一 Key）→ 得临时 URL（30min）→ `analyze_image(url, 问题)`。

## 5. 验收记录（T1~T13，2026-08-14 首站 gb10 实测）

| # | 故事 | 验证步骤与通过标准 | 结果 |
|---|---|---|---|
| T1 | US-P1 | OpenAI 兼容流式对话 deepseek；错 Key→401；站点隧道断开 → 可判读 5xx 快速返回 | ✅ SSE 流式通过；错 Key 401；隧道断开实测 Connection error 500（connect_timeout=5） |
| T2 | US-P2 | 带图请求直选 qwen，返回识图结果 | ✅ 红色 PNG → 「红色」 |
| T3 | US-P3 | Claude Code 仅配 base_url+Key 完成含工具调用会话 | ◐ `/v1/messages` 协议转换 200（claude-opus-5 别名）；真实 Claude Code 会话待 D2 |
| T4 | US-P4 | URL 图一步识别；本地图经 /mcp/upload 两步；错 Key→401 | ✅ 两步式识别通过；MCP 无效 Key 401；TTL 清理线程在跑 |
| T5 | US-P5 | 站点重启/断网 → 隧道自愈 | ✅ wg-quick systemd 自启 + PersistentKeepalive=25；安全组放行后自动重握手实测 |
| T6 | US-P6 | 同名模型双 deployment 分流；停一站无感；全停→503 | ◐ 单站点已验证重试/冷却路径；双站点分流待第二站点接入（D4） |
| T7 | US-P7 | site-add 一行命令接入；token 复用→403 | ✅ gb10 一行命令完成（装 WG→注册→自启→自检→注册模型）；坏 token 403 实测 |
| T8 | US-P8 | site-revoke 后隧道即断、deployment 摘除 | ✅ 流程实测（wg_remove ok、deployment 摘除、状态 revoked） |
| T9 | US-P9 | Admin UI 建/禁 Key 即时生效；用量可筛 | ✅ `/ui` 可用（master key 登录）；建 Key 即时生效（home-key 实测） |
| T10 | US-P10 | `/key/info` 仅见自身用量 | ✅ 用户 Key 自查 200 |
| T11 | US-P11 | `/v1/models` 见全部对外名；未知名→400/404 | ✅ 4 个对外名（deepseek/qwen 直选 + claude-opus-5/qwen3.6-35b-a3 别名） |
| T12 | US-P12 | 外部 MCP 注册后 tools/list 前缀工具可用 | ◐ 框架就绪（配置文件 + 占位符过滤 + 前缀代理）；智谱真实凭据待录入验证 |
| T13 | US-P13 | Key 绑组仅组内路由；伪造 tag 无法越组；组内无部署→可判读错误 | ✅ 六项矩阵全过：home Key→组外模型 401 可判读、组内 200、伪造 x-litellm-tags 双向无效、未绑组全量 |
| T14 | US-P9 修订/P14 | 控制台全流程（2026-08-14 晚实测） | ✅ 登录三态（master→admin / 用户 Key→user / 错 Key 401，连错 5 次 60s 内 429）；user 访问管理 API 全 403、/my 数据真实（今日 133 次 + 分模型）；Key 建/禁/解禁/删全链路（blocked→chat 401、mcp 401，删除→401）；分组 create/rename/delete 的 retag 实效（/model/info tags 逐条核对）；站点 token 下发（900s + 安装命令）；别名创建（/v1/models 可见 + 调用 200）；MCP 注册/移除（配置 0600 + restart + 凭据只显尾 4 位 + 不可达服务优雅降级） |
| T15 | US-P14 | 暴露面收敛回归（2026-08-14 晚实测） | ✅ 管理面 404 矩阵：/ui、/login、/sso、/openapi.json、/redoc、/health、/key/generate、/key/block、/key/update、/key/list、/model/new、/model/info、/team/list、/global/spend、/spend/logs、/onboard/admin/*、任意未知名全 404；保留面正常：/（主页 200）、/v1/models（带 Key 200）、SSE 流式 chat、/v1/messages（CC 协议 200）、/key/info、/health/liveliness、/mcp（无 Key 401）、/onboard/install（坏 token 403）、/console/（200）；site-add/list CLI 走本机 8100 不受影响；deploy.sh 冒烟含收敛自检 |

**部署当日实测补充**：本地 pi CLI（badlogic/pi-mono 0.84.1）以 `private-llm` provider（baseUrl `https://private-llm.onlyservice.io/v1`）直调 deepseek 与 qwen 均通过——US 的「本地 pi 直接调用网关模型」目标达成。

## 6. 日常运维

```bash
# 日常管理（r6 起唯一入口）：https://<域名>/console/
#   管理员：/console/admin-login.html（邮箱 + 密码 + 2FA 动态码，凭据在 vps/.env 的
#           ADMIN_EMAIL/ADMIN_PASSWORD/ADMIN_TOTP_SECRET，deploy.sh 写入 console.env；
#           配置后 master key 不再作为网页登录方式；轮换 = 改 .env 重跑 deploy.sh）
#   用户：/console/login.html（分配的访问密钥）
#   站点/分组/模型别名/用户 Key/用量/外部 MCP 全部在控制台完成；LiteLLM 引擎级配置走下面 SSH
# 日志（#7 容器化：compose 一把抓）
cd ~/private-llm-src/vps && docker compose logs -f            # 全部；--tail 100 起
docker compose logs -f console mcp-hub onboardd               # 单看三服务
# 重启组件（consoled 重启会清会话=全员重新登录；mcp-hub 重启会中断进行中的 MCP 调用；
#   wireguard 重启 = wg-quick down/up，隧道瞬断、conf 持久化的 peer 自动恢复）
cd ~/private-llm-src/vps && docker compose restart console
cd ~/private-llm-src/vps && ./deploy.sh    # 幂等升级（compose build + up + 收敛自检；无需 sudo）
# LiteLLM 应急通道（管理 API/UI 已公网 404）：ssh -L 4000:127.0.0.1:4000 vps-tencent-tokyo → http://localhost:4000/ui
# 证书：certbot 每日 cron 自动续期（已并入 renew.sh）
```

## 7. 密钥与安全（C1/C2/C5）

- master key / postgres 密码 / onboard admin token / 管理员邮箱密码与 TOTP 密钥：`vps/.env`（VPS 上 0600，不入库），compose 变量注入各容器；`/etc/private-llm/` 只剩 `external-mcp.json`（外部 MCP 注册表）。r6 起公网不再有任何接受 master key 的端点（控制台管理员登录走独立邮箱+密码+2FA 页，master key 仅服务端回环；用户登录用分配的虚拟 Key）。管理员登录连错 5 次/分钟锁定，与用户登录共用限速。
- **docker.sock 取舍（#7）**：console/onboardd 容器挂 `/var/run/docker.sock`（执行 wg peer 管理 / mcp-hub 重启），挂 sock 的容器 ≈ 宿主机 root——只给这两个管理面容器，且它们本身已是管理员权限面；其余容器（litellm/postgres/mcp-hub/wireguard）不挂。
- WG 私钥：VPS `/var/lib/private-llm/wireguard-private.key`（0600）与 `/etc/wireguard/wg0.conf`（0600）；站点私钥仅站点本机。
- 用户 Key 永不出网关：mcp-hub 只用它调 `/key/info` 与回环 LiteLLM；上游无鉴权直连不带 Key。
- 公网面：443/tcp（nginx）、51820/udp（WG）、SSH；其余容器仅 127.0.0.1 回环发布或 compose 内网互通，不监听公网。
