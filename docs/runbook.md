# Runbook：LLM-Portal 网关部署与运维

> 本手册对应仓库根目录的服务源码与 `vps/` 部署物；公开规划见 [`../ROADMAP.md`](../ROADMAP.md)。

## 1. 拓扑与端口

| 组件 | 位置 | 端口 | 说明 |
|---|---|---|---|
| edge nginx | standalone/external 为公网 80/443；offload 为受信 LAN 80 | 取决于 `EDGE_MODE` | allowlist 单入口：`/`→主页、三条兼容 API→compat:8400、其余业务 API→litellm、`/mcp*`→mcp-hub:8200、公开 onboard 路径→onboardd:8100、`/console`→consoled:8300；**其余一律 404**；SSE 不缓冲 |
| LiteLLM Proxy（容器） | 回环 | 127.0.0.1:4000 | 双协议 API（`/v1/chat/completions` + `/v1/messages`）、别名、least-busy 分流、虚拟 Key、用量记账——r6 起退居底层引擎，管理面经控制台 |
| **compat 协议兼容层（容器 `private-llm-compat`）** | 本机回环 | 127.0.0.1:8400 | **协议兼容代理（issue #9，2026-08-15）**：nginx 三条 API 路径（`= /v1/messages`、`= /v1/messages/count_tokens`、`= /v1/chat/completions`）经此转 LiteLLM——①US-13 us13-v1 内联 system 规范化（`/messages` 与 `/count_tokens` 共用同一纯函数，norm_hash 可对账）；②单工具 `required`/`any` 改写为指定工具；③多工具 forced 稳定 400 `forced_tool_choice_unsupported`；④OpenAI 流式 finish_reason=stop→`tool_calls` 修正。无变换即原始字节透传（保护 prompt cache），SSE 逐行不缓冲；鉴权/路由/记账仍归 LiteLLM；脱敏指标走 `docker logs private-llm-compat`（无 key/正文） |
| Postgres（容器） | compose 内 | 无主机端口 | LiteLLM Key/用量/deployment 存储（`STORE_MODEL_IN_DB=True`） |
| mcp-hub（容器 `private-llm-mcp-hub`） | 本机回环 | 127.0.0.1:8200 | `/mcp`（Streamable HTTP）+ `/mcp/upload` + `/mcp/files/*` + `/mcp/usage` + 主页托管；nginx 经共享网络容器名反代（#7） |
| onboardd（容器 `private-llm-onboardd`） | 本机回环 | 127.0.0.1:8100 | 站点注册：install/register/confirm + admin API（admin/* 公网 404；site-tools CLI 走本机回环）；经 docker.sock 管 wg peer（#7） |
| **consoled（容器 `private-llm-console`）** | 本机回环 | 127.0.0.1:8300 | **管理控制台后端（r6）**：`/console/` 页 + `/console/api/*`；会话双角色（管理员=邮箱登录 9 页 / 用户 Key=仅我的用量）；聚合 LiteLLM 管理 API（容器名回环）+ onboardd + wg show（经 docker.sock）+ mcp-hub 状态 |
| WireGuard wg0（wireguard sidecar 容器，host 网络 + NET_ADMIN） | 公网 | 51820/udp | wg0 仍留在**宿主机**网络命名空间（10.77.0.1/24，站点从 .11 递增；litellm 容器→站点路由模型不变）；容器起=wg-quick up、停=down；peer 增删 = onboardd/consoled 经 docker.sock `docker exec … wg set`，持久化 `/etc/wireguard/wg0.conf` |

关键实现与运维说明：
1. **三种 Edge 模式**：standalone 由本栈 `edge-nginx` + `edge-certbot` 发布 80/443；external 向既有 nginx/certbot 注入配置；offload 由受信 LAN 上游终结 TLS，本栈仅发布 HTTP 80。三者共用同一 allowlist 与严格冒烟检查。
2. **Vision MCP 不绑定固定模型名**：管理员在「MCP 管理」从已注册模型中选择；models.dev
   的 `modalities.input` 判定图片能力，未收录的私有模型须通过真实图片探测。
3. **US-P13 分组 tag 语义按 LiteLLM 1.96.2 实测校准**：① `enable_tag_filtering` 路由器级配置实测未生效，钩子改为每请求强制注入 `enable_tag_filtering=True`；② 带 `default` tag 的 deployment 会被实现当作「tag 无匹配时的兜底池」，与基线「组内无部署→报错」冲突——deployment 一律**不打 default tag**（default 组 = 隐式全量池），绑组 Key 由钩子注入组 tag、未绑组 Key 由钩子清空 tags（顺带清除客户端伪造的 `x-litellm-tags`）。Key 的分组仍存 `metadata.group`（default 视同未绑）。
4. **wstunnel 过渡通道已移除**：部署当日因云安全组未放行 51820/udp 临时用 wstunnel（UDP-over-WS 走 443）打通，后被腾讯云主机安全标记为 Risktool（Linux.Risktool.Wstunell.Agow），按安全策略双端移除（服务/二进制/nginx 路径/uffw 规则全部清除），恢复设计原方案的直连 WG UDP。
5. **隧道传输调优（2026-08-14 晚，TFT 优化，issue #6）**：跨境 wg 隧道晚高峰实测 10~43% 丢包，内层 CUBIC 把随机丢包当拥塞，40KB 请求要 8-12s、400KB 要 80-97s（等效 ~4KB/s）。修复 = **四端 BBR**（VPS 宿主、site-a 宿主、litellm 容器 netns 经 compose `sysctls`、客户端工作站）+ **wg MTU 1280**（两端 wg0.conf 持久化；大 UDP 包丢弃率高，吞吐 3-6 倍于默认 1420）+ TCP 缓冲调大（容器 `tcp_rmem/wmem` 16MB，宿主 `rmem/wmem_max` 7.5MB）。已落入 deploy.sh / install.sh 模板 / wg0.conf 模板。
   **效果（当晚 23:45 实测，隧道丢包 43% 的最差窗口）**：隧道 100KB ~20s→1.06s（最优）；短请求 keep-alive 开销 0.58s ≈ 2 RTT + LiteLLM ~0.2s（达标）；8K-token（79KB）热请求增量 **+1.11s**；32K-token（318KB）增量 **+2.09s**（低于 3s 物理约束判定线，高于 0.7s 理想线）。**结论**：网关自身开销已达「≈1-2 RTT」目标；大 prompt 残余增量由跨境链路丢包/带宽决定（错峰显著更好）。基准脚本为 `tools/bench-gateway.py`。注意：`tcp_congestion_control` 为 per-netns，客户端工作站也须单独开启 BBR。
6. **网关主页 + API 面收敛**（2026-08-14 评审意见「根路径是 Swagger，所有 API 都暴露了」）：根路径 `/` 由 mcp-hub 托管静态主页 `mcp-hub/homepage.html`（对外脸面：BASE URL、模型清单、快速开始、MCP 用法，带在线状态灯）；`/openapi.json`、`/redoc`、`/health` 在 nginx 层对公网返回 404（`/health/liveliness` 保留供主页状态灯）。
7. **自写控制台 + LiteLLM 退居底层 + 全量收敛**：consoled:8300 提供双角色会话（管理员使用邮箱+密码+可选 TOTP；用户虚拟 Key 仅访问「我的用量」）；会话 sqlite 落盘，重部署不掉线。nginx allowlist 使 LiteLLM `/ui`、`/login`、管理 API 与 `/onboard/admin/*` 对外返回 404，应急管理通过 `ssh -L 4000:127.0.0.1:4000`。模型别名存于 DB，站点分组变更通过先建后删 deployment 保持连续可用。
   **reasoning_effort 直通（2026-08-17，issue #46）**：`drop_params: true` 下通用 `openai/` deployment 的 supported 参数列表不含 `reasoning_effort`，会被**静默丢弃**（响应 200 但上游用默认档位，基准/评测口径被污染）；vLLM 上游实际支持。修复：全部注册/重建路径（onboardd confirm、console retag_site、别名克隆）统一带 `litellm_params.allowed_openai_params: ["reasoning_effort"]`；实测其余思考参数（enable_thinking、reasoning dict、top_k、chat_template_kwargs 等）本就走 extra_body 不受 drop_params 影响，无需列入。注意：**任何新的 /model/new 调用点都必须带此字段**，否则一次重建即洗掉直通。
   **部署坑（重要）**：external 模式下 nginx.conf 可能以单文件 bind mount 进入既有 nginx 容器；`sed -i` 会换 inode，导致容器内仍读旧内容。deploy.sh 使用原地写入保留 inode，使 reload 即时生效。
   **静态页面门禁（2026-08-15 评审意见「对外尽量少暴露内部信息」）**：consoled 的静态服务带会话门禁——未登录仅放行 `login.html`、`admin-login.html`、`assets/portal.css`、`favicon.ico`，其余页面与 portal.js 一律 302 跳登录（页面源码、内部组件名/策略文案不可匿名抓取，页面存在性亦不可探测）；dashboard 攻击面卡已去内部组件枚举，细节只留 runbook。
   **管理员独立登录（2026-08-15）**：新增 `/console/admin-login.html`——邮箱（`ADMIN_EMAIL`）+ 密码（`ADMIN_PASSWORD`）+ 可选 TOTP 2FA，凭据在 `vps/.env` 由 compose 注入；登录成功签发与用户登录同一套 `pll_session`（内存会话 + HMAC cookie，8h）。配置 ADMIN_EMAIL 后 master key 不再作为网页登录（未配置则保留旧行为兜底）；管理员登录不依赖 LiteLLM 可达。
   **2FA 完整实现（2026-08-15，issue #8）**：控制台「安全设置」页（`/console/2fa.html`，仅管理员）——生成密钥 → **二维码扫码**（segno 服务端出 SVG data URI，含 otpauth:// URI；也提供手工密钥）→ 输码确认启用；可输码更换密钥（轮换）；停用需密码 + 当前动态码。已启用密钥存 `/var/lib/private-llm/console/totp.json`（0600，容器 bind mount 持久化），**优先于** env 预置的 `ADMIN_TOTP_SECRET`（env 来源只读，页面提示去 env 清空后重新启用）。TOTP 校验复用既有实现：RFC 6238/SHA1/6 位/30s、±1 步漂移容错、同码防重放。**手机丢失恢复**：SSH 删 `/var/lib/private-llm/console/totp.json`（页面密钥）或清 env `ADMIN_TOTP_SECRET` 后 `docker compose restart console`，回到仅密码登录再重新启用。
   **LiteLLM 1.96.2 管理面实测语义**（consoled 依赖）：`/key/list` 需 `return_full_object=true` 且 `size≤100`（超限 422）；禁用字段是 `blocked`（`/key/block`/`/key/unblock`，payload `{"key":"<sha256哈希>"}`，与 `/key/delete` 的 `{"keys":[…]}` 形状不同）；blocked Key 在鉴权层直接 401（API 与 mcp-hub 的 `/key/info` 验真同步生效，无需额外代码）；`/key/info` 自查不返回 blocked 字段；`/spend/logs` 的 `api_key`/`start_date` 过滤参数实测不可靠 → consoled 全量拉取本地聚合；错误行 = `status=="failure"`（鉴权失败 401 不入日志，控制台错误表已注明口径）。
8. **全量容器化（2026-08-15，issue #7）**：一个 `vps/docker-compose.yml` 管理 litellm、compat、postgres、mcp-hub、onboardd、console、wireguard **7 个核心服务**；standalone 另启 edge profiles。deploy.sh 由 docker 组用户执行，日常零 sudo。wireguard sidecar 使用 host 网络 + NET_ADMIN；consoled/onboardd 挂 docker.sock 执行 WireGuard 管理与 mcp-hub 重启，**挂 sock 的容器约等于宿主机 root**。4000/8100/8200/8300/8400 仅发布到 127.0.0.1。状态落 `/var/lib/private-llm`，容器重建不丢；旧 systemd 迁移、ufw 与 BBR 属于一次性宿主机操作。
9. **会话落盘（2026-08-15）**：会话由 consoled 内存表改为 sqlite（`/var/lib/private-llm/console/sessions.db`，bind mount 持久化）——容器重建/重部署不再全员下线（此前每次 deploy 即全员重新登录，实测困扰）；登出即删行，cookie 仍只放 sid+HMAC（key 永不进 cookie）。TTL 不变（8h），登录时顺手清理过期行。
10. **用量总览重设计（2026-08-15 晚，C 方案：双 Tab，参照 sub2api，经 3 原型评审定稿）**：
    - **趋势 Tab**：6 指标卡（请求/输入/缓存读取/输出/平均TFT/平均总延迟）+ 按小时「请求量(Token 双面积)」混合图 + 平均 TFT 柱图（>1.5s 橙 / >3s 红）+ 模型分布与 Key 占比条形 + 近期错误卡；`/usage` 扩展返回 `hourly`（今天 24 小时 / 多日按日期铺满）与 `avg_tft`。
    - **明细 Tab**：`GET /console/api/usage/logs` 逐请求（上限 500 行）——**TFT = completionStartTime − startTime（生产 100% 可算）**、Token 与延迟为聚合双行列（↓输入 ↑输出 / ▣缓存读；首T + 总 + 双段迷你条，阈值着色）、Key 只显示别名、**全列可排序**、筛选 + 搜索 + 分页 + 详情抽屉（request_id/session_id/耗时分解/IP）；**↻ 刷新按钮只刷数据不刷页面**。
    - **思考强度列（2026-08-17）**：明细行展示该请求实际携带的思考参数——group_routing 钩子在 `async_pre_call_hook` 归一化后**原地**写入请求 metadata 的 `spend_logs_metadata.effort`（OpenAI `reasoning_effort`/`reasoning.effort` 原样；Anthropic `thinking.budget_tokens` → `budget:N`；`type=disabled` → `off`），历史行/未携带显示「—」。1.96.2 实测三坑：① 落库按 `SpendLogsMetadata.__annotations__` 白名单重建 metadata，`requester_metadata` 不在白名单、写库时被丢弃（custom logger 事件里能看到，DB 里没有）——`spend_logs_metadata`（自由键值槽）在白名单内，是唯一可靠落库通道；② `function_setup` 在钩子前把 `data["metadata"]` 同引用存进 Logging 对象，钩子里必须原地改写、整体替换会丢；③ `/v1/messages` 入口的代理 metadata 在 `litellm_metadata` 通道，钩子需两通道都写。已知局限：钩子在路由前，`drop_params` 对不支持上游的静默丢弃不可感知（记录的是请求携带值）。
    - **时区修正**：日志时间统一转 Asia/Shanghai(+08) 展示（此前直接切 UTC 字符串，差 8 小时）。
    - **客户端 IP（2026-08-15 二次调查后解决）**：LiteLLM 实为支持 XFF——`general_settings.use_x_forwarded_for: true`（config.yaml 已加）即记录 nginx 传来的 `X-Forwarded-For`（consoled 取首跳）；此前记的是 nginx 容器地址（172.18.x，历史行页面标注「经 nginx」）。仅当上游为可信反代时开启：litellm 端口只在 docker 网内可达，安全。已实测：工作站经公网调用，日志记录真实出口 IP。
   **本地集成实测（2026-08-15）**：4 镜像本地构建 + 4 容器栈（wireguard 用隔离 netns 冒烟）——admin 容器内登录、console→docker.sock→wg sidecar 的 `wg show`/`wg set peer` 链路、`/mcp/register` 触发 `docker restart private-llm-mcp-hub`（容器 StartedAt 实变）、external-mcp.json 跨容器共享写读、LiteLLM 缺席容错，全部通过。
11. **协议兼容层（2026-08-15，issue #9）**：nginx 与 LiteLLM 之间新增 compat-proxy（`compat/compat_proxy.py`，容器 `private-llm-compat:8400`，Starlette 单文件，随 compose 第 7 服务部署）。背景：[`tools/agent-compat/`](../tools/agent-compat/) 矩阵实测 forced tool choice 和内联 system 消息存在上游兼容差异。独立代理在 LiteLLM 解析前执行确定性规范化：单工具 forced choice 改写、多工具 forced choice 稳定 400、内联 system 合并、OpenAI 流式 finish_reason 修正，以及 DSML arguments 的安全 JSON 规范化。无变换请求保持原始字节透传；鉴权、路由和记账仍由 LiteLLM 负责。

## 2. VPS 部署（一次性）

前置：`ssh your-vps` 可登，用户在 docker 组，并完成所选入口模式的网络准备。standalone 需 DNS A 记录及 80/443/tcp、51820/udp；external 需既有 nginx/certbot；offload 需受信 LAN 上游反代至本机 80/tcp，并放行站点直连的 51820/udp。ufw 与 BBR sysctl 可能需要一次性 sudo。

```bash
# 本地：首次部署可同步工作树；生产升级必须使用下节的 commit 包
rsync -av --exclude .env ./ your-vps:~/LLM-Portal/

# VPS：
cd ~/LLM-Portal/vps
cp .env.example .env && vi .env       # LITELLM_MASTER_KEY=sk-$(openssl rand -hex 16)、ADMIN_* 等
./deploy.sh                            # docker 组用户执行，无需 sudo
```

deploy.sh 幂等；首次在存量部署上运行会自动停用旧 systemd 单元（该步若非免密 sudo 会提示手动执行；wg-quick 切换瞬间隧道短暂中断）。三种入口都会执行 `nginx -t`；external 注入既有配置时另带备份与失败回滚。

### 2.1 生产升级与回滚（commit 包）

多台网关必须逐台升级：每台验收通过后再继续下一台，不得并行升级。下文以目标源码目录 `ROOT=$HOME/LLM-Portal` 为例。

```bash
# 本地仓库；MERGE_SHA 必须是已合并 main 的 commit
vps/release-package.sh "$MERGE_SHA" /tmp/private-llm-release
cd /tmp/private-llm-release
shasum -a 256 -c "private-llm-$MERGE_SHA.tar.sha256"

# 每台目标机先记录恢复点
ROOT=${ROOT:-$HOME/LLM-Portal}
STAMP=$(date +%Y%m%d-%H%M%S)
tar --exclude='./vps/.env' -C "$ROOT" -czf "$HOME/private-llm-source-$STAMP.tgz" .
cd "$ROOT/vps"
docker inspect -f '{{.Name}} {{.Image}} {{.State.Status}}' \
  litellm private-llm-compat private-llm-postgres private-llm-mcp-hub \
  private-llm-onboardd private-llm-console private-llm-wireguard \
  > "$HOME/private-llm-images-$STAMP.txt"

# 上传四个发布文件到独立目录后：先验 tar，再解到暂存目录，再原子口径同步；.env 永不覆盖/删除
RELEASE_DIR=$HOME/private-llm-release-$MERGE_SHA
cd "$RELEASE_DIR"
shasum -a 256 -c "private-llm-$MERGE_SHA.tar.sha256"
STAGE=$(mktemp -d)
tar -xf "private-llm-$MERGE_SHA.tar" -C "$STAGE"
MANIFEST=$(pwd)/private-llm-$MERGE_SHA.files.sha256
(cd "$STAGE" && shasum -a 256 -c "$MANIFEST")
rsync -a --delete --exclude '.git/' --exclude 'vps/.env' "$STAGE/" "$ROOT/"
(cd "$ROOT" && shasum -a 256 -c "$MANIFEST")
cd "$ROOT/vps" && ./deploy.sh          # 任一必需检查失败即非零退出
```

发布后从目标机确认 7 个核心容器均为 `running`，本机 console 无会话为 401、无效 MCP Key 为 401；再从发布控制端检查对应公网域名的 `/console/` 可达、`/mcp` 无效 Key 为 401。源码清单、镜像 ID、HTTP 状态和备份路径共同组成发布 receipt。

单端失败必须先恢复该端，恢复通过前不得继续下一端。恢复源码备份后保留现有 `vps/.env`，重跑该备份版本的 `vps/deploy.sh` 并重复全部健康检查。若目标版本早于 issue #51，**必须在覆盖当前源码前**运行：

```bash
cd "$ROOT"
./vps/prepare_legacy_mcp_rollback.py /etc/private-llm/external-mcp.json
# 仅 groups 字段缺失或严格等于 [] 的合法条目保留；其余受限、畸形或结构错误条目全部隔离。
# 任一备份、解析、写入或验证失败都会非零退出，此时禁止启动旧 mcp-hub。
```

隔离器生成 0600 原字节备份与 quarantine 文件；故障排除后可恢复。GitHub 侧用 revert merge commit，不改写 main 历史。

### 建用户 Key（C3：管理员创建分发）

```bash
# 首选：控制台 https://<域名>/console/ →「用户 Key」页（管理员登录，一次性展示全文）
# CLI 等价（默认组；#7 容器化后 env 只在 vps/.env）：
source ~/LLM-Portal/vps/.env
curl -s http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'content-type: application/json' \
  -d '{"key_alias":"pi-local","metadata":{"group":"default"}}'
# 绑定分组（US-P13）：先把站点划入分组（控制台「分组」页或站点页），再把 Key 的 metadata.group 设为组名
```

## 3. 站点接入（US-P7/P8）

控制台「站点与公钥」的新增站点可选 Direct：填写形如
`http://192.168.100.55:8005/v1` 的服务地址，探测并选择 `/v1/models` 返回的模型即可登记。
Direct 仅接受 IP 字面量的本机、RFC1918/ULA 或 CGNAT 地址，不跟随 HTTP 重定向；它不创建
WireGuard peer，也不会在删除时停止上游服务。需要跨网络接入的机器仍使用下述 WireGuard 流程。

```bash
# VPS 上签发（例：site-a，注册两个模型端口）
site-add site-a --model deepseek-v4-flash-0731:8890 \
  --model qwen3.8-27b:8004:qwen3.8-27b-mtp2 --group default
# 输出：curl -fsSL "https://llm-portal.example.com/onboard/install?token=..." | sudo bash

# 站点机器（如 ssh site-a）执行上面一行；自检全绿即自动注册进路由池
# token 一次性、15 分钟过期；二次使用 → 403

site-list          # 站点清单（名称/公钥/WG IP/模型/分组/状态）
site-revoke site-a   # 吊销：wg 摘 peer + LiteLLM 摘 deployment + 状态标记
```

install.sh 在站点侧：装 wireguard-tools → `wg genkey`（私钥不出机）→ 公钥注册 → 写 `/etc/wireguard/wg0.conf` → `systemctl enable --now wg-quick@wg0`（自启自愈）→ 自检（ping 10.77.0.1 + 各模型端口 `/v1/models`）→ confirm 注册 deployment。

站点接入后的模型增删改走控制台「站点」页的 **模型** 按钮（无需重新走 install.sh）：

- **刷新上游**：站点换了模型（引擎/端口不变）时用——对外模型名不变、仅替换发往上游的 model id（先建新 deployment 再删旧，不留 404 窗口），订阅方与 Key 无需任何改动；
- **添加模型**：独立弹窗三步——① 端口从站点已知端口下拉选（后端 `known_ports` = deployment ∪ 登记簿，避免手打拼错 api_base；新端口才选「其他端口」手填），选定即**自动探测**该端口 `/v1/models`（llama.cpp/vLLM 均适用；探测失败可手填上游 id）→ ② 点选上游真实 id（自动带出对外名）→ ③ 确认对外模型名；加完自动回到列表弹窗；
- **删除**：把单个 deployment 摘出路由池（两段式确认）。以上操作均同步 onboardd 登记簿（`/onboard/admin/models`）。

## 4. 客户端接入

统一：`base_url = https://llm-portal.example.com`，`key = sk-<用户虚拟Key>`（管理员分发）。

| 客户端 | 配置 |
|---|---|
| OpenAI SDK | `base_url=https://llm-portal.example.com/v1`，model 使用 `/v1/models` 返回的当前注册名（如 `deepseek-v4-flash` / `qwen3.8-27b`） |
| Claude Code | `ANTHROPIC_BASE_URL=https://llm-portal.example.com`，`ANTHROPIC_AUTH_TOKEN=sk-…`，默认模型名 `claude-opus-5`（别名已配） |
| Pi（badlogic/pi-mono） | 控制台 Key「使用」→ Pi 可复制完整 `models.json` + `settings.json`；私有 DeepSeek 显式配置 1M 上下文、默认 `high`，仅开放 `high/max` effort；MCP 需先 `pi install npm:pi-mcp-adapter` |
| DeepSeek Harness（dsh） | 控制台 Key「使用」→ DeepSeek Harness 可复制 `~/.dsh/.credentials.yaml` + `~/.dsh/settings.yaml`；通过内置 `dsh-llm-pi-ai` 的 `llm-pi-ai` settings 分节注册 Portal 自定义路由，显式配置 1M 上下文、默认 `high`，仅开放 `high/max` effort |
| MCP 客户端（Streamable HTTP + Bearer） | URL `https://llm-portal.example.com/mcp`，头 `Authorization: Bearer sk-…`；工具名 `[a-z0-9_]`（`analyze_image`、`upload_image`、外部 MCP 前缀如 `zhipu_*`） |

本地图片（issue #71）三种方式，同一套校验（类型白名单 jpg/png/webp/gif、声明 MIME 须与字节签名一致、≤10MB）与临时文件存储（随机 token、30min TTL）：
- **MCP 内嵌一步式**：`analyze_image(question, image_base64, mime_type?)`——本地图片 Base64 直接识别，`mime_type` 缺省按签名识别，无需先上传；
- **MCP 上传工具**：`upload_image(image_base64, mime_type?)` → 返回临时 URL（30min）→ 可反复传给 `analyze_image(question, image_url)`；
- **HTTP 两步式（保留兼容）**：`POST /mcp/upload`（multipart `file=`，同一 Key）→ 得临时 URL → `analyze_image(question, image_url)`。

Vision 后端在控制台「MCP 管理」选择。控制台缓存 `https://models.dev/models.json` 24 小时；
刷新失败时沿用最后一次成功缓存。目录明确不含 `image` 输入的模型不可选；目录未知的私有
模型在保存前经回环 LiteLLM 发送最小图片探测。选择持久化于
`/etc/private-llm/vision/config.json`，mcp-hub 每次调用动态读取；删除已选模型后页面显示
配置失效，调用返回明确模型错误，不静默回退。旧 `MCP_VISION_MODEL` 仅用于升级迁移。

外部 MCP 由管理员在「MCP 管理」绑定零个或多个分组：不绑定（`groups: []` 或旧条目无
`groups`）表示全局可用；绑定后，仅 `metadata.group` 命中的 Key 能在 `tools/list`
发现并通过 `tools/call` 使用。未绑定/`default` Key 只见全局工具；内建
`analyze_image` 默认全局。客户端 URL、Bearer Key 与传输协议不变。

注册外部 MCP 前，控制台会在 10 秒内完成上游 MCP 初始化和 `tools/list` 预检；至少
发现一个工具后才会写入 `external-mcp.json` 并重启 mcp-hub。鉴权、网络、TLS、协议或
零工具失败会保留表单与原配置，不会重启服务；错误只返回可操作的类别，不回显外部凭据。
注册、分组保存和移除均先在控制台页面内确认重启影响，不会触发浏览器原生确认框。

注册后的 mcp-hub 精确 SHA/工具归属 attestation 使用单调时钟 deadline，而不是固定轮数：
默认 `MCP_HUB_READY_TIMEOUT=45`、`MCP_HUB_READY_POLL_INTERVAL=0.5`。外部 MCP 初始化或
`tools/list` 较慢时，仅提高 timeout；候选配置和回滚后的旧配置都使用同一 deadline/polling
策略，绝不能改为只检查进程存活或放宽 SHA/owner 匹配。

部署前和每次三项外部 MCP 注册批次前，先创建时间戳备份：备份必须是 `0600`、字节 SHA256
读回一致，并记录目标文件的 owner、mode 和 inode；备份文件及其目录都必须 `fsync` 成功，任一
检查失败即停止，禁止开始注册。运行时失败会原 inode 恢复原字节、mode/owner，重启 mcp-hub 并
确认旧 SHA 和工具归属证明。操作 receipt 仅记录路径、SHA、mode 和 attestation，绝不包含 JSON
内容或外部凭据。

### BigModel MCP 批次

以下三项均为全局工具，不绑定任何 groups；每次只注册一项，确认预检、重启和 tools/list 后再继续。
不要把 Bearer 凭据写入 shell history、配置仓库、截图或运维 receipt。

| 名称 | URL | 前缀 | 预期工具 |
|---|---|---|---|
| `zai-search` | `https://open.bigmodel.cn/api/mcp/web_search_prime/mcp` | `zai_search_` | `webSearchPrime` |
| `zai-reader` | `https://open.bigmodel.cn/api/mcp/web_reader/mcp` | `zai_reader_` | `webReader` |
| `zai-zread` | `https://open.bigmodel.cn/api/mcp/zread/mcp` | `zai_zread_` | `search_doc`, `get_repo_structure`, `read_file` |

安全验收调用应只使用无敏感内容的公开查询：Search 查询公开项目名称，Reader 读取公开 HTTPS
页面，Zread 仅查询公开仓库结构或读取公开文件。任何预检、工具归属或调用失败时，立即停止后续
两项注册；通过「移除」撤销刚注册的项，等待 mcp-hub 重启并确认旧 SHA/工具证明恢复后再排障。

## 5. 验收记录（T1~T15，2026-08-14 首站 site-a 实测）

| # | 故事 | 验证步骤与通过标准 | 结果 |
|---|---|---|---|
| T1 | US-P1 | OpenAI 兼容流式对话 deepseek；错 Key→401；站点隧道断开 → 可判读 5xx 快速返回 | ✅ SSE 流式通过；错 Key 401；隧道断开实测 Connection error 500（connect_timeout=5） |
| T2 | US-P2 | 带图请求直选 qwen，返回识图结果 | ✅ 红色 PNG → 「红色」 |
| T3 | US-P3 | Claude Code 仅配 base_url+Key 完成含工具调用会话 | ◐ `/v1/messages` 协议转换 200（claude-opus-5 别名）；真实 Claude Code 会话待 D2 |
| T4 | US-P4 | URL 图一步识别；本地图经 /mcp/upload 两步；错 Key→401 | ✅ 两步式识别通过；MCP 无效 Key 401；TTL 清理线程在跑 |
| T5 | US-P5 | 站点重启/断网 → 隧道自愈 | ✅ wg-quick systemd 自启 + PersistentKeepalive=25；安全组放行后自动重握手实测 |
| T6 | US-P6 | 同名模型双 deployment 分流；停一站无感；全停→503 | ◐ 单站点已验证重试/冷却路径；双站点分流待第二站点接入（D4） |
| T7 | US-P7 | site-add 一行命令接入；token 复用→403 | ✅ site-a 一行命令完成（装 WG→注册→自启→自检→注册模型）；坏 token 403 实测 |
| T8 | US-P8 | site-revoke 后隧道即断、deployment 摘除 | ✅ 流程实测（wg_remove ok、deployment 摘除、状态 revoked） |
| T9 | US-P9 | 管理控制台建/禁 Key 即时生效；用量可筛 | ✅ `/console/` 管理面可用；建 Key 即时生效（home-key 实测） |
| T10 | US-P10 | `/key/info` 仅见自身用量 | ✅ 用户 Key 自查 200 |
| T11 | US-P11 | `/v1/models` 见全部对外名；未知名→400/404 | ✅ 4 个对外名（deepseek/qwen 直选 + claude-opus-5/qwen3.6-35b-a3 别名） |
| T12 | US-P12/#51 | 外部 MCP 注册后前缀工具可用，按 Key 分组裁剪 tools/list/tools/call | ✅ FastMCP 3.4.7 授权矩阵与 console 配置测试通过；Codex `web-reader` 真实注册为 `web_webReader`（home 标签）并经代理读取 example.com 通过 |
| T13 | US-P13 | Key 绑组仅组内路由；伪造 tag 无法越组；组内无部署→可判读错误 | ✅ 六项矩阵全过：home Key→组外模型 401 可判读、组内 200、伪造 x-litellm-tags 双向无效、未绑组全量 |
| T14 | US-P9 修订/P14 | 控制台全流程（2026-08-14~15 实测） | ✅ 管理员邮箱+密码（可选 TOTP）→admin / 用户 Key→user / 错凭据 401，连错 5 次 60s 内 429；user 访问管理 API 全 403；Key 建/禁/解禁/删、分组 retag、站点 token、别名和 MCP 注册/移除链路通过 |
| T15 | US-P14 | 暴露面收敛回归（2026-08-14 晚实测） | ✅ 管理面 404 矩阵：/ui、/login、/sso、/openapi.json、/redoc、/health、/key/generate、/key/block、/key/update、/key/list、/model/new、/model/info、/team/list、/global/spend、/spend/logs、/onboard/admin/*、任意未知名全 404；保留面正常：/（主页 200）、/v1/models（带 Key 200）、SSE 流式 chat、/v1/messages（CC 协议 200）、/key/info、/health/liveliness、/mcp（无 Key 401）、/onboard/install（坏 token 403）、/console/（200）；site-add/list CLI 走本机 8100 不受影响；deploy.sh 冒烟含收敛自检 |

**部署当日实测补充**：本地 pi CLI（badlogic/pi-mono 0.84.1）以 `private-llm` provider（baseUrl `https://llm-portal.example.com/v1`）直调 deepseek 与 qwen 均通过——US 的「本地 pi 直接调用网关模型」目标达成。

## 6. 日常运维

```bash
# 日常管理（r6 起唯一入口）：https://<域名>/console/
#   管理员：/console/admin-login.html（邮箱 + 密码 + 2FA 动态码；2FA 在「安全设置」页扫码启用/轮换/停用，
#           页面密钥优先于 env 的 ADMIN_TOTP_SECRET；手机丢失恢复见 §1.8）
#   用户：/console/login.html（分配的访问密钥）
#   站点/分组/模型别名/用户 Key/用量/外部 MCP 全部在控制台完成；LiteLLM 引擎级配置走下面 SSH
# 日志（#7 容器化：compose 一把抓）
cd ~/LLM-Portal/vps && docker compose logs -f            # 全部；--tail 100 起
docker compose logs -f console mcp-hub onboardd               # 单看三服务
# compat 变换指标（issue #9；脱敏：只有规则/索引/norm_hash，无 key 无正文）
docker logs --tail 200 private-llm-compat
#   对账：同一报文分别打 /v1/messages 与 /v1/messages/count_tokens，两条 compat.transform
#   的 norm_hash 应一致（= 同一有效消息序列，US-13 验收口径）
# compat 回滚（兼容层自身故障时；恢复 = git 还原后重跑 deploy.sh）
cd ~/LLM-Portal && git checkout <compat 之前的提交> -- vps/   # 或手改 nginx conf 去掉三个 = location
cd ~/LLM-Portal/vps && ./deploy.sh                        # 重渲染 nginx，三条路径回直达 litellm
docker compose stop compat                                     # 容器可留可停，不再有流量
# 重启组件（consoled 会话已落盘，重启不掉线；mcp-hub 重启会中断进行中的 MCP 调用；
#   wireguard 重启 = wg-quick down/up，隧道瞬断、conf 持久化的 peer 自动恢复）
cd ~/LLM-Portal/vps && docker compose restart console
# 单服务重建（改代码后）：up -d --build <svc>；注意——重建任何 nginx 上游容器（litellm/compat/
#   console/mcp-hub/onboardd）后其共享网络 IP 会变，nginx 把容器名解析成旧 IP 导致公网 502，
#   须补一刀：docker exec <edge-nginx 容器> nginx -s reload（2026-08-16 实测踩坑）
cd ~/LLM-Portal/vps && ./deploy.sh    # 幂等升级（compose build + up + 收敛自检；无需 sudo）
# LiteLLM 应急通道（管理 API/UI 已公网 404）：ssh -L 4000:127.0.0.1:4000 your-vps → http://localhost:4000/ui
# 证书：standalone 的 edge-certbot 每 12h 检查续期；external 沿用既有 certbot；offload 不在本栈持证书
```

## 7. 密钥与安全（C1/C2/C5）

- master key / postgres 密码 / onboard admin token / 管理员邮箱密码与 TOTP 密钥：`vps/.env`（VPS 上 0600，不入库），compose 变量注入各容器。配置 `ADMIN_EMAIL` 后控制台管理员登录使用独立邮箱+密码+2FA，master key 仅服务端回环；若留空则保留旧版 master key 网页登录兼容路径，生产部署禁止留空。用户登录使用虚拟 Key；管理员与用户登录均按 IP 限速。
- **docker.sock 取舍（#7）**：console/onboardd 容器挂 `/var/run/docker.sock`（执行 wg peer 管理 / mcp-hub 重启），挂 sock 的容器 ≈ 宿主机 root——只给这两个管理面容器，且它们本身已是管理员权限面；其余容器（litellm/compat/postgres/mcp-hub/wireguard）不挂（compat 亦不持任何密钥，鉴权头原样透传）。
- WG 私钥：VPS `/var/lib/private-llm/wireguard-private.key`（0600）与 `/etc/wireguard/wg0.conf`（0600）；站点私钥仅站点本机。
- 用户 Key 永不出网关：mcp-hub 只用它调 `/key/info` 与回环 LiteLLM；上游无鉴权直连不带 Key。
- **Key 明文保险库（2026-08-15，管理员可再查）**：管理员需求「查看生成的 key，而非仅一次展示」——consoled 在创建时把明文 Fernet 加密存 `/var/lib/private-llm/console/keyvault.db`（密钥文件 `keyvault.key` 0600，独立于密文），`POST /console/api/keys/reveal`（仅管理员）解密取回；「使用」弹窗自动取回代入。保险库启用前的旧 Key 无法由哈希反推：配置区在取得真实明文前保持为空；管理员可粘贴完整 Key，经所选 token 的 SHA-256 与在线 `/key/info` 双重校验后补录保险库，不轮换、不改动该 Key。**边界变化：网关成为密钥保管者**——VPS 失陷即密钥失陷（加密仅防离库拖走）；轮换保险库 = 删 `keyvault.key`（旧密文不可解，等同重签）。
- 对外面按入口模式收敛：standalone 为 80/443/tcp、51820/udp 与 SSH；external 以既有 nginx 实际入口为准；offload 的 HTTP 80 只允许受信 LAN 上游访问，另保留 51820/udp 与 SSH。其余容器仅回环或 compose 内网互通。
# Console usage read-only database role

Set `CONSOLE_USAGE_PASSWORD` to `openssl rand -hex 24` in `vps/.env` before deploying. `vps/deploy.sh` rejects a missing or non-hex value, then creates or rotates the `console_usage` role with only `SELECT` on `LiteLLM_SpendLogs`.
