# 安全策略（Security Policy）

本项目是面向中小企业自部署的统一 AI 网关（LiteLLM + 自写 console / onboardd / mcp-hub / compat + WireGuard 多站点隧道）。本文档说明：如何报告安全漏洞、系统的安全边界与已知取舍、以及部署时应完成的安全清单。部署架构详见 [`execution/proto-remote-access/README.md`](execution/proto-remote-access/README.md) 与 [`execution/proto-remote-access/docs/runbook.md`](execution/proto-remote-access/docs/runbook.md)。

## 支持的版本

| 版本 | 支持状态 |
|------|----------|
| `main` 分支（开源首个发布） | ✅ 支持 |
| 旧Tag/历史提交 | ❌ 不支持，请先升级到最新 `main` 再报告 |

## 报告漏洞

**请勿通过公开 issue / PR / 讨论区报告安全漏洞。**

推荐流程（私有 GitHub Security Advisories，优先）：

1. 打开本仓库 GitHub 页面 → **Security** 标签页 → **Report a vulnerability**（即私有 Security Advisories）；
2. 在表单中描述：问题类型、影响组件（litellm / console / onboardd / mcp-hub / compat / wireguard / nginx 配置 / 部署脚本）、复现步骤、影响评估（如可导致的越权 / 信息泄露 / RCE 范围）；
3. 若有 PoC 或补丁，可直接在 Advisories 中私下附件或等待维护者开通私有 fork 后提交。

备选渠道（邮件）：

- `johnymoo12@gmail.com`（维护者邮箱；建议后续配置 PGP 公钥并考虑迁移到专用 security 地址）

我们会在 **72 小时内**确认收到，修复进度通过 Advisory 私有通道同步；修复发布后再公开披露并致谢报告者（除非你要求匿名）。

报告前请先阅读下文「安全边界与已知取舍」——以下情形属于**已声明的设计取舍**，不算漏洞（除非你能证明其影响超出所述边界，例如 docker.sock 挂载容器存在未鉴权的公网可达端点）。

## 安全边界与已知取舍

### 服务与权限级别

部署形态为单 VPS 全容器栈（`execution/proto-remote-access/vps/docker-compose.yml`），各服务权限：

| 服务 | 权限级别 | 说明 |
|------|----------|------|
| console / onboardd | **高（≈宿主机 root）** | 挂载 `/var/run/docker.sock`：console 经 `docker exec` 管理 WireGuard peer、`docker restart` 重启 mcp-hub；onboardd 经 `docker exec` 管理 wg0。**有意取舍**：这两个容器本身就是管理员权限面（会话鉴权 / admin token），挂 sock 仅限这两个管理面容器 |
| compat | 低 | 无状态、不落盘、非 root（UID 10001）、不持任何密钥（鉴权头原样透传，鉴权/路由/记账仍归 LiteLLM） |
| litellm | 中 | 端口仅发布到 `127.0.0.1:4000`；master key 经 env 注入，公网无任何接受 master key 的端点 |
| postgres | 低 | 仅 compose 内网互通，无端口发布 |
| mcp-hub | 低 | 状态落宿主机 bind mount；不挂 docker.sock |
| wireguard sidecar | 高（NET_ADMIN） | host 网络 + NET_ADMIN 用于创建 wg0 接口；不挂 docker.sock |

### 网关凭据模型（三层）

1. **master key**（`LITELLM_MASTER_KEY`）：LiteLLM 管理密钥，r6 起公网收敛后**仅服务端内部与回环使用**（onboardd/consoled 调 LiteLLM、本机 CLI、`ssh -L 4000:127.0.0.1:4000` 应急通道）；控制台管理员登录走独立的邮箱+密码+2FA 页面，不接受 master key。
2. **用户虚拟 Key**：管理员在控制台签发给业务方/用户的 LiteLLM 虚拟 Key，用于 `/v1/*` API 鉴权、限额与记账；用户 Key 永不出网关（mcp-hub 只用它调 `/key/info` 与回环 LiteLLM）。
3. **Key 明文保险库**：为满足「管理员可再查明文 Key」需求，consoled 在创建 Key 时把明文 **Fernet 加密**落盘到 `/var/lib/private-llm/console/keyvault.db`（密钥文件 `keyvault.key` 0600，独立于密文），仅管理员经 `POST /console/api/keys/reveal` 解密取回。

### 公网暴露面（allowlist）

nginx（`vps/nginx/private-llm.conf`）按 allowlist 分发，**其余路径一律 404**：

- 允许：`/`（网关主页）、`/v1/*`（双协议 API；三条 compat 路径精确匹配接管）、`/key/info`（用户自查）、`/health/liveliness`、`/mcp*`、`/onboard/install|register|confirm`、`/console`；
- 拦截（404）：LiteLLM `/ui`、`/login`、`/sso`、`/openapi.json`、`/redoc`、`/key/generate` 等全部管理 API、`/onboard/admin/*`；
- 所有容器服务端口仅发布到 `127.0.0.1`（4000/8100/8200/8300/8400），公网只开 80/443/tcp 与 51820/udp（WireGuard）。

### 已知取舍（明确声明，非漏洞）

1. **挂 docker.sock 的容器 ≈ 宿主机 root**：console/onboardd 若被攻破（含容器内任意代码执行），攻击者可借 sock 控制宿主机。取舍理由：管理面本身已是最高权限面，避免更复杂的 IPC 方案；缓解措施：两容器均不直接暴露公网原始端口（经 nginx 鉴权路径或回环），公网收敛自检持续覆盖。
2. **VPS 失陷即保险库失陷**：keyvault 的 Fernet 加密仅防「离库拖走密文」（密钥文件与密文分文件、均 0600），攻击者拿到整台 VPS 即可解密全部用户 Key 明文。**轮换保险库 = 删 `keyvault.key` 重签**（旧密文不可解，等同全部 Key 重签）；保险库启用前的旧 Key 只有哈希，无法取回明文。
3. **上游站点模型无鉴权直连**：网关经 WireGuard 隧道访问各站点内网推理服务（vLLM/llama.cpp），信任边界是 WG 隧道与站点内网，上游服务自身不设鉴权。
4. **compat 无状态透传**：不校验、不改写鉴权头，鉴权完全由 LiteLLM 承担——compat 自身失陷不泄露密钥，但可窃听经它的流量（部署上它仅在 compose 内网）。
5. **单机部署**：全部组件在同一 VPS，无租户级物理隔离；多租户强隔离不在本项目当前范围。

## 部署安全清单

按 `execution/proto-remote-access/docs/runbook.md` 部署时，逐项确认：

1. **防火墙 / 云安全组**：仅放行 `80/tcp`、`443/tcp`、`51820/udp`（WireGuard）与 SSH（建议限源 IP）；其余一律拒绝。云安全组与主机 ufw 两层都要查——WireGuard 端口漏放会导致隧道不通（历史踩坑见 runbook §1）。
2. **强凭据生成**：复制 `vps/.env.example` 为 `.env` 后，全部 `REPLACE_ME` 用随机值替换（**禁止**使用示例值/弱口令）：
   ```bash
   openssl rand -hex 16 | sed 's/^/sk-/'   # LITELLM_MASTER_KEY（LiteLLM 要求 sk- 前缀）
   openssl rand -hex 16                     # POSTGRES_PASSWORD
   openssl rand -hex 24                     # ONBOARD_ADMIN_TOKEN
   openssl rand -base64 18                  # ADMIN_PASSWORD（管理员初始密码）
   ```
   `.env` 文件权限 0600、不入库（已 gitignore）。
3. **管理员 2FA**：部署后在 `/console/2fa.html` 扫码启用 TOTP（或预置 `.env` 的 `ADMIN_TOTP_SECRET`，生成方式见 `.env.example`）；master key 不再作为网页登录方式（配置 `ADMIN_EMAIL` 后自动生效）。
4. **公网收敛自检**：`deploy.sh` 步骤 7b 自动执行；手动复查（无 `!!` 输出即通过）：
   ```bash
   for path in /ui /login /sso /openapi.json /key/generate /onboard/admin/list /spend/logs /team/list; do
     curl -s -o /dev/null -w "%{http_code} $path\n" "https://<你的域名>$path"   # 应全部 404
   done
   curl -s -o /dev/null -w "%{http_code}\n" "https://<你的域名>/v1/models"       # 应 401（未带 Key）
   ```
5. **敏感文件确认**：`/var/lib/private-llm/wireguard-private.key`、`/etc/wireguard/wg0.conf`、`/var/lib/private-llm/console/keyvault.key` 均 0600；`.env` 从未提交进 git（CI 含 gitleaks 全历史扫描兜底）。

## 范围外

- 各站点（上游模型机器）自身的系统安全与站点内网安全；
- 客户端机器与用户虚拟 Key 的本地保管；
- LiteLLM / postgres / nginx 等上游组件自身的漏洞——请直接报告上游项目，我们跟进升级（Dependabot 周更依赖）。
