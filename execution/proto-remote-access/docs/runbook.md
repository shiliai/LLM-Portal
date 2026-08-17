# Runbook：private-llm 网关部署与运维

> 交付物：`execution/proto-remote-access/`（设计文档 §10）
> 设计：`docs/superpowers/specs/2026-08-14-remote-model-access-prototype-design.md`（proto-r6）

## 1. 拓扑与端口

| 组件 | 位置 | 端口 | 说明 |
|---|---|---|---|
| nginx（既有 `nginx-sub2api` 容器） | 公网 | 443/tcp | 单入口（r6 allowlist）：`/`→主页、`= /v1/messages`+`= /v1/messages/count_tokens`+`= /v1/chat/completions`→compat:8400（issue #9）、其余`/v1/*`+`= /key/info`+`= /health/liveliness`→litellm、`/mcp*`→mcp-hub:8200、`/onboard/install\|register\|confirm`→onboardd:8100、`/console`→consoled:8300；**其余一律 404**（LiteLLM `/ui`/`/login`/全部管理 API、`/onboard/admin/*` 不对公网）；SSE 不缓冲；本机仍可直查 127.0.0.1:4000 |
| LiteLLM Proxy（容器） | 回环 | 127.0.0.1:4000 | 双协议 API（`/v1/chat/completions` + `/v1/messages`）、别名、least-busy 分流、虚拟 Key、用量记账——r6 起退居底层引擎，管理面经控制台 |
| **compat 协议兼容层（容器 `private-llm-compat`）** | 本机回环 | 127.0.0.1:8400 | **协议兼容代理（issue #9，2026-08-15）**：nginx 三条 API 路径（`= /v1/messages`、`= /v1/messages/count_tokens`、`= /v1/chat/completions`）经此转 LiteLLM——①US-13 us13-v1 内联 system 规范化（`/messages` 与 `/count_tokens` 共用同一纯函数，norm_hash 可对账）；②单工具 `required`/`any` 改写为指定工具；③多工具 forced 稳定 400 `forced_tool_choice_unsupported`；④OpenAI 流式 finish_reason=stop→`tool_calls` 修正。无变换即原始字节透传（保护 prompt cache），SSE 逐行不缓冲；鉴权/路由/记账仍归 LiteLLM；脱敏指标走 `docker logs private-llm-compat`（无 key/正文） |
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
5. **隧道传输调优（2026-08-14 晚，TFT 优化，issue #6）**：跨境 wg 隧道晚高峰实测 10~43% 丢包，内层 CUBIC 把随机丢包当拥塞，40KB 请求要 8-12s、400KB 要 80-97s（等效 ~4KB/s）。修复 = **四端 BBR**（VPS 宿主、site-a 宿主、litellm 容器 netns 经 compose `sysctls`、客户端工作站）+ **wg MTU 1280**（两端 wg0.conf 持久化；大 UDP 包丢弃率高，吞吐 3-6 倍于默认 1420）+ TCP 缓冲调大（容器 `tcp_rmem/wmem` 16MB，宿主 `rmem/wmem_max` 7.5MB）。已落入 deploy.sh / install.sh 模板 / wg0.conf 模板。
   **效果（当晚 23:45 实测，隧道丢包 43% 的最差窗口）**：隧道 100KB ~20s→1.06s（最优）；短请求 keep-alive 开销 0.58s ≈ 2 RTT + LiteLLM ~0.2s（达标）；8K-token（79KB）热请求增量 **+1.11s**；32K-token（318KB）增量 **+2.09s**（低于 3s 物理约束判定线，高于 0.7s 理想线）。**结论**：网关自身开销已达「≈1-2 RTT」目标；大 prompt 残余增量由跨境链路丢包/带宽决定（错峰显著更好）。基准脚本固化 `docs/bench-gateway.py`（部署机 `~/private-llm-bench/bench.py` 同源）。注意：`tcp_congestion_control` 为 per-netns，客户端工作站也须单独开启 BBR。
6. **网关主页 + API 面收敛**（2026-08-14 评审意见「根路径是 Swagger，所有 API 都暴露了」）：根路径 `/` 由 mcp-hub 托管静态主页 `mcp-hub/homepage.html`（对外脸面：BASE URL、模型清单、快速开始、MCP 用法，带在线状态灯）；`/openapi.json`、`/redoc`、`/health` 在 nginx 层对公网返回 404（`/health/liveliness` 保留供主页状态灯）。
7. **r6 自写控制台 + LiteLLM 退居底层 + 全量收敛**（2026-08-14 晚，proto-r6/US-P14）：新增 consoled:8300（`/console/` 9 页，双角色登录：master key=管理员 8 页 / 用户虚拟 Key=仅「我的用量」；会话 sqlite 落盘，r10 起重部署不掉线）；nginx 改 allowlist（见拓扑表，LiteLLM `/ui`、`/login`、全部管理 API、`/onboard/admin/*` 公网 404，应急通道 `ssh -L 4000:127.0.0.1:4000`）；静态别名 claude-opus-5、qwen3.6-35b-a3 一次性迁移为 DB deployment（config.yaml 只剩设置骨架，T3/T11 复验过）；分组改写 = 对站点 deployment 先 `/model/new`（带新 tags）再 `/model/delete`（实测 tags 经 /model/new 写入、/model/info 完整回显；不依赖 `/model/update` 的 tags 透传）。
   **reasoning_effort 直通（2026-08-17，issue #46）**：`drop_params: true` 下通用 `openai/` deployment 的 supported 参数列表不含 `reasoning_effort`，会被**静默丢弃**（响应 200 但上游用默认档位，基准/评测口径被污染）；vLLM 上游实际支持。修复：全部注册/重建路径（onboardd confirm、console retag_site、别名克隆）统一带 `litellm_params.allowed_openai_params: ["reasoning_effort"]`；实测其余思考参数（enable_thinking、reasoning dict、top_k、chat_template_kwargs 等）本就走 extra_body 不受 drop_params 影响，无需列入。注意：**任何新的 /model/new 调用点都必须带此字段**，否则一次重建即洗掉直通。
   **部署坑（重要）**：nginx.conf 被单文件 bind-mount 进 nginx-sub2api 容器，`sed -i` 会换 inode → 容器内仍是旧内容、reload 无效（2026-08-14 实测：改完 reload 行为不变，需重启容器才恢复）；deploy.sh 已改为 `cat >` 原地写（保留 inode），此后 reload 即生效。
   **静态页面门禁（2026-08-15 评审意见「对外尽量少暴露内部信息」）**：consoled 的静态服务带会话门禁——未登录仅放行 `login.html`、`admin-login.html`、`assets/portal.css`、`favicon.ico`，其余页面与 portal.js 一律 302 跳登录（页面源码、内部组件名/策略文案不可匿名抓取，页面存在性亦不可探测）；dashboard 攻击面卡已去内部组件枚举，细节只留 runbook。
   **管理员独立登录（2026-08-15）**：新增 `/console/admin-login.html`——邮箱（`ADMIN_EMAIL`）+ 密码（`ADMIN_PASSWORD`）+ 可选 TOTP 2FA，凭据在 `vps/.env` 由 compose 注入；登录成功签发与用户登录同一套 `pll_session`（内存会话 + HMAC cookie，8h）。配置 ADMIN_EMAIL 后 master key 不再作为网页登录（未配置则保留旧行为兜底）；管理员登录不依赖 LiteLLM 可达。
   **2FA 完整实现（2026-08-15，issue #8）**：控制台「安全设置」页（`/console/2fa.html`，仅管理员）——生成密钥 → **二维码扫码**（segno 服务端出 SVG data URI，含 otpauth:// URI；也提供手工密钥）→ 输码确认启用；可输码更换密钥（轮换）；停用需密码 + 当前动态码。已启用密钥存 `/var/lib/private-llm/console/totp.json`（0600，容器 bind mount 持久化），**优先于** env 预置的 `ADMIN_TOTP_SECRET`（env 来源只读，页面提示去 env 清空后重新启用）。TOTP 校验复用既有实现：RFC 6238/SHA1/6 位/30s、±1 步漂移容错、同码防重放。**手机丢失恢复**：SSH 删 `/var/lib/private-llm/console/totp.json`（页面密钥）或清 env `ADMIN_TOTP_SECRET` 后 `docker compose restart console`，回到仅密码登录再重新启用。
   **LiteLLM 1.96.2 管理面实测语义**（consoled 依赖）：`/key/list` 需 `return_full_object=true` 且 `size≤100`（超限 422）；禁用字段是 `blocked`（`/key/block`/`/key/unblock`，payload `{"key":"<sha256哈希>"}`，与 `/key/delete` 的 `{"keys":[…]}` 形状不同）；blocked Key 在鉴权层直接 401（API 与 mcp-hub 的 `/key/info` 验真同步生效，无需额外代码）；`/key/info` 自查不返回 blocked 字段；`/spend/logs` 的 `api_key`/`start_date` 过滤参数实测不可靠 → consoled 全量拉取本地聚合；错误行 = `status=="failure"`（鉴权失败 401 不入日志，控制台错误表已注明口径）。
8. **全量容器化（2026-08-15，issue #7）**：consoled/onboardd/mcp-hub 从宿主机 systemd 迁入 compose——连同 litellm/postgres/wireguard sidecar，**一个 `vps/docker-compose.yml` 管 6 服务**，`docker compose up -d --build` 即部署/升级；deploy.sh 去 root 化（docker 组用户可跑，日常零 sudo）。nginx 上游从宿主机网关 IP 改共享网络容器名（`private-llm-console:8300` 等）。wireguard sidecar = host 网络 + NET_ADMIN，wg0 仍留宿主机 netns（路由模型不变，litellm 容器内 BBR 由 compose `sysctls` 设置）；consoled/onboardd 挂 docker.sock 执行 `docker exec private-llm-wireguard wg …` 与 `docker restart private-llm-mcp-hub`——**挂 sock 的容器 ≈ 宿主机 root**，仅给这两个管理面容器（有意取舍）；命令前缀 env 可覆写（`WG_EXEC`/`MCP_RESTART_CMD`，默认保留宿主机直跑语义）。127.0.0.1:4000/8100/8200/8300 端口发布仅供本机 CLI/冒烟/调试。状态全落 `/var/lib/private-llm`（bind mount，容器重建不丢，wg 密钥保留）。一次性迁移：deploy.sh 自动停用旧 systemd 单元（该步需 sudo；wg-quick 迁移瞬间隧道短暂中断）、旧 ufw 8100/8200/8300 规则作废（可手动清理）。宿主机一次性前置（sudo）：`ufw allow 51820/udp`、BBR sysctl。
9. **会话落盘（2026-08-15）**：会话由 consoled 内存表改为 sqlite（`/var/lib/private-llm/console/sessions.db`，bind mount 持久化）——容器重建/重部署不再全员下线（此前每次 deploy 即全员重新登录，实测困扰）；登出即删行，cookie 仍只放 sid+HMAC（key 永不进 cookie）。TTL 不变（8h），登录时顺手清理过期行。
10. **用量总览重设计（2026-08-15 晚，C 方案：双 Tab，参照 sub2api，经 3 原型评审定稿）**：
    - **趋势 Tab**：6 指标卡（请求/输入/缓存读取/输出/平均TFT/平均总延迟）+ 按小时「请求量(Token 双面积)」混合图 + 平均 TFT 柱图（>1.5s 橙 / >3s 红）+ 模型分布与 Key 占比条形 + 近期错误卡；`/usage` 扩展返回 `hourly`（今天 24 小时 / 多日按日期铺满）与 `avg_tft`。
    - **明细 Tab**：`GET /console/api/usage/logs` 逐请求（上限 500 行）——**TFT = completionStartTime − startTime（生产 100% 可算）**、Token 与延迟为聚合双行列（↓输入 ↑输出 / ▣缓存读；首T + 总 + 双段迷你条，阈值着色）、Key 只显示别名、**全列可排序**、筛选 + 搜索 + 分页 + 详情抽屉（request_id/session_id/耗时分解/IP）；**↻ 刷新按钮只刷数据不刷页面**。
    - **思考强度列（2026-08-17）**：明细行展示该请求实际携带的思考参数——group_routing 钩子在 `async_pre_call_hook` 归一化后**原地**写入请求 metadata 的 `spend_logs_metadata.effort`（OpenAI `reasoning_effort`/`reasoning.effort` 原样；Anthropic `thinking.budget_tokens` → `budget:N`；`type=disabled` → `off`），历史行/未携带显示「—」。1.96.2 实测三坑：① 落库按 `SpendLogsMetadata.__annotations__` 白名单重建 metadata，`requester_metadata` 不在白名单、写库时被丢弃（custom logger 事件里能看到，DB 里没有）——`spend_logs_metadata`（自由键值槽）在白名单内，是唯一可靠落库通道；② `function_setup` 在钩子前把 `data["metadata"]` 同引用存进 Logging 对象，钩子里必须原地改写、整体替换会丢；③ `/v1/messages` 入口的代理 metadata 在 `litellm_metadata` 通道，钩子需两通道都写。已知局限：钩子在路由前，`drop_params` 对不支持上游的静默丢弃不可感知（记录的是请求携带值）。
    - **时区修正**：日志时间统一转 Asia/Shanghai(+08) 展示（此前直接切 UTC 字符串，差 8 小时）。
    - **客户端 IP（2026-08-15 二次调查后解决）**：LiteLLM 实为支持 XFF——`general_settings.use_x_forwarded_for: true`（config.yaml 已加）即记录 nginx 传来的 `X-Forwarded-For`（consoled 取首跳）；此前记的是 nginx 容器地址（172.18.x，历史行页面标注「经 nginx」）。仅当上游为可信反代时开启：litellm 端口只在 docker 网内可达，安全。已实测：工作站经公网调用，日志记录真实出口 IP。
   **本地集成实测（2026-08-15）**：4 镜像本地构建 + 4 容器栈（wireguard 用隔离 netns 冒烟）——admin 容器内登录、console→docker.sock→wg sidecar 的 `wg show`/`wg set peer` 链路、`/mcp/register` 触发 `docker restart private-llm-mcp-hub`（容器 StartedAt 实变）、external-mcp.json 跨容器共享写读、LiteLLM 缺席容错，全部通过。
11. **协议兼容层（2026-08-15，issue #9）**：nginx 与 LiteLLM 之间新增 compat-proxy（`compat/compat_proxy.py`，容器 `private-llm-compat:8400`，Starlette 单文件，随 compose 第 7 服务部署）。背景：agent-compat 矩阵实测 deepseek-v4-flash-0731 上 4 个 forced 用例畸形（`tool_choice=required`/`any` 不产生工具调用）、内联 `messages[].role=system` 被 LiteLLM 1.96.2 静默丢弃（issue #2）。**为何是独立代理而非 LiteLLM hook**：`group_routing.py` 的 `async_pre_call_hook` 位于 LiteLLM 请求解析之后，内联 system 到达时已丢失；独立代理在解析前规范化。**修复语义**：①内联 system 结构化块合并进最近前一条 user（无前置 user 原地转合成 user；顶层 `system`、tool ID、cache_control、thinking 不动；确定性——多轮重发前缀字节稳定，US-08 缓存互锁）；②单工具 forced 改写为指定该工具（探针实测可完整恢复）；③多工具 forced 稳定 400（`forced_tool_choice_unsupported`，OpenAI/Anthropic 各自错误格式；禁止静默删参或代选第一个）；④OpenAI 流式见过 tool_calls fragments 却报 stop → 改写为 `tool_calls`（逐 choice，仅重写该行）；⑤**DSML 参数规范化（部署中实测发现，探针未覆盖）**：vLLM（site-a deepseek）在 forced/指定函数路径把 DeepSeek 原生 `<｜DSML｜…>` 标记文本放进 `function.arguments`（auto 路径则是干净 JSON），第二轮历史回传时 vLLM 解析 arguments 即 400「Expecting value」——compat 在 OpenAI 非流式响应侧与请求侧 assistant 历史双侧把非法 JSON 的 DSML arguments 确定性转为 JSON（非 DSML 结构不猜测、原样透传）。**不变式**：无变换即原始字节透传；`Accept-Encoding: identity`；内部容器（mcp-hub/onboardd/console）仍直连 litellm 不经 compat；多工具 forced 待上游支持后移除 400 并回归矩阵。验证：`compat/test_compat.py` 30 项单测 + 本地 stub 端到端冒烟 15 项 + agent-compat 矩阵 16 项（含 2 项新增多工具 400 用例；inline_system 升级为硬性 PASS 条件）；TTFT 对比无回归（openai 715→751ms / anthropic 655→646ms，晚高峰噪声内）。

## 2. VPS 部署（一次性）

前置：DNS A 记录已指向 VPS；云安全组放行 443/tcp 与 51820/udp；`ssh your-vps` 可登；VPS 用户在 docker 组；**一次性 sudo**（#7 容器化后仅剩这些）：`sudo ufw allow 51820/udp`、BBR sysctl（`/etc/sysctl.d/99-private-llm-tunnel.conf`，已配置的 VPS 跳过）。

```bash
# 本地：同步代码上 VPS
rsync -av --exclude .env execution/proto-remote-access/ your-vps:~/LLM-Portal/

# VPS：
cd ~/LLM-Portal/vps
cp .env.example .env && vi .env       # LITELLM_MASTER_KEY=sk-$(openssl rand -hex 16)、ADMIN_* 等
./deploy.sh                            # docker 组用户执行，无需 sudo
```

deploy.sh 幂等；首次在存量部署上运行会自动停用旧 systemd 单元（该步若非免密 sudo 会提示手动执行；wg-quick 切换瞬间隧道短暂中断）；nginx 改动带备份与 `nginx -t` 失败自动回滚（不影响 sub2api 等既有站点）。

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

```bash
# VPS 上签发（例：site-a，注册两个模型端口）
site-add site-a --model deepseek-v4-flash-0731:8890 --model qwen3.6-35b-fp8:8004 --group default
# 输出：curl -fsSL "https://llm-portal.example.com/onboard/install?token=..." | sudo bash

# 站点机器（如 ssh site-a）执行上面一行；自检全绿即自动注册进路由池
# token 一次性、15 分钟过期；二次使用 → 403

site-list          # 站点清单（名称/公钥/WG IP/模型/分组/状态）
site-revoke site-a   # 吊销：wg 摘 peer + LiteLLM 摘 deployment + 状态标记
```

install.sh 在站点侧：装 wireguard-tools → `wg genkey`（私钥不出机）→ 公钥注册 → 写 `/etc/wireguard/wg0.conf` → `systemctl enable --now wg-quick@wg0`（自启自愈）→ 自检（ping 10.77.0.1 + 各模型端口 `/v1/models`）→ confirm 注册 deployment。

站点接入后的模型增删改走控制台「站点」页的 **模型** 按钮（无需重新走 install.sh）：

- **刷新上游**：站点换了模型（引擎/端口不变）时用——对外模型名不变、仅替换发往上游的 model id（先建新 deployment 再删旧，不留 404 窗口），订阅方与 Key 无需任何改动；
- **添加模型**：四步引导——① 端口从站点已知端口下拉选（后端 `known_ports` = deployment ∪ 登记簿，避免手打拼错 api_base；新端口才选「其他端口」手填）→ ② 探测该端口 `/v1/models`（llama.cpp/vLLM 均适用）→ ③ 点选上游真实 id → ④ 起/改对外模型名（点选 id 自动带出）；
- **删除**：把单个 deployment 摘出路由池（两段式确认）。以上操作均同步 onboardd 登记簿（`/onboard/admin/models`）。

## 4. 客户端接入

统一：`base_url = https://llm-portal.example.com`，`key = sk-<用户虚拟Key>`（管理员分发）。

| 客户端 | 配置 |
|---|---|
| OpenAI SDK | `base_url=https://llm-portal.example.com/v1`，model 直选 `deepseek-v4-flash-0731` / `qwen3.6-35b-fp8`（或别名 `claude-opus-5`、`qwen3.6-35b-a3`） |
| Claude Code | `ANTHROPIC_BASE_URL=https://llm-portal.example.com`，`ANTHROPIC_AUTH_TOKEN=sk-…`，默认模型名 `claude-opus-5`（别名已配） |
| Pi（badlogic/pi-mono） | `pi` 设置里 provider：baseUrl `https://llm-portal.example.com/v1`，apiKey `sk-…`，model 填对外名；MCP 需先 `pi install npm:pi-mcp-adapter` |
| MCP 客户端（Streamable HTTP + Bearer） | URL `https://llm-portal.example.com/mcp`，头 `Authorization: Bearer sk-…`；工具名 `[a-z0-9_]`（`analyze_image`、外部 MCP 前缀如 `zhipu_*`） |

本地图片两步式：`POST /mcp/upload`（multipart `file=`，同一 Key）→ 得临时 URL（30min）→ `analyze_image(url, 问题)`。

## 5. 验收记录（T1~T13，2026-08-14 首站 site-a 实测）

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
| T9 | US-P9 | Admin UI 建/禁 Key 即时生效；用量可筛 | ✅ `/ui` 可用（master key 登录）；建 Key 即时生效（home-key 实测） |
| T10 | US-P10 | `/key/info` 仅见自身用量 | ✅ 用户 Key 自查 200 |
| T11 | US-P11 | `/v1/models` 见全部对外名；未知名→400/404 | ✅ 4 个对外名（deepseek/qwen 直选 + claude-opus-5/qwen3.6-35b-a3 别名） |
| T12 | US-P12 | 外部 MCP 注册后 tools/list 前缀工具可用 | ◐ 框架就绪（配置文件 + 占位符过滤 + 前缀代理）；智谱真实凭据待录入验证 |
| T13 | US-P13 | Key 绑组仅组内路由；伪造 tag 无法越组；组内无部署→可判读错误 | ✅ 六项矩阵全过：home Key→组外模型 401 可判读、组内 200、伪造 x-litellm-tags 双向无效、未绑组全量 |
| T14 | US-P9 修订/P14 | 控制台全流程（2026-08-14 晚实测） | ✅ 登录三态（master→admin / 用户 Key→user / 错 Key 401，连错 5 次 60s 内 429）；user 访问管理 API 全 403、/my 数据真实（今日 133 次 + 分模型）；Key 建/禁/解禁/删全链路（blocked→chat 401、mcp 401，删除→401）；分组 create/rename/delete 的 retag 实效（/model/info tags 逐条核对）；站点 token 下发（900s + 安装命令）；别名创建（/v1/models 可见 + 调用 200）；MCP 注册/移除（配置 0600 + restart + 凭据只显尾 4 位 + 不可达服务优雅降级） |
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
# 证书：certbot 每日 cron 自动续期（已并入 renew.sh）
```

## 7. 密钥与安全（C1/C2/C5）

- master key / postgres 密码 / onboard admin token / 管理员邮箱密码与 TOTP 密钥：`vps/.env`（VPS 上 0600，不入库），compose 变量注入各容器；`/etc/private-llm/` 只剩 `external-mcp.json`（外部 MCP 注册表）。r6 起公网不再有任何接受 master key 的端点（控制台管理员登录走独立邮箱+密码+2FA 页，master key 仅服务端回环；用户登录用分配的虚拟 Key）。管理员登录连错 5 次/分钟锁定，与用户登录共用限速。
- **docker.sock 取舍（#7）**：console/onboardd 容器挂 `/var/run/docker.sock`（执行 wg peer 管理 / mcp-hub 重启），挂 sock 的容器 ≈ 宿主机 root——只给这两个管理面容器，且它们本身已是管理员权限面；其余容器（litellm/compat/postgres/mcp-hub/wireguard）不挂（compat 亦不持任何密钥，鉴权头原样透传）。
- WG 私钥：VPS `/var/lib/private-llm/wireguard-private.key`（0600）与 `/etc/wireguard/wg0.conf`（0600）；站点私钥仅站点本机。
- 用户 Key 永不出网关：mcp-hub 只用它调 `/key/info` 与回环 LiteLLM；上游无鉴权直连不带 Key。
- **Key 明文保险库（2026-08-15，管理员可再查）**：管理员需求「查看生成的 key，而非仅一次展示」——consoled 在创建时把明文 Fernet 加密存 `/var/lib/private-llm/console/keyvault.db`（密钥文件 `keyvault.key` 0600，独立于密文），`POST /console/api/keys/reveal`（仅管理员）解密取回；「使用」弹窗自动取回代入。**边界变化：网关成为密钥保管者**——VPS 失陷即密钥失陷（加密仅防离库拖走）；保险库启用前的旧 Key 只有哈希，reveal 404 提示重签；轮换保险库 = 删 `keyvault.key`（旧密文不可解，等同重签）。
- 公网面：443/tcp（nginx）、51820/udp（WG）、SSH；其余容器仅 127.0.0.1 回环发布或 compose 内网互通，不监听公网。
