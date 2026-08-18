# mcp-hub：外部 MCP 注册/分发 + 按分组（Group）控制可分发的 MCP 调研

> 日期：2026-08-18
> 目的：调研在 mcp-hub 上实现「注册并分发其他 MCP」+「对 Group 可控分发的 MCP」+「同一 server URL + 同一把 Key 完成发现（tools/list）与使用（tools/call）」的可行落点。**本期只出调研结论并开 issue，不做开发。**
> 依据：现有 `execution/proto-remote-access/mcp-hub/mcp_hub.py`、`console/console.py`（MCP 注册管理面）、`planning/03-core/prototype_remote_model_access_baseline_proto-r6.md`（US-P4/P12/P13）、runbook、以及已安装 fastmcp **3.4.7** 源码（`fastmcp/server/middleware/authorization.py`、`server/auth`、`utilities/authorization.py`、`tools/base.py`、`server/dependencies.py`）。均为本地一手代码，无外部网络依赖。

## 1. 现状（proto-r6 已交付）

| 组件 | 现行为 |
|---|---|
| `mcp-hub`（`/mcp`，Streamable HTTP + `Authorization: Bearer`） | 暴露内建视觉工具 `analyze_image` + 全部已注册外部 MCP 的**全部工具**（`external-mcp.json`，启动时静态 `mcp.add_tool` 代理，前缀防冲突） |
| 外部 MCP 注册管理面 | 管理员经 `console` 的 `/console/api/mcp/register`（写 `external-mcp.json` 0600 + 重启 mcp-hub）；每项含 `name/url/api_key/prefix` |
| 鉴权 | `LiteLLMTokenVerifier.verify_token` → 回环 LiteLLM `/key/info` 验真；`AccessToken(token, client_id=key_hash, scopes=[])` |
| 分组（US-P13） | Key 的 `metadata.group` 用于 **LLM 请求路由**到 provider 部署（group_routing 钩子注入 tag）；`default` = 未绑定/全量池。**与 MCP 无关联** |

**关键缺口**：外部 MCP 当前是**全局**的——一把有效 Key 就能发现并使用全部已注册外部 MCP 的所有工具，**没有任何按分组/按 Key 的裁剪**。用户提出的三点需求里，「注册/分发其他 MCP」和「单 URL + 单 Key 发现与使用」现状已基本成立，**真正的新增是「对 Group 可控分发（每个 MCP 可绑到若干 Group，仅组内 Key 可见可用）」**。

## 2. 目标（用户需求拆解）

1. **注册 + 分发其他 MCP**：mcp-hub 作为 MCP 网关/注册中心，管理员注册外部 MCP（URL + 服务侧凭据），网关代持凭据、代理其工具并聚合透出。→ 现状「代理」已具备，需演进为「按分组分发」。
2. **对 Group 可控分发的 MCP**：每个外部 MCP 可绑定若干 Group；只有 `metadata.group` 命中该组的用户 Key，才能在 `tools/list` 看到并在 `tools/call` 调用其工具。内建 `analyze_image` 默认全局。
3. **单 server URL + 同一把 Key 完成发现与使用**：客户端始终连 `https://<域名>/mcp` + 自己的虚拟 Key；网关按 Key 解析出分组，动态裁剪工具面。发现与使用走同一入口、同一鉴权。→ 传输/鉴权模型不变，仅增加**请求级按组过滤**。

## 3. 技术落点（fastmcp 3.4.7，本地源码实证）

现静态代理（启动时全量注册 + 无过滤）无法按请求裁剪工具。fastmcp 3.4 提供了**请求级 auth 中间件**，天然落到这里：

- **`AuthMiddleware(auth=<AuthCheck>)`**（`fastmcp/server/middleware/authorization.py`）：
  - `on_list_tools`：对 `tools/list` **逐工具过滤**（`run_auth_checks` 不通过 → 直接隐藏，不泄漏受限 MCP 的工具名）；
  - `on_call_tool`：对 `tools/call` **调用前拒绝**，且对「不存在 / 未授权」返回**同一暧昧错误**（不披露工具存在性，符合安全最佳实践）。
  - 挂在 `FastMCP(..., middleware=[...])` 或 `add_middleware()`（`server/server.py:330/527`）。
- **组件tags**：`Tool` 携带 `tags: set[str]`（`tools/base.py`、`utilities/components.py`），可给每个代理工具打组标签；`AuthContext.component.tags` 可取。
- **token.scopes 传分组**：`LiteLLMTokenVerifier.verify_token` 现返回 `scopes=[]`；改为 `scopes=[group]`（Key 的 `metadata.group`，缺省 `default`）。`AuthMiddleware` 内 `get_access_token()`（`dependencies.py:467`）从 HTTP request scope / contextvar 取回该 token，`AuthContext.token.scopes` 即可读调用者分组。→ 分组随每一次请求自然流入，**无需改传输/鉴权协议**。
- **自定义 `AuthCheck`**（`utilities/authorization.py`）：`Callable[[AuthContext], bool|Awaitable[bool]]`。写一个 `group_access(ctx)`：
  - 工具无组标签（全局）→ 放行；
  - 否则 → `ctx.token.scopes` 中的组 ∈ `ctx.component.tags` 才放行。

### 推荐实现路径（供 issue 采用）

1. `external-mcp.json` 条目增加可选 `groups: ["home"]`（缺省 = 全局，兼容既有条目不破坏）；console 注册/编辑页可绑定多组。
2. mcp-hub 注册外部工具时按条目 `groups` 给每个代理 `FastTool(..., tags=set(groups))`；内建 `analyze_image` 不打组标签（全局）。
3. `LiteLLMTokenVerifier.verify_token` 把分组写入 `scopes`。
4. `FastMCP(..., middleware=[AuthMiddleware(auth=group_access)])` 一处打通 `tools/list` 过滤 + `tools/call` 拒绝。
5. console：MCP 管理页加「绑定分组」；分组管理/快照可回显每组持有的 MCP 数。

## 4. 关键取舍/待确认（写进 issue 的开放问题）

- **`default`（未绑定）Key 与受限 MCP 的关系**：推荐「未绑定/`default` 的 Key 只见全局 MCP」——受限 MCP 是组内显式授权，符合 US-P13「default=全量池」但受限项不进 default。需确认。
- **组绑定为多对多**（一个 MCP 可绑多组）——推荐列表式，需确认是否允许跨组共享同一 MCP。
- **内建视觉工具是否也可按组受限**——推荐默认全局，预留扩展。
- **拒绝形态**：`tools/call` 越权返回 MCP 层 `access_denied`/permission 错误（非 200），由 fastmcp `AuthorizationError` 抛给协议层。
- **规模**：仍保持「启动时全量注册 + 请求级过滤」的静态代理（简单、复用现状），不引入按请求动态连上游；`tools/list` 由中间件按组裁剪，每调用者工具面收敛。
- **凭据边界 C5 不变**：外部 MCP Key 仍只存网关侧 `external-mcp.json`（0600），用户虚拟 Key 永不出网关。

## 5. 影响范围与本轮边界

- 涉及：`mcp-hub`（核心：schema + tags + scopes + AuthMiddleware）、`console`（MCP 分组绑定 UI/API、分组快照）、`external-mcp.json` 结构、（可选）主页/runbook 文档、mcp-hub 单测。
- 安全边界为正收益：从「任一有效 Key 全量可见」收紧为「按组授权」，杜绝跨组工具泄漏；`tools/list` 隐藏 + `tools/call` 暧昧拒绝。
- **本轮不做任何开发**：仅输出本文调研 + 开 GitHub issue 记录需求与方案，待下一步决策后另起开发轮。
