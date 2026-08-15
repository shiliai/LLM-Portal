# MCP 客户端兼容性调研（US-P4 视觉 MCP 可移植性）

> 日期：2026-08-14
> 目的：US-P4 的网关托管视觉 MCP（`/mcp` 暴露 `analyze_image`）不能只对着 Claude Code 设计，需覆盖多 agent harness。本文给出 4 个客户端的传输/鉴权/工具名/模型协议矩阵，作为 handoff 时设计文档 §3.3 / §3.5 / US-P4 / US-P12 修订的依据。
> 来源口径：**仅采信一手资料**（官方 docs、GitHub README/源码、release notes）；内容农场博客不计入，凡只能从博客得到的结论标 LOW。
> 状态：**已并入设计文档**（2026-08-14 handoff 修订：US-P3 spike PASS + US-P13 加固 + US-P4 可移植集，见设计 §3.1/§3.3/§4 流3/§12）。

## 兼容矩阵

| 客户端 | 远程 Streamable HTTP？ | 其它传输 | 鉴权机制 | 工具名约束 | 模型协议（到网关） |
|---|---|---|---|---|---|
| **Claude Code**（Anthropic CLI） | 是（high） | stdio、SSE(弃用) | `headers: {"Authorization":"Bearer …"}` 于 `.mcp.json`；OAuth 2.1 自动发现。头名固定 `Authorization`。**已知怪癖**：偶发忽略静态 Bearer 转而发起 OAuth（anthropics/claude-code#47424，med） | `mcp__<server>__<tool>` 前缀；identifier-safe（high） | 仅 Anthropic `/v1/messages`（high） |
| **Hermes Agent**（NousResearch） | 是（high） | stdio、SSE legacy | `headers` 映射（可配，支持 `${VAR}`）；OAuth 2.1（DCR+PKCE）；mTLS。`Authorization: Bearer` 可用（high） | 归一化为 `mcp_<server>_<tool>`（`-`/`.` → `_`，有效字符集 ≈ `[a-z0-9_]`）（high） | 多 provider：Anthropic `/v1/messages`、OpenAI `/chat/completions`、Bedrock、Codex（high） |
| **DeepSeek Harness**（官方 `deepseek-ai/deepseek-harness`，`dsh`） | 是（high） | stdio | `headers`（`Record<string,string>`，YAML `!!js` 内插 env 注 `Authorization: Bearer`）；**无 OAuth**（配置 schema 穷举、无此字段）（high） | `mcp__<serverName>__<rawName>`；归一化 ≤64 字符 `[A-Za-z0-9_-]`、非法字符→`_`、损失性改动追加 SHA-256 前 12 位 hex；线上 `tools/call` 只发 raw name（high） | 内置 `dsh-llm-deepseek`＝OpenAI 兼容 chat-completions（`baseURL` 可指自建网关，`$DEEPSEEK_BASE_URL` 回退）；`dsh-llm-pi-ai` adapter 另支持 `openai-responses` / `anthropic-messages`，`baseURL` 指向网关为一等场景（high） |
| **Pi**（badlogic/pi-mono；经 `pi-mcp-adapter` 扩展） | 是，**须先装扩展**（high） | stdio、Unix socket（rmcp-mux） | `bearer`（`bearerToken`/`bearerTokenEnv`）或 `oauth`；`headers` 可配（`${VAR}`/`!cmd`）（high） | `toolPrefix`：`server`→`<server>_<tool>`(默认) / `short` / `none` / `mcp`→`mcp__<server>__<tool>`；模糊匹配 `_`/`-`（high） | 多 provider（pi-ai）：`openai-completions`、`anthropic-messages`、`google-generative-ai` 等；Quickstart 默认 Anthropic（high） |

> 无需额外 spike——四者的相关字段均有文档/源码佐证。

## 结论：`/mcp` 最小可移植配置

**「Streamable HTTP + 标准 `Authorization: Bearer` + identifier-safe 工具名（`[a-z0-9_]+`）」即足以覆盖全部四个客户端。** 无任何客户端强制 stdio 桥、异类传输或非标准鉴权头。

落点（待 handoff 并入设计）：
1. **传输**：`/mcp` 维持 Streamable HTTP（四者原生支持；Pi 依赖扩展）。不引入 stdio 桥。
2. **鉴权**：mcp-hub 用**独立** `FastMCP()` + middleware 读 `Authorization` 头（**勿用** `FastMCP.from_fastapi`，见 PrefectHQ/fastmcp#2817 会剥掉 authorization 头）。标准 `Authorization: Bearer` 全覆盖。
3. **OAuth 2.1 兜底（handoff 决定：本期不做、预留）**：主要缓解是 runbook 指定 Claude Code 用 `headersHelper`（保证工具调用 POST 也带头）；仅当 D2 实测复现 #47424（Bearer 被误判 OAuth）时，再在 `/mcp`（或 `.well-known/oauth-authorization-server`）加 MCP OAuth 2.1 元数据端点（实现量小）。不默认提供——半吊子 OAuth 端点反而可能诱导客户端走一条不通的 OAuth 流。
4. **工具名预归一化**：对外暴露 `analyze_image`（下划线，勿用 `analyze-image`/`analyze.image`）；**外部 MCP 前缀必须 identifier-safe**（细化 US-P12「命名加前缀防冲突」——前缀字符集限 `[a-z0-9_]`，如 `zhipu_*`）。四客户端各自还会再归一化，但网关侧预归一化可避免分歧。
5. **Pi 前置说明（文档）**：Pi 用户须先 `pi install npm:pi-mcp-adapter`，再在 `.mcp.json` 加 `url` 型 server；之后与其它三者无异。需写入 runbook/原型说明。

## 与 US-P3 的关键联动（降风险）

Hermes、DeepSeek Harness、Pi 三者均**可配到 OpenAI Chat Completions 路径**（DeepSeek Harness 内置 adapter 即 OpenAI 兼容且 `baseURL` 可指网关）；仅 Claude Code 锁定 Anthropic `/v1/messages`。含义：
- issue #4 spike 已 **PASS**（LiteLLM 1.96.2，流式 + 工具调用实测可用），Anthropic 路径的当期风险已消解；
- 但即便未来 LiteLLM 版本回归引入同类 bug，这三个 harness 仍可切换到 OpenAI 路径绕开——多 harness 故事不挂死在 Anthropic 适配器上，只有「必须用 Claude Code」时才受其制约。

## 身份核验（已确认）

- 「最新的 deepseek harness」＝官方 **`deepseek-ai/deepseek-harness`**（用户 2026-08-14 确认并给出 URL）。本调研初版误判为 CodeWhale（`Hmbown/CodeWhale`），已更正：矩阵中 CodeWhale 行删除，替换为官方 harness 行（四要素依据见同目录 `deepseek_harness_mcp.md`）。
- 「Pi」＝`badlogic/pi-mono`（pi-ai 生态），MCP 经第三方 `pi-mcp-adapter`（用户已确认）。

## 一手来源

- Hermes MCP：https://github.com/NousResearch/hermes-agent/blob/master/website/docs/user-guide/features/mcp.md
- Hermes 架构/传输：https://github.com/NousResearch/hermes-agent （AGENTS.md、agent/transports/）
- DeepSeek Harness 四要素：详见同目录 `deepseek_harness_mcp.md`（含 9 条官方仓库一手 URL：mcp-client README 与 `src/index.ts`/`transport.ts`/`tools.ts`、llm-deepseek/llm-pi-ai README 与源码）
- pi-mcp-adapter：https://github.com/nicobailon/pi-mcp-adapter/blob/master/README.md
- Pi「No MCP」立场 + providers：https://github.com/badlogic/pi-mono/blob/master/packages/coding-agent/README.md
- pi-ai 多 provider 适配：https://github.com/badlogic/pi-mono （packages/ai）
- Claude Code Bearer-vs-OAuth 怪癖：https://github.com/anthropics/claude-code/issues/47424
- Claude 平台 MCP connector（远程 Bearer）：https://platform.claude.com/docs/en/agents-and-tools/mcp-connector
