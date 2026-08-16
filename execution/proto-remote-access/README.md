# proto-remote-access：远程访问私有推理模型 MVP

按设计文档 `docs/superpowers/specs/2026-08-14-remote-model-access-prototype-design.md`（proto-r5 基线）实现。

**架构**：LiteLLM Proxy（双协议入口/别名/least-busy 分流/虚拟 Key，Docker）+ 自写 mcp-hub（视觉 MCP + 外部 MCP 代理）+ WireGuard 多站点公钥隧道 + 自写 onboardd/site-add 一键接入 + 既有 nginx 443 单入口（本 VPS 已有 nginx+certbot，等价替代设计中的 Caddy）。

```text
├── vps/                    # VPS 侧部署物
│   ├── deploy.sh           # 一键部署：wg0 + 证书 + nginx + compose + systemd
│   ├── docker-compose.yml  # litellm + postgres（litellm 只发布到 127.0.0.1:4000）
│   ├── .env.example        # LITELLM_MASTER_KEY / POSTGRES_PASSWORD / ... 占位
│   ├── nginx/private-llm.conf       # nginx server block（SSE 不缓冲）
│   ├── litellm/config.yaml          # 静态别名 + 路由 + tag 过滤
│   ├── litellm/group_routing.py     # US-P13：Key→分组→路由 tag 注入钩子
│   └── wireguard/wg0.conf.example   # VPS 侧 wg0 模板
├── mcp-hub/                # /mcp Streamable HTTP + /mcp/upload + /mcp/usage
├── onboardd/               # 站点注册 API（token/register/confirm + admin）
├── site-tools/             # site-add / site-revoke / site-list / install.sh.tpl
└── docs/runbook.md         # 部署步骤 + T1~T13 验收表 + 四客户端接入示例
```

## 与设计的差异（环境适配，详见 docs/runbook.md §1）

1. **Caddy → 既有 nginx + certbot**：VPS 443 已由 nginx-sub2api 容器（certbot webroot 续期）服务多域名，新增 server block 即可；SSE 用 `proxy_buffering off` 等价 `flush_interval -1`。
2. **qwen 实际模型名 `qwen3.6-35b-fp8`**（llama.cpp 实报）；基线口径名 `qwen3.6-35b-a3` 作别名并存。
3. **US-P13 分组语义按 LiteLLM 1.96.2 实测校准**：路由器级 `enable_tag_filtering` 未生效 → 钩子每请求强制注入；`default` tag 是实现的兜底池语义 → deployment 不打 default tag（default 组=隐式全量池）；钩子对绑组 Key 注入组 tag、未绑组清空 tags（清除客户端伪造的 `x-litellm-tags`）。
4. **云安全组须放行 51820/udp**（WireGuard 隧道端口）；部署当日曾因未放行临时用 wstunnel 过渡，后被主机安全标记 Risktool 遂移除，恢复直连 WG UDP。

## 部署状态（2026-08-14）

- VPS `llm-portal.example.com`：litellm（容器，127.0.0.1:4000）+ postgres（compose 内网）+ mcp-hub/onboardd（systemd）+ wg0（10.77.0.1/24）全量上线。
- 站点 site-a（192.0.2.10，WG IP 10.77.0.11）：`deepseek-v4-flash-0731`（:8890）与 `qwen3.6-35b-fp8`（:8004）已注册进路由池；对外名共 4 个（含 `claude-opus-5`、`qwen3.6-35b-a3` 别名）。
- 验收 T1~T13 绝大部分通过（双站点分流与外部 MCP 实凭据两项待后续），本地 pi 已直连网关调用。
- **性能（2026-08-14 晚调优后）**：短请求网关开销 0.58s ≈ 2 RTT + LiteLLM 0.2s；8K/32K-token 热 prompt 增量 +1.1s/+2.1s（隧道丢包 43% 最差窗口实测）。调优 = 四端 BBR + wg MTU 1280 + TCP 缓冲（详见 runbook §1.5 与 issue #6）。

## 快速部署

VPS 侧（详见 `docs/runbook.md`）：

```bash
cd execution/proto-remote-access/vps
cp .env.example .env && vi .env        # 填 master key / postgres 密码 / 域名
sudo ./deploy.sh                       # wg0 + 证书 + nginx + litellm compose + systemd 服务
```

站点侧（在 VPS 上签发，拷到站点机器执行）：

```bash
site-add site-a --model deepseek-v4-flash-0731:8890 --model qwen3.6-35b-fp8:8004 --group default
# 输出：curl -fsSL "https://<域名>/onboard/install?token=..." | sudo bash
```
