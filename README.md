# LLM-portal

[English](README.en.md) | **中文**

面向**中小企业**的自部署统一 AI 网关（Unified AI Gateway）：在一台公网 VPS 上聚合散布于
多个内网站点的私有推理模型，以 **OpenAI / Anthropic 兼容 API** 统一对外，并提供虚拟密钥、
分组路由、用量计量与 Web 管理控制台。

> **状态：Alpha。** 当前版本已用于实际部署，但接口、配置和升级流程在首个稳定版前仍可能调整。
> 已知限制与后续方向见 [`ROADMAP.md`](ROADMAP.md)。

## 核心能力（MVP 已实现）

- **双协议入口**：OpenAI `/v1/chat/completions` 与 Anthropic `/v1/messages`（含
  count_tokens、SSE 流式、tool calls）；协议兼容层 `compat` 处理 Anthropic 工具链的
  已知兼容性问题（内联 system 合并、强制工具选择改写、OpenAI 流式 finish_reason 修正）。
- **多站点接入**：站点（内网机器）经 **WireGuard** 公钥隧道主动连出，零公网暴露；
  `site-add` 一键签发接入命令，站点侧一条 curl 完成安装注册。
- **模型路由**：LiteLLM 统一模型名 → 上游 deployment，least-busy 分流、故障 60s 冷却、
  模型别名；站点分组（tag）与 Key 绑定分组实现「按 Key 路由到指定站点组」。
- **虚拟密钥**：签发/禁用/删除/模型白名单/分组绑定；明文仅签发时展示一次，管理端可再查
  （Fernet 加密保险库）。
- **用量计量**：按 Key / 模型 / 时间的 token、缓存命中、首 token 延迟、逐请求明细。
- **管理控制台**：管理员邮箱+密码+TOTP 两步验证登录；站点/分组/模型/Key/用量/外部 MCP
  全图形化管理；用户凭虚拟 Key 查自己的用量。
- **MCP 工具面**：内建图像理解工具 + 外部 MCP 代理注册（凭据不出网关）；外部工具可绑定
  一个或多个分组，由用户 Key 的 `metadata.group` 同时约束 `tools/list` 与 `tools/call`。
- **公网收敛**：nginx allowlist——LiteLLM 管理面 `/ui`、管理 API 一律 404，仅暴露业务路径。

## 架构

```text
                     ┌────────────────────────── VPS（公网，docker compose）──────────────────────────┐
 客户端（OpenAI SDK / │  nginx 443 (allowlist) ── compat(8400) ──┐                                    │
 Claude Code / pi …）─┼─► /v1/messages|chat ──────────────────────┼─► litellm:4000 ── postgres        │
                     │  /console ──► consoled(8300) ◄─ docker.sock │        │                           │
                     │  /mcp ──────► mcp-hub(8200)                │   WireGuard wg0 (10.77.0.1/24)    │
                     │  /onboard/* ─► onboardd(8100) ◄─ docker.sock        │                           │
                     └────────────────────────────────────────────────────┼───────────────────────────┘
                                                                                │ 公钥隧道（站点主动外连）
                                          ┌───────────────────────────────────┴──────────────────────┐
                                          │ 站点 A（内网）: vLLM :8890 / llama.cpp :8004 …（OpenAI 兼容）│
                                          │ 站点 B …（多站点经 WG 虚拟 IP 寻址，LAN 网段互不冲突）        │
                                          └───────────────────────────────────────────────────────────┘
```

七个核心 compose 服务：`litellm`（路由/鉴权/记账，仅回环发布）、`compat`（协议兼容层，非 root）、
`postgres`、`mcp-hub`、`onboardd`（站点注册）、`console`（管理面）、`wireguard` sidecar；
standalone 模式另启 `edge-nginx` + `edge-certbot`。也可复用既有 nginx（external），或由局域网
上游设备终结 TLS、仅启 HTTP edge-nginx（offload）。

## 快速开始

前置：一台公网 VPS（域名解析到位、放行 80/443/tcp 与 51820/udp）、docker 组权限用户。

```bash
git clone https://github.com/shiliai/LLM-Portal.git && cd LLM-Portal/vps
cp .env.example .env && vi .env     # 填 LITELLM_MASTER_KEY / POSTGRES_PASSWORD / DOMAIN / ADMIN_* 等
./deploy.sh                         # 幂等：wg 引导 + compose + 证书 + nginx + 冒烟收敛自检
```

签发站点接入命令（VPS 上执行，输出拷到内网机器执行）：

```bash
site-add my-site --model my-model:8000 --group default
# 输出：curl -fsSL "https://<域名>/onboard/install?token=..." | sudo bash
```

客户端接入（OpenAI 兼容；Anthropic 工具把 base_url 指向网关即可）：

```bash
curl https://llm-portal.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk-你的虚拟Key" -H "Content-Type: application/json" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hi"}]}'
```

详细部署步骤、验收表与故障处置见 [`docs/runbook.md`](docs/runbook.md)。

## 配置

全部环境变量见 [`vps/.env.example`](vps/.env.example)（真实值只放 `.env`，不入库）。
要点：

- `EDGE_MODE=standalone`（默认）：本栈自带 edge-nginx/edge-certbot 发布 80/443，全新 VPS
  零依赖；`external`：复用既有 nginx 容器（另设 `EDGE_NGINX_CONTAINER` / `NGINX_CONF_DIR` /
  `CERTBOT_DIR`）；`offload`：上游设备终结 TLS，本栈只发布 HTTP 80，需同步设置 `PUBLIC_BASE`
  与 WireGuard 直连地址 `WG_ENDPOINT_HOST`。
- `LITELLM_MASTER_KEY` 仅用于管理；生产环境必须配置 `ADMIN_EMAIL` / `ADMIN_PASSWORD`，使网页
  管理改走独立账号 + 可选 TOTP，并关闭旧版 master key 网页登录兼容路径。
- WireGuard：`WG_PORT` / `WG_SUBNET` / `WG_VPS_IP`，站点侧模板见 `vps/wireguard/`。

## 测试与开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
make test               # 全部单测（compat + console + onboardd + mcp-hub）
make lint               # py_compile 全部入库 .py
make compose-validate   # docker compose 配置校验
```

console 浏览器 E2E（Playwright + mock LiteLLM，不依赖真实部署）：
见 [`console/e2e/README.md`](console/e2e/README.md)。CI（单测/语法/Compose 校验/
secret 扫描/依赖审计）见 `.github/workflows/ci.yml`。

## 目录结构

| 目录 | 内容 |
|------|------|
| `vps/` | Compose、部署/发布/回滚脚本、nginx、LiteLLM 与 WireGuard 配置 |
| `console/`、`onboardd/`、`mcp-hub/`、`compat/` | 自研服务、依赖与测试 |
| `site-tools/` | 站点签发、查看和吊销命令 |
| `docs/` | 面向部署者的运行手册 |
| `tools/` | 兼容性实验与诊断工具，不进入运行时容器 |

## 安全边界（摘要）

- console / onboardd 挂载 docker.sock（≈宿主机 root）——管理面有意取舍，详见 `SECURITY.md`；
  compat 无状态非 root 运行。
- 密钥模型：配置 `ADMIN_EMAIL` 后 master key 仅回环；若留空会启用旧版 master key 网页登录兼容
  模式，因此生产部署必须配置管理员邮箱和密码。用户虚拟 Key 明文只在签发时返回一次（加密保险库可再查）；
  会话库只存哈希。
- 上游模型服务在隧道内网且默认无鉴权——**安全边界是 WireGuard 隧道**，请勿将上游端口
  暴露到站点局域网之外。
- 漏洞报告与完整边界说明见 [`SECURITY.md`](SECURITY.md)。

## 路线图

当前限制、近期优先级和长期方向统一维护在 [`ROADMAP.md`](ROADMAP.md)。路线图用于说明方向，
具体范围和排期以关联 issue 为准。

## 贡献与许可

- 贡献指引：[`CONTRIBUTING.md`](CONTRIBUTING.md)（含「禁止提交真实密钥/内网拓扑」红线）。
- 许可证：Apache License 2.0（见 [`LICENSE`](LICENSE)）。
