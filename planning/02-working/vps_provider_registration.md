# VPS 部署下的 Provider 注册设计：本地 vLLM + 上游 provider 统一暴露

> 来源：01-raw/sub2api（Go 后端）与 01-raw/claude-relay-service（Node 后端）源码精读，2026 年 8 月 13 日
> 场景：LLM-portal 部署在一台 VPS 上，注册「本地 vLLM provider」与本机/远程「上游 provider」，统一对外暴露给任何可访问该 VPS 的客户端
> 关联文档：sub2api_analysis.md（既有）、token_love_product_spec.md、user_story_baseline_r2.md（US-03 / US-10 / US-11）

## 一句话结论

**provider 注册在 VPS 场景下 = 每条上游管理「base_url + 协议能力 + 凭据」三元组，再经 US-03 模型映射统一暴露在一个入口（`0.0.0.0` + HTTPS 反代）下。本地 vLLM 与远程 upstream 没有本质区别——都是待映射的上游，唯一特殊点是本地要用 `127.0.0.1`/内网 base_url，这需要显式的 SSRF 放行开关（现成参考：sub2api 的 `allow_private_hosts`）与 DNS-rebinding 防护。CRS 因上游写死不适用作 provider 注册参考；sub2api 的 urlvalidator 值得照搬思路（不搬代码，LGPL）。**

## 先厘清两个参考项目的定位差异（决定了谁能抄谁能不抄）

| | sub2api | claude-relay-service (CRS) |
|---|---|---|
| 定位 | AI API **网关**，支持多类上游（OAuth 订阅账号 + API Key），上游 URL 可配置 | 订阅账号**池化中转**（Claude Pro/Max、Gemini、OpenAI、Bedrock、Azure、Droid） |
| 后端 | Go (Gin + Ent + Wire) | Node.js |
| 硬依赖 | PostgreSQL 15+ + Redis 7+ | Redis 6+ |
| 上游 URL | **用户可配置** + SSRF 出站校验 | **写死官方端点**，无「自主注册任意 provider」能力 |
| 许可证 | LGPL-3.0 | MIT |

**关键判断：**

- **CRS 不是 provider 注册的参考**。它的账户类型（`claudeAccountService` / `openaiResponsesAccountService` / `geminiAccountService` / `bedrockAccountService` / `azureOpenaiAccountService` / `droidAccountService` 等）各自对接**固定的官方端**——已核对 `claudeAccountService.js` 硬编码直连 `https://api.anthropic.com/api/...`。它没有「用户自填 base_url、注册任意 provider」的入口（要把上游指到本机 vLLM，除非新增账户类型）。它解决的是「订阅池化」，不是「统一接入任意 provider」。
- **sub2api 更接近需要的模型**：可配置上游 URL + 一套完整 SSRF 防出站校验（`backend/internal/util/urlvalidator/validator.go`），正是「注册 provider」要面对的边界问题。但它是「账号池」语义，不是「企业统一网关」语义。
- **贴合的机制其实是 LLM-portal 自己的 US-03 模型映射**（对外模型名 → 上游供应商/模型）。两个仓库给出的是**参考实现 + 边界警示**，不是要照搬的基座（与 `02-working/sub2api_analysis.md` 既有结论一致；LGPL 亦不适合搬代码）。

## 核心模型：provider 注册 = 「URL + 协议 + 凭据」

注册任意 provider（本地 vLLM 或远程 upstream），抽象上都是管理：

```text
base_url           上游 HTTP 地址（含 scheme/端口/路径）
协议能力            chat_completions(OpenAI) / messages(Anthropic) / responses
凭据               API Key 或 OAuth 账号
安全标记            允许私网/回环？仅 HTTPS？URL 白名单？
```

在 VPS 上统一后的拓扑：

```text
客户端(可访问 VPS) ──统一入口──> LLM-portal ──US-03 映射表──> 上游 provider
   OpenAI /v1/chat/completions                └→ vLLM(本机)  http://127.0.0.1:8001/v1   [OpenAI兼容]
   Anthropic /v1/messages                     └→ OpenAI      https://api.openai.com/v1   [OpenAI兼容]
   Responses /v1/responses                    └→ Anthropic   https://api.anthropic.com/v1 [Anthropic原生]
                                               └→ sub2api     http://<vps>:8080           [Anthropic/OpenAI兼容]
                                               └→ CRS         http://<vps>:3000/claude    [Anthropic]
```

对外**只有一个入口**（VPS 上 80/443 或网关端口），客户端只需一个虚拟密钥 + base_url 指向 VPS，无感知底层是哪个 provider——这就是「统一」的全部含义。

## 设计点 1：本地 vLLM 的 base_url 写法

vLLM 跑同机时用回环地址而非机 IP：

| 场景 | base_url |
|---|---|
| 同机 Docker（vLLM 容器 + portal 容器同桥） | `http://vllm:8000/v1` 或 `http://127.0.0.1:8000/v1`（随网络模式） |
| 同机裸进程 | `http://127.0.0.1:8000/v1` |
| 局域网另一台机 | `http://<内网IP>:8000/v1` |

这引出唯一只有「本地 provider」才有的问题：**安全校验必须与远程 upstream 区分对待**。

## 设计点 2（sub2api 直接警示）：SSRF 出站校验

sub2api 在**注册任何上游 URL 时**默认封禁 `localhost`/私有网段 IP（`validator.go` 的 `isBlockedHost`：回环/私网/链路本地/未指定地址全部拒），并提供 `security.url_allowlist.allow_private_hosts` 开关显式放行（`config.go:804`）。它意图清晰：

- **远程 upstream** → 严格校验（防 SSRF：拒回环/私网/链路本地 IP，仅 HTTPS，可选 allowlist）。
- **本地 vLLM** → 必须**显式开启** `allow_private_hosts`（base_url 就是 `127.0.0.1` 或内网 IP）。
- **DNS Rebinding 防护**：`ValidateResolvedIP`（`validator.go:108`）**在实际发请求时**再解析 IP 校验，避免「注册时校验通过、请求时被 DNS 重绑定到内网」的 TOCTOU 漏洞。sub2api 在 `!AllowPrivateHosts` 时才启用该项（`http_upstream.go:579` `shouldValidateResolvedIP()`）。

**落地建议（写入 US-03 边界/安全约束，可入 r3）：**

1. provider 表单加「允许私网/回环地址」显式开关（或按 provider 类型如 `vllm-local` 自动放行），默认对 upstream 关闭。
2. 一旦放行 `127.0.0.1`/内网即丧失防 SSRF 天然屏障——本地 provider 只应由管理员配置（C2 单管理员约束天然满足）。
3. 建议在**请求发出时**做解析后 IP 校验，而非只在保存时校验（sub2api 做法）。
4. `allow_insecure_http`：本地 vLLM 走 HTTP 合理；远程 upstream 生产环境应只允许 HTTPS（sub2api 生产建议 `allow_insecure_http: false`）。
5. 远程 URL 白名单可支持 `*.example.com` 通配（`isAllowedHost`，sub2api 实现）。

## 设计点 3：协议能力决定 provider 能挂到哪个入口

vLLM 暴露 **OpenAI 兼容 `/v1`**，正好落在 **US-01（OpenAI 入口）**，且无需跨协议转换——归「OpenAI 兼容上游」即可被 `/v1/chat/completions` 直接路由。

注意 **US-11 边界**：OpenAI Responses 入口只做受管透传、不跨协议转换。因此：
- 本地 vLLM（OpenAI chat 兼容）→ 供 `/v1/chat/completions`（US-01）
- 若要响应 `/v1/responses`（US-11），取决于 vLLM 是否实现了 Responses 端点；未实现则对 Responses 入口返回「该映射不支持此入口」的可诊断错误。

**注册元数据应含「协议能力」字段**（chat_completions / messages(anthropic) / responses），使 portal 能判断「哪个入口可路由到该 provider」，避免静默失败（US-01/03 的错误可诊断要求）。

## 设计点 4：对外统一接口的监听 / 暴露方式

对「任何可访问 VPS 的客户端」，三层：

1. **服务绑定**：portal 监听 `0.0.0.0:端口`（非仅 `127.0.0.1`），否则外部客户端连不上。CRS 用 `0.0.0.0:3000`、sub2api 用 `0.0.0.0:8080` 均如此。
2. **反向代理 + TLS（生产推荐）**：Caddy 或 Nginx 把 `https://<域名>` 反代到本机 portal 端口，portal 进程只监听本机。CRS README 给了完整 Caddy/Nginx 配置，其中对 portal 直接可用的关键点：
   - **SSE 流式必须**：`proxy_buffering off`（Nginx）、`flush_interval -1`（Caddy）、长超时 `read_timeout/write_timeout 300s`、`proxy_request_buffering off`。
   - Nginx 需在 `http` 块加 `underscores_in_headers on;`（否则丢弃带下划线的头，破坏粘性会话类头）。两个项目 README 都强调了这一点。
3. **防火墙只开 80/443**，隐藏服务端口。

## 设计点 5：sub2api / CRS 也可作为 portal 的「上游」

`sub2api_analysis.md` 既有结论：用户现网在跑 sub2api，MVP 演示链路 `LLM-portal → sub2api → 上游`。VPS 部署时：

- 本地 vLLM、sub2api、CRS 可能都与其 portal 在**同一台 VPS**。它们对 portal 都是「上游 HTTP 服务」：sub2api 暴露 `http://<vps>:8080`（Anthropic 原生/OpenAI 兼容），CRS 暴露 `http://<vps>:3000/claude`（Anthropic）或 `/openai`（Responses）。
- 统一注册的 provider 清单可能是：本地 vLLM、sub2api、CRS、Anthropic 官方、OpenAI 官方……这些都可能「指向 VPS 自身」→ 落入需要 `allow_private_hosts` 的本地分支；若与 portal 非同机或走域名访问，则是远程分支。
- **不要在「本地/远程」上做死二分**，而用「base_url 是否可回环/私网解析」动态判断——这正是 SSRF 校验的目的。

## 落地建议清单（供进入 03-core 评审）

1. **Provider 抽象**：provider 注册建模为 `base_url + 协议能力 + 凭据 + 安全标记`，与 US-03 模型映射解耦（先有 provider，再映射对外模型名）。
2. **协议能力字段**：区分 vLLM(chat_completions)、Anthropic(messages)、responses，决定 provider 可被哪个入口路由（US-01/02/11），对不支持入口返回可诊断错误。
3. **本地 provider 的 SSRF 例外**：借鉴 sub2api——默认防 SSRF（拒回环/私网/链路本地 + 远程仅 HTTPS），本地 vLLM 显式放行；建议请求时做 resolved-IP 校验防 DNS rebinding。建议作为新约束 C 入 r3，并补进 US-03 验收。
4. **反代层放流式**：SSE 场景 Caddy/Nginx `proxy_buffering off` + 长超时（复用 CRS README 的 Nginx advanced 配置）；Nginx `underscores_in_headers on`。
5. **部署拓扑**：VPS 上 portal 监听 `0.0.0.0`，外部经 Caddy/Nginx(HTTPS) 反代；portal 内上游 registry 统一管理本地 vLLM 与远程 upstream。

## 对 LLM-portal 的启示（一句话）

Provider 注册在 VPS 场景下没有新架构——它就是「每条上游 = base_url + 协议能力 + 凭据」再加一层 US-03 映射，本地 vLLM 相对远程 upstream 的唯一额外成本是**显式 SSRF 放行 + resolved-IP 校验**。CRS 上游写死，参考价值在反代放流式与 SSH-aware 配置；sub2api 的 urlvalidator（allow_private_hosts + 通配白名单 + DNS-rebinding 防护）是 provider 注册安全边界最值得照搬思路的现成实现（只读思想，不搬 LGPL 代码）。
