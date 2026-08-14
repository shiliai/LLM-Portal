# Runbook：private-llm 网关部署与运维

> 交付物：`execution/proto-remote-access/`（设计文档 §10）
> 设计：`docs/superpowers/specs/2026-08-14-remote-model-access-prototype-design.md`（proto-r5）

## 1. 拓扑与端口

| 组件 | 位置 | 端口 | 说明 |
|---|---|---|---|
| nginx（既有 `nginx-sub2api` 容器） | 公网 | 443/tcp | 单入口：`/v1`、`/ui` → litellm；`/mcp*` → mcp-hub:8200；`/onboard/*` → onboardd:8100；SSE 不缓冲 |
| LiteLLM Proxy（容器） | 回环 | 127.0.0.1:4000 | 双协议 API（`/v1/chat/completions` + `/v1/messages`）、别名、least-busy 分流、虚拟 Key、Admin UI `/ui` |
| Postgres（容器） | compose 内 | 无主机端口 | LiteLLM Key/用量/deployment 存储（`STORE_MODEL_IN_DB=True`） |
| mcp-hub（host systemd） | 0.0.0.0:8200 | ufw 拦公网 | `/mcp`（Streamable HTTP）+ `/mcp/upload` + `/mcp/files/*` + `/mcp/usage` |
| onboardd（host systemd, root） | 0.0.0.1:8100→0.0.0.0:8100 | ufw 拦公网 | 站点注册：install/register/confirm + admin API |
| WireGuard wg0（host 内核） | 公网 | 51820/udp | 10.77.0.1/24，站点从 .11 递增；未注册公钥内核静默丢弃 |

与设计的差异（环境适配，其余逐字落地）：
1. **Caddy → 既有 nginx + certbot**：VPS 443 已由 nginx-sub2api 服务多域名，追加 server block 而非另起入口；SSE 用 `proxy_buffering off` 等价 Caddy `flush_interval -1`。
2. **qwen 实际模型名 `qwen3.6-35b-fp8`**（llama.cpp 实报）；基线口径名 `qwen3.6-35b-a3` 作别名并存。
3. **US-P13 分组 tag 语义按 LiteLLM 1.96.2 实测校准**：① `enable_tag_filtering` 路由器级配置实测未生效，钩子改为每请求强制注入 `enable_tag_filtering=True`；② 带 `default` tag 的 deployment 会被实现当作「tag 无匹配时的兜底池」，与基线「组内无部署→报错」冲突——deployment 一律**不打 default tag**（default 组 = 隐式全量池），绑组 Key 由钩子注入组 tag、未绑组 Key 由钩子清空 tags（顺带清除客户端伪造的 `x-litellm-tags`）。Key 的分组仍存 `metadata.group`（default 视同未绑）。
4. **wstunnel 过渡通道已移除**：部署当日因云安全组未放行 51820/udp 临时用 wstunnel（UDP-over-WS 走 443）打通，后被腾讯云主机安全标记为 Risktool（Linux.Risktool.Wstunell.Agow），按安全策略双端移除（服务/二进制/nginx 路径/uffw 规则全部清除），恢复设计原方案的直连 WG UDP。

## 2. VPS 部署（一次性）

前置：DNS A 记录已指向 VPS；云安全组放行 443/tcp 与 51820/udp（ufw 由 deploy.sh 处理）；`ssh vps-tencent-tokyo` 可登。

```bash
# 本地：同步代码上 VPS
rsync -av --exclude .env execution/proto-remote-access/ vps-tencent-tokyo:~/private-llm-src/

# VPS：
cd ~/private-llm-src/vps
cp .env.example .env && vi .env       # LITELLM_MASTER_KEY=sk-$(openssl rand -hex 16) 等
sudo ./deploy.sh
```

deploy.sh 幂等；nginx 改动带备份与 `nginx -t` 失败自动回滚（不影响 sub2api 等既有站点）。

### 建用户 Key（C3：管理员创建分发）

```bash
source /root/../etc/private-llm/onboardd.env 2>/dev/null || source ~/private-llm-src/vps/.env
# 默认组（全部 provider）
curl -s http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'content-type: application/json' \
  -d '{"key_alias":"pi-local","metadata":{"group":"default"}}'
# 绑定分组（US-P13）：先建带 tag 的 deployment 分组，再把 Key 的 metadata.group 设为组名
# Admin UI：https://<域名>/ui （master key 登录）可视化完成同样的操作
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

**部署当日实测补充**：本地 pi CLI（badlogic/pi-mono 0.84.1）以 `private-llm` provider（baseUrl `https://private-llm.onlyservice.io/v1`）直调 deepseek 与 qwen 均通过——US 的「本地 pi 直接调用网关模型」目标达成。

## 6. 日常运维

```bash
# 日志
journalctl -u mcp-hub -u onboardd -f
docker logs -f litellm
# 模型/Key 管理：https://<域名>/ui（master key）
# 重启组件
sudo systemctl restart mcp-hub onboardd
cd ~/private-llm-src/vps && sudo ./deploy.sh   # 幂等升级
# 证书：certbot 每日 cron 自动续期（已并入 renew.sh）
```

## 7. 密钥与安全（C1/C2/C5）

- master key / postgres 密码 / onboard admin token：`vps/.env`（VPS 上 0600，不入库）+ `/etc/private-llm/*.env`。
- WG 私钥：VPS `/var/lib/private-llm/wireguard-private.key`（0600）与 `/etc/wireguard/wg0.conf`（0600）；站点私钥仅站点本机。
- 用户 Key 永不出网关：mcp-hub 只用它调 `/key/info` 与回环 LiteLLM；上游无鉴权直连不带 Key。
- 公网面：443/tcp（nginx）、51820/udp（WG）、SSH；其余全部 ufw DROP + 仅回环/compose 网内监听。
