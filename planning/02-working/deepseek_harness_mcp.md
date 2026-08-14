# deepseek-ai/deepseek-harness MCP 调研
> 调研日期：2026-08-14 · 口径：仅一手资料（官方仓库 README / 源码，经 zread 实时抓取 GitHub master 分支）

仓库定性：DeepSeek Harness（`dsh`）是 DeepSeek AI 官方开源的 agent harness，"everything is a plugin" 架构，基于 Cordis 插件框架，当前处于 **developer preview**（官方 README 原文："currently in _developer preview_ … THERE WILL BE COMPATIBILITY-BREAKING CHANGES"）。所有配置走 `cordis.yml` 插件行（无 `.mcp.json` 类独立配置文件）。

## 结论矩阵（四问题）

| 维度 | 结论 | 置信度 |
|------|------|--------|
| 1. MCP 客户端支持 | **支持**。专用插件 `@deepseek-ai/dsh-mcp-client`，一个插件实例连一个 MCP server；传输仅 `stdio` 与 `streamable-http`（远程）。**无独立 legacy SSE 传输选项**。配置方式为 `cordis.yml` 插件行（非 `.mcp.json`、非 CLI 命令） | high |
| 2. 远程 MCP 鉴权 | 仅 **自定义 `headers`**（`Record<string,string>`，可注入任意 header 如 `Authorization: Bearer …`，支持 YAML `!!js` 内插环境变量）；stdio 侧用 `env` 注入。**无 OAuth 支持**（配置 schema 中不存在任何 OAuth 字段） | high |
| 3. 工具命名 | 公开名为 **`mcp__<serverName>__<rawName>`**（README 自述与 Claude Code / Codex 同形）。归一化到 DeepSeek function-name 契约：≤64 字符、仅 `[A-Za-z0-9_-]`，非法字符替换为 `_`，名字被改动时追加 `(serverName, rawName)` 的 SHA-256 前 12 位 hex 保证唯一；`serverName` 约束 `[A-Za-z0-9_-]{1,32}` 且跨实例唯一。线上 `tools/call` 只发 raw name，公开名永不下发 | high |
| 4. 模型协议 | 双 adapter：① 内置 `dsh-llm-deepseek`＝DeepSeek 官方 **chat-completions** wire format（fetch+SSE），`baseURL` 可配（含网关，`$DEEPSEEK_BASE_URL` env 回退）；② `dsh-llm-pi-ai`＝多 provider 通用 adapter（`@earendil-works/pi-ai`），可配协议 **`openai-completions` / `openai-responses` / `anthropic-messages`**，`baseURL` 指向自建网关为一等场景 | high |

---

## 各问题一手证据

### 1. MCP 客户端支持

**结论：支持；传输 = stdio + streamable-http（远程）；无独立 SSE 传输；配置在 `cordis.yml`。**

- 包 README（`packages/mcp/mcp-client/README.md`）开头：
  > "MCP client bridge plugin: connects to external [Model Context Protocol](https://modelcontextprotocol.io/) servers and registers their tools on `ctx.tools`, making them available to the model as native tools under server-qualified names (`mcp__<serverName>__<rawName>`)."
  >
  > "One plugin instance per MCP server in `cordis.yml`:"
- 配置示例（README 原文，两种传输各一）：
  ```yaml
  - id: mcp-github
    name: '@deepseek-ai/dsh-mcp-client'
    config:
      serverName: github
      transport: stdio
      command: npx
      args: ['-y', '@modelcontextprotocol/server-github']
      env:
        GITHUB_TOKEN: !!js process.env.GITHUB_TOKEN

  - id: mcp-web
    name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: web
        transport: streamable-http
        url: http://localhost:3000/mcp
        headers:
          Authorization: !!js '`Bearer ${process.env.MCP_TOKEN}`'
  ```
  （`serverName` 为本地命名空间；`transport` 取值 `"stdio"` 或 `"streamable-http"`——README Config 表：`transport | both | yes | "stdio" or "streamable-http"`）
- 源码佐证（`packages/mcp/mcp-client/src/index.ts`）——配置 schema 是穷举的 zod union，只有两种传输：
  ```ts
  export const Config = z.union([
    z.object({ transport: z.const('stdio'), serverName: …, command: …, args: …, env: …, cwd: …, … }),
    z.object({ transport: z.const('streamable-http'), serverName: …, url: …, headers: z.dict(String)…, … }),
  ])
  ```
  以及类型注释 `/** Config for connecting to an MCP server over Streamable HTTP (SSE). */`——源码把 SSE 视作 streamable-http 的底层流机制，而非独立传输选项。
- 传输工厂（`packages/mcp/mcp-client/src/transport.ts`）：
  ```ts
  case 'stdio':
    return new StdioClientTransport({ command: config.command, args: config.args, env: buildChildEnv(config.env), cwd: config.cwd })
  case 'streamable-http':
    return new StreamableHTTPClientTransport(
      new URL(config.url),
      { requestInit: { headers: config.headers } },
    ) as Transport
  ```
  使用官方 `@modelcontextprotocol/sdk` 的 `StdioClientTransport` 与 `StreamableHTTPClientTransport`；**没有** `SSEClientTransport` 分支 → 独立 legacy SSE 传输无此能力。
- README Known Limitations 也印证 streamable-http 的重连语义依赖 SDK 自身："Streamable HTTP failures surface per request and through the SDK transport's own SSE-stream recovery"。
- 配置入口不存在 `.mcp.json` 等价物：根 README 只给出 `npx @deepseek-ai/dsh web` 启动方式；MCP server 一律以 `cordis.yml` 插件行声明（多个 server＝多个插件实例，HMR 热切换）。

### 2. 远程 MCP 鉴权

**结论：仅自定义 headers（含 Bearer + 环境变量注入）；stdio 用 env；无 OAuth。**

- `headers` 字段（README Config 表原文）："`headers` | http | no | Extra headers (e.g. auth tokens)"；类型 `Record<string, string>`（`src/index.ts` 的 `StreamableHttpConfig`：`/** Additional headers attached to MCP requests. */ headers: Record<string, string>`）。
- Bearer + 环境变量注入（README 示例原文）：
  ```yaml
  headers:
    Authorization: !!js '`Bearer ${process.env.MCP_TOKEN}`'
  ```
  即 token 不落盘，经 YAML `!!js` 内插从 `process.env` 取。stdio 侧等价物是 `env: { GITHUB_TOKEN: !!js process.env.GITHUB_TOKEN }`（env 叠加在净化过的父进程 env 上，`transport.ts` 的 `buildChildEnv`）。
- `transport.ts` 实现即把 headers 塞进 `requestInit: { headers: config.headers }`——除此之外无任何鉴权层。
- **OAuth：无此能力**。证据：`src/index.ts` 的 Config zod union 穷举了全部配置字段（两分支各仅 transport/serverName/url|command/args/env/cwd/headers/toolCallTimeoutMs/failOnStartupError/reconnect），不存在任何 OAuth/token-endpoint/client-id 字段；`transport.ts` 未构造任何 OAuth 凭据。README 亦无 OAuth 字样。

### 3. 工具命名

**结论：`mcp__<serverName>__<rawName>`；归一化到 64 字符 / `[A-Za-z0-9_-]`，损失性改动追加 12 位 hex 哈希；线上调用用 raw name。**

- README「Tool naming」原文：
  > "Every MCP tool has two names: the raw MCP name (sent on the wire in `tools/call`) and the public name `mcp__<serverName>__<rawName>` registered on `ctx.tools`. Public names are normalized to the DeepSeek function-name contract (64 chars, `[A-Za-z0-9_-]`); when replacement or truncation changes the name, a deterministic 12-hex-char hash of `(serverName, rawName)` is appended so distinct tools never collapse into one name. Names are pure functions of `(serverName, rawName)` …"
  >
  > "The model sees `mcp__github__create_issue`, `mcp__web__search`, … — the same server-qualified shape Claude Code and Codex use."
- 源码实现（`packages/mcp/mcp-client/src/tools.ts`）：
  ```ts
  const MAX_PUBLIC_NAME_LENGTH = 64                     // DeepSeek function-name contract
  const INVALID_NAME_CHARS = /[^A-Za-z0-9_-]/g
  const HASH_LENGTH = 12                                 // SHA-256 identity hash

  export function publicToolName(serverName: string, rawName: string): string {
    const joined = `mcp__${serverName}__${rawName}`
    const normalized = joined.replace(INVALID_NAME_CHARS, '_')
    if (normalized === joined && normalized.length <= MAX_PUBLIC_NAME_LENGTH) return normalized
    const hash = createHash('sha256').update(`${serverName}\0${rawName}`).digest('hex').slice(0, HASH_LENGTH)
    return `${normalized.slice(0, MAX_PUBLIC_NAME_LENGTH - HASH_LENGTH - 1)}_${hash}`
  }
  ```
- 字符集约束有两层：`serverName` 由 schema 约束 `/^[A-Za-z0-9_-]{1,32}$/` 且跨存活实例唯一（重复在加载期报错）；raw name 中非法字符归一化为 `_`。
- 执行时永远以 raw name 调服务端（`tools.ts` 的 `createExecutor` 闭包持 rawName，`client.request({ method: 'tools/call', params: { name: rawName, … } })`；注释明确 "the public name is never sent to the server"）。

### 4. 模型协议

**结论：OpenAI 兼容 chat-completions 与 Anthropic messages 均可；`baseURL` 指向自建网关为两个 adapter 的一等能力；多 provider 由 pi-ai adapter 承担。**

内置 adapter `@deepseek-ai/dsh-llm-deepseek`（`packages/llm/llm-deepseek/README.md`）：
> "DeepSeek chat-completions adapter for the harness LLM seam: direct `fetch` + SSE … translating the official wire format … into the `StreamChunk` protocol."
- 配置（README 原文片段）：`apiKeyEnv: DEEPSEEK_API_KEY`（凭据引用，不落盘）；`baseURL: https://api.deepseek.com # optional; $DEEPSEEK_BASE_URL then the public API when omitted`；`models` 可自定义（含私网模型示例 `- id: private-reasoner / description: Company-hosted reasoning model`）。
- 网关场景明确支持（README「App attribution」节）："Both headers go to the resolved `baseURL`, **including a configured gateway**"。

多 provider adapter `@deepseek-ai/dsh-llm-pi-ai`（`packages/llm/llm-pi-ai/README.md` + `src/provider.ts`）：
> "Generic multi-provider adapter … backed by `@earendil-works/pi-ai`. … a route pi-ai does not ship is declared outright, so an OpenAI-compatible gateway, a self-hosted server, or a provider newer than the installed catalog is configuration rather than a code change."
- 可名命的 wire 协议（`src/provider.ts` 的 `PROTOCOLS` 表原文）：
  ```ts
  const PROTOCOLS: Readonly<Record<string, () => ProviderStreams>> = {
    'openai-completions': openAICompletionsApi,
    'openai-responses': openAIResponsesApi,
    'anthropic-messages': anthropicMessagesApi,
  }
  ```
  即 **OpenAI Chat Completions、OpenAI Responses、Anthropic Messages（/v1/messages）** 三种；Bedrock（SigV4）/Vertex/Azure/Codex（OAuth）被刻意排除（该文件注释逐一说明原因）。
- 自建网关配置示例（README 原文的 hand-declared route）：
  ```yaml
  acme-gateway:
    displayName: Acme Gateway
    apiKeyEnv: ACME_GATEWAY_API_KEY
    api: openai-completions
    baseURL: https://gateway.acme.example/v1
    compat:
      thinkingFormat: deepseek
    models:
      - id: acme-large
        contextWindow: 65536
  ```
  另有 catalog route 换代理的例子：`openai: { apiKeyEnv: OPENAI_API_KEY, baseURL: https://proxy.example.com:8443 }`；README 明言 "`baseURL` sets the endpoint of every model on the route, so private proxies such as `https://proxy.example.com:8443` remain supported"。catalog 内置路由示例含 `anthropic: { apiKeyEnv: ANTHROPIC_API_KEY, … }`（走 pi-ai 内置 catalog 的 anthropic-messages）。
- 其他要点：每 route 一个协议（`api` 作用于整条 route）；凭据只存 `apiKeyEnv` 引用、按请求经 `ctx.credentials` 解析；profile 还支持 `headers`、`transport`、`retryPolicy` 等字段（README「Supported profile fields are …」句）。

---

## 一手来源

- https://github.com/deepseek-ai/deepseek-harness （根 README：项目定位、developer preview、`npx @deepseek-ai/dsh web`）
- https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md （MCP 用法/Config 表/工具命名/行为与限制）
- https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/src/index.ts （Config zod schema：传输枚举、headers/env 字段、serverName 约束）
- https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/src/transport.ts （StdioClientTransport / StreamableHTTPClientTransport 工厂，requestInit headers）
- https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/src/tools.ts （publicToolName 归一化 + SHA-256 哈希、raw name 下发）
- https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-deepseek/README.md （DeepSeek chat-completions、baseURL/apiKeyEnv、网关）
- https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-pi-ai/README.md （多 provider 配置、acme-gateway 网关示例、profile 字段清单）
- https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-pi-ai/src/provider.ts （PROTOCOLS 表：openai-completions / openai-responses / anthropic-messages）
- https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-pi-ai/src/catalog.ts （catalog 解析、排除 OAuth-only provider 的说明）
