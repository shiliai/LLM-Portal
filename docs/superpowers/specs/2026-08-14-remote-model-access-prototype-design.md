# 设计文档：远程访问私有推理模型（快速原型）

> 日期：2026-08-14
> 状态：待用户评审
> 用户故事基线：`proto-r5`（BASELINE_APPROVED，已冻结）
> 基线文件：`planning/03-core/prototype_remote_model_access_baseline_proto-r5.md`
> 基线 SHA-256：`05857180a4147a93b62533f0c1a80b830299234b186fa03fa991ff1bb0a0bb40`
> 范围声明：独立于 LLM-portal 旧 PRD 基线（r2~r4），互不引用、互不约束。

## 0. 一句话方案

**VPS 上跑 LiteLLM Proxy（统一 API 网关 + 虚拟 Key + Admin UI）+ 自写 mcp-hub（网关托管视觉 MCP 与外部 MCP 代理）+ WireGuard（多站点公钥身份隧道）+ 自写 onboardd/site-add（一键站点接入与吊销）+ 自写 ~30 行分组路由钩子（Key 绑定 provider 分组，US-P13），Caddy 443 单入口自动 HTTPS。自写代码合计约 730 行，其余全部复用成熟开源组件。**

## 1. 技术选型与被否方案

| 决策点 | 选型 | 被否方案与原因 |
|---|---|---|
| API 网关 | **LiteLLM Proxy**（MIT）：原生双协议入口（OpenAI `/v1/chat/completions` + Anthropic `/v1/messages`，含流式 SSE 与 tool_use↔tool_calls 转换，满足 US-P3）；`model_name` 与 `litellm_params.model` 解耦即别名映射（US-P11）；Router `least-busy` 策略 + 健康检查冷却（逐字满足 US-P6「按在途请求数/延迟分流」）；虚拟 Key + Postgres + Admin UI + `/key/info`（US-P9/P10、C3） | **new-api**：负载均衡为加权随机，不满足冻结故事 US-P6 的「按在途请求数/延迟分流」原文；AGPL-3.0 与下一阶段改造路径冲突（详见 `planning/02-working/new_api_analysis.md`）。降级为「不改源码整机部署」的备选 |
| 内网穿透 | **WireGuard**：每站点密钥对即站点身份，公钥可列出/吊销（US-P7/P8 的「公钥管理」只有 WG 天然支持）；内核态、断线自愈（US-P5）；未注册公钥的 UDP 探测被静默丢弃（C2） | **frp**：共享 token 鉴权，无逐站点公钥身份，无法满足 US-P8 吊销语义 |
| 视觉 MCP | **自写 mcp-hub**（Python + fastmcp，约 300 行）：Streamable HTTP 挂 `/mcp`，形态同智谱 vision-mcp-server（US-P4 逐字要求）；同时承担外部 MCP 代理（US-P12） | 现成 MCP 网关（如 mcp-proxy 类项目）：无「内建视觉工具 + 上传换临时 URL + 按调用者 Key 记账」的组合能力，改造成本高于自写 |
| 反代/TLS | **Caddy**：自动证书（C1），`flush_interval -1` 放行 SSE 流式 | Nginx + certbot：可用但配置量更大，原型期不选 |

## 2. 架构总览

```text
                    公网攻击面：仅 443/tcp + 51820/udp + SSH
┌─ 任意地点客户端 ────────────────────────────────────────────────┐
│ OpenAI SDK / Claude Code / 任意 MCP 客户端                        │
│  base_url = https://<你的域名>   key = sk-<用户虚拟Key>            │
└───────────────┬─────────────────────────────────────────────────┘
                │ HTTPS :443
┌─ VPS ─────────▼─────────────────────────────────────────────────┐
│ Caddy（自动 HTTPS，按路径分发）                                    │
│   /v1/*、/ui*  → 127.0.0.1:4000  LiteLLM Proxy ──┐               │
│   /mcp*        → 127.0.0.1:8200  mcp-hub ────────┤ 回环互访       │
│   /onboard/*   → 127.0.0.1:8100  onboardd ───────┘               │
│ Postgres（LiteLLM 虚拟 Key/用量）   SQLite（mcp-hub 调用计数）      │
│ wg0 = 10.77.0.1/24  监听 :51820/udp（wg-quick@wg0）               │
└───────┬──────────────────────────────┬──────────────────────────┘
        │ WireGuard 加密隧道             │
┌─ 站点A（10.77.0.11）────────┐  ┌─ 站点B（10.77.0.12）… ─────────┐
│ 常开机器 192.0.2.10      │  │ 各站点 LAN 网段允许重叠          │
│  deepseek :8890  qwen :8004 │  │ （只寻址 WG 虚拟 IP，不路由 LAN）│
└─────────────────────────────┘  └────────────────────────────────┘
```

**组件与职责（每个单元一句话即可说清）：**

| 组件 | 职责 | 端口 | 来源 |
|---|---|---|---|
| Caddy | 唯一公网 HTTP 入口，自动 HTTPS，路径分发，SSE 放行 | 443（公网） | 开源 |
| LiteLLM Proxy | 双协议 API、模型别名、多站点路由、虚拟 Key、Admin UI、用量 | 4000（回环） | 开源 |
| group-routing 钩子（自写 ~30 行） | LiteLLM 自定义 logger：鉴权后把 Key 的 `metadata.group` 注入为请求路由 tag，使分组授权服务端化、客户端不可旁路 | （随 LiteLLM 进程） | 自写 |
| Postgres | LiteLLM 的 Key 与用量存储 | 5432（回环） | 开源 |
| WireGuard | 站点隧道，公钥即站点身份 | 51820/udp（公网） | 内核 |
| mcp-hub（自写 ~300 行） | `/mcp` 端点：内建 `analyze_image` + 外部 MCP 代理 + 上传接口 + 按 Key 计数 | 8200（回环） | 自写 |
| onboardd（自写 ~150 行） | 站点注册 API：验一次性 token、加 WG peer、注册 LiteLLM deployment | 8100（回环） | 自写 |
| site-add / site-revoke（自写 ~150 行） | 管理员 CLI：签发一次性接入命令 / 吊销站点 | — | 自写 |
| install.sh 模板（~100 行） | 站点侧一行命令安装：genkey→注册→wg-quick 自启→自检 | — | 自写 |

**寻址规则（多站点 LAN 网段冲突的解法）**：VPS 只寻址各站点的 WG 虚拟 IP（10.77.0.11+），不路由任何 LAN 网段——因此多个站点同为 192.168.88.x 也互不冲突。站点常开机器即 WG peer；首站点 192.0.2.10 本身就是模型机，模型端口直接从其 WG IP 可达（`http://10.77.0.11:8890/v1`）。若未来站点的模型在常开机器之外的内网主机上，由 install.sh 在常开机器加本机端口转发（iptables DNAT），对 VPS 侧无感知（扩展点，本期不实现）。

## 3. 关键配置设计

### 3.1 LiteLLM `config.yaml`（模型注册 + 别名 + 路由）

```yaml
model_list:
  # 直选名（US-P1/P2）：站点A
  - model_name: deepseek-v4-flash-0731
    litellm_params:
      model: openai/deepseek-v4-flash-0731
      api_base: http://10.77.0.11:8890/v1
      api_key: "none"            # 字面量占位（会以 Bearer none 发出、无鉴权上游忽略），非真实凭据；用户 Key 永不外发（C5）
      tags: ["default"]          # 所属分组（US-P13）：provider↔分组多对多，站点全部 deployment 同组
  - model_name: qwen3.6-35b-a3
    litellm_params:
      model: openai/qwen3.6-35b-a3
      api_base: http://10.77.0.11:8004/v1
      api_key: "none"
      tags: ["default", "home"]  # hq-office 同时属 default 与 home 组
  # 别名（US-P11）：同一上游可挂多个对外名
  - model_name: claude-opus-5
    litellm_params:
      model: openai/deepseek-v4-flash-0731
      api_base: http://10.77.0.11:8890/v1
      api_key: "none"
      tags: ["default"]
  # 站点B 部署同名 deepseek 时由 onboardd 追加一条同 model_name、不同 api_base 的记录
  #（LiteLLM Router 自动把同名多条视为同一模型的多个 deployment 并分流，满足 US-P6）

router_settings:
  routing_strategy: least-busy    # 按在途请求数分流（US-P6 原文语义）
  enable_tag_filtering: true      # 分组过滤（US-P13）：Key 带分组 tag → 只路由到同 tag 的 deployment
  num_retries: 1                  # 单站点失败重试到其他健康 deployment
  timeout: 5                      # 连接级快速失败，不无限挂起（US-P1 失败路径）
  cooldown_time: 60               # 故障 deployment 冷却 60s（熔断）
  allowed_fails: 1

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY   # 仅管理用途
  database_url: os.environ/DATABASE_URL       # Postgres：虚拟 Key + 用量

litellm_settings:
  drop_params: true               # 上游不认识的参数直接丢弃，提高客户端兼容性
```

要点：
- **别名与直选并存**（US-P11 边界）：`deepseek-v4-flash-0731`、`qwen3.6-35b-a3`、`claude-opus-5` 都在 `model_list`，`/v1/models` 全部可见；未注册模型名 LiteLLM 返回 400/404 语义的「模型不存在」，不误路由。
- **US-P6 的实现即配置**：同 `model_name` 多条 deployment + `least-busy` + `cooldown`，无需自写调度代码。
- **每站点限额**（US-P6「可为每站点设并发/速率上限」）：deployment 级 `rpm`/`tpm` 字段，由 onboardd 注册时可选传入。
- **别名的多站点分流**：`site-add --model` 传什么对外名就注册什么 deployment；若希望别名（如 `claude-opus-5`）也跨站点分流，需在各站点显式以该别名注册（同一端口可注册多个对外名）。T6 演练前按此配置。
- **分组即 tag（US-P13 的实现）**：分组不引入新存储——deployment 的 `litellm_params.tags` 列出其所属分组名（多对多），Key 建号时把分组名写入 Key `metadata.group`（一把 Key 一个组）；`enable_tag_filtering: true` 后，路由器按本请求的路由 tag 只在同 tag 的 deployment 中做 least-busy 分流与熔断，组内无该模型部署即返回可判读错误。未绑组 Key 走 `default` 组（全部 provider 的 deployment 都带 `default` tag）。
  - **为什么需要一个 ~30 行的自写 pre-call 钩子**：免费版 `enable_tag_filtering` 按**请求自带的 tag** 过滤（客户端用 `x-litellm-tags` 头携带）；而「Key 自带的组自动作用于路由」属于 LiteLLM **Enterprise** 的 team-based tag routing，且 key 级 `allowed_tags` 尚在提案阶段未实现（[litellm#22966](https://github.com/BerriAI/litellm/issues/22966)）。为避免客户端自行伪造 tag 绕过分组授权，组标签必须在**网关侧、鉴权之后**注入——故加一个免费版自定义 logger（`async_pre_call_hook`）读取已鉴权 Key 的 `metadata.group`、写入本请求的路由 tag，使分组授权不可被客户端旁路；钩子读不到组时回落 `default`。此钩子是本期唯一为 US-P13 新增的自写代码（计入 §10 自写合计 ~700→~730）。

### 3.2 Caddyfile（单入口路径分发）

```caddyfile
<你的域名> {
    handle /mcp* {
        reverse_proxy 127.0.0.1:8200
    }
    handle /onboard/* {
        reverse_proxy 127.0.0.1:8100
    }
    handle {
        reverse_proxy 127.0.0.1:4000 {
            flush_interval -1      # SSE 流式必需
        }
    }
}
```

### 3.3 mcp-hub（自写，Python + fastmcp，~300 行）

对外（经 Caddy）三个入口，同一把用户虚拟 Key 鉴权：

| 入口 | 方法 | 行为 |
|---|---|---|
| `/mcp` | Streamable HTTP（MCP 协议） | `tools/list` 聚合：内建 `analyze_image(image_url, question)` + 各外部 MCP 工具（名称加前缀如 `zhipu_*` 防冲突）；`tools/call` 分发执行 |
| `/mcp/upload` | POST multipart | 本地图片换限时临时 URL：随机不可猜路径，30 分钟 TTL 自动清理，类型/大小白名单（jpg/png/webp/gif，≤10MB） |
| `/mcp/usage` | GET | 调用者本 Key 的 MCP 工具调用计数（按次；补充 US-P10/P12 的 MCP 侧口径） |

**鉴权与记账的凭据边界（C5 的实现口径）：**

1. 客户端带用户虚拟 Key 连 `/mcp`；mcp-hub 调 LiteLLM `/key/info` 验真伪与可用状态——用户 Key 的旅程到此为止，**永不发往任何上游**。
2. `analyze_image` 内部：mcp-hub 以**调用者自己的 Key** 回调回环 LiteLLM `/v1/chat/completions`（model=`qwen3.6-35b-a3`，图片 URL 由 mcp-hub 进程内取回并转 base64 塞入消息体）——token 用量自然记在调用者 Key 上（US-P4 用量归属条款），LiteLLM 出站到无鉴权私有 qwen 时不带任何 Key。
3. 外部 MCP 工具（US-P12）：mcp-hub 用**网关注册时保存的该服务凭据**向外部 MCP 转发；调用次数记入 SQLite（`key_hash, tool, ts`），token 不保证可得（基线原文如此）。

外部 MCP 注册为 mcp-hub 的配置文件条目（`name / url / api_key / 前缀`），管理员编辑后重载生效；凭据文件权限 0600、不入库。

### 3.4 onboardd + site-add / site-revoke（一键接入与公钥管理）

**site-add（管理员在 VPS 执行）：**

```bash
site-add beijing --model deepseek-v4-flash-0731:8890 --model qwen3.6-35b-a3:8004 \
         --group default --group home     # 站点所属分组（US-P13），缺省仅 default
# 输出一条限时命令，拷到站点机器执行：
#   curl -fsSL "https://<你的域名>/onboard/install?token=<一次性token>" | sudo bash
```

- token：高熵随机、一次性、15 分钟过期；站点名、模型清单、预分配 WG IP（10.77.0.x 递增）随 token 存入 onboardd 状态。

**install.sh（站点机器执行，模板渲染下发）：**

1. 安装 `wireguard-tools`（apt/yum 自适应）；
2. 本地 `wg genkey`——**私钥永不离开站点机器**；
3. `POST /onboard/register`（token + 公钥）→ onboardd 校验 token、`wg set wg0 peer <公钥> allowed-ips 10.77.0.x/32`、返回渲染好的 `wg0.conf`（含 VPS 公钥、endpoint、`PersistentKeepalive=25`）；
4. 写 `/etc/wireguard/wg0.conf`（0600），`systemctl enable --now wg-quick@wg0`（US-P5 自启自愈）；
5. 自检并回显：ping 10.77.0.1、本机模型端口连通性逐个探测；
6. `POST /onboard/confirm`（token + 自检结果）→ onboardd 收到全绿确认后调 LiteLLM `/model/new`（master key，回环）注册各 deployment——站点模型即刻进入路由池；自检有红项则不注册并回显失败项。

**site-revoke（US-P8）：**

```bash
site-revoke beijing
# = wg set wg0 peer <该站点公钥> remove   → 隧道立即断开且无法重连
# + LiteLLM /model/delete 摘除该站点全部 deployment → 路由池即刻摘除
# + onboardd 状态表标记吊销
```

站点清单（名称、公钥、WG IP、模型、状态）存 onboardd 的 SQLite，`site-list` 命令列出（US-P8「标识、列出、吊销」三动作齐备；site-list 并入 site-tools 行数预算）。

### 3.5 用户 Key 的绑定模型（持有人 ↔ Key ↔ 分组/模型 ↔ 用量）

```text
持有人（人/设备）──别名标注──▶ 虚拟 Key（sk-…，本期唯一身份实体，无独立账号表）
虚拟 Key ──绑定──────────────▶ provider 分组 Group（站点集合，多对多；未绑＝default 组＝全部 provider）【US-P13】
虚拟 Key ──可选──────────────▶ 模型白名单（LiteLLM 原生按 Key `models` 字段，管理员创建/编辑时设定）
        可用模型 ＝ 分组内 provider 部署的模型 ∩ 模型白名单；请求只在组内 deployment 上分流/熔断
虚拟 Key ──记账──────────────▶ API：请求数 + token（LiteLLM→Postgres）；MCP：按次（mcp-hub→SQLite）
```

- **本期无独立「账号」实体，Key 即身份**：这是基线冻结口径决定的——C3 为「管理员创建/分发/禁用 Key」，US-P10 为「凭自己这把 Key 查自己的用量」，均以 Key 为粒度。一人多设备发多把 Key（别名区分），用量各记各的。
- **Key ↔ provider 分组（US-P13，sub2api group 分层）**：管理员把 provider（本期即站点，未来含外部云上游）归入命名分组（多对多），每把 Key 绑一个分组，请求只在组内 provider 的 deployment 上做 least-busy 分流与故障转移；组内无该模型部署 → 可判读错误，不误路由到组外；未绑组走 `default` 组（全部 provider）。调整分组成员即对组内全部 Key 批量生效。实现为 LiteLLM tag 过滤 + 一个 ~30 行自写 pre-call 钩子（见 §3.1「分组即 tag」要点：免费版只按请求 tag 过滤，组标签须网关侧注入以防旁路）。
- **分组的管理面（US-P13，参照 sub2api 双向编辑）**：分组作为一等对象有两个等价管理入口——①「分组」页：列出全部分组（成员站点 chip、绑定 Key 数）、新建/改名/删除分组、勾选成员站点；②「站点与公钥」页：每个 provider 可勾选 0 个或多个所属分组（多对多，与①等价、双向同步）。`default` 为系统组（= 全部在线 provider，新接入站点自动并入，不可删除/改名）。落到 LiteLLM 侧即改 deployment 的 `tags` 与 Key 的 `metadata.group`，**无新增自写组件**（计入既有 ~730 行预算）。
- **Key ↔ 模型授权（与分组正交）**：默认新 Key 可调用全部对外模型名（含别名与 MCP 工具）。如管理员需限制某把 Key，创建时填 LiteLLM 原生 `models` 白名单（零自写代码），越权调用返回明确的拒绝错误；白名单裁剪的是「模型名」维度，分组裁剪的是「provider」维度，两者独立设定、交集生效。「用户申请模型开通」的审批流是基线 non-goal，留下一阶段。
- **API 与 MCP 同权、同分组、同白名单**：同一把 Key 通吃 `/v1/*` 与 `/mcp`（US-P4 条款）；`analyze_image` 以调用者的 Key 回调 LiteLLM，因此分组与模型白名单对视觉工具天然同样生效；禁用 Key 对 API 与 MCP 同时即时生效（mcp-hub 每次经 `/key/info` 校验）。
- **生命周期**：创建（别名 + 分组 + 可选模型白名单 + 可选 rpm/tpm）→ 分发 → 禁用/启用 → 删除，全程管理页操作（US-P9）；日志与用量只记尾 4 位。

## 4. 数据流（五条主链路）

**流 1 — OpenAI 客户端直选文本模型（US-P1）**：
客户端 `POST https://<你的域名>/v1/chat/completions`（model=deepseek-v4-flash-0731，stream=true）→ Caddy → LiteLLM 验虚拟 Key（错/无 Key → 401）→ Router 选健康 deployment → 经 wg0 到 `10.77.0.11:8890` → SSE 逐块回流。隧道断开时 5s 连接超时 → 重试其他 deployment → 全不可用返回可判读 503（不挂起）。

**流 2 — 带图直选 qwen（US-P2）**：同流 1，model=qwen3.6-35b-a3，消息体含 `image_url`（公网 URL 或 base64 内联均可，由客户端自备），LiteLLM 原样透传给 OpenAI 兼容的 qwen 上游。

**流 3 — Claude Code 主对话（US-P3 + US-P11）**：Claude Code 配 `ANTHROPIC_BASE_URL=https://<你的域名>` + 虚拟 Key，默认模型名 `claude-opus-5` → LiteLLM `/v1/messages` 入口做 Anthropic→OpenAI 请求转换（含 system、tools、流式事件、tool_use↔tool_calls 双向映射）→ 别名解析到 deepseek → 响应逆向转换回 Anthropic 事件流。

**流 4 — 视觉工具（US-P4，两步式覆盖本地图）**：
- 图已有 URL：agent 调 `analyze_image(url, 问题)` → mcp-hub 取图转 base64 → 以调用者 Key 回调 LiteLLM(qwen) → 返回文字结果。
- 图在本地：agent 先 `POST /mcp/upload`（同一 Key）得临时 URL（30min TTL）→ 再走上一条。基线已冻结机制共识：toolcall 参数只含路径/URL 短字符串，base64 转换发生在 mcp-hub 进程内。

**流 5 — 外部 MCP 代理（US-P12）与站点接入（US-P7）**：见 §3.3 第 3 条与 §3.4。

## 5. 错误处理矩阵

| 场景 | 行为 | 对应故事/约束 |
|---|---|---|
| 无/错虚拟 Key（API 或 MCP 或上传） | 401 拒绝 | US-P1 边界、US-P4 边界、C1 |
| 未注册模型名 | 400/404「模型不存在」，不误路由 | US-P11 边界 |
| Key 的分组内无请求模型的部署 | 可判读错误，不误路由到组外 provider | US-P13 边界 |
| 单站点宕/隧道断 | 5s 超时 → 重试健康 deployment → 该 deployment 冷却 60s | US-P6 |
| 全部站点不可用 | 快速返回可判读 503，不无限挂起 | US-P1 失败路径、US-P6 |
| 上传超限/类型非法 | 413 / 400，明确报错 | US-P4 边界 |
| `analyze_image` 参数无有效图片 | MCP 结构化错误（isError + 说明） | US-P4 边界 |
| 外部 MCP 凭据失效/不可达 | 可判读的 MCP 结构化错误（透传上游状态码与摘要） | US-P12 |
| 吊销站点后其 WG 握手 | 内核静默丢弃，无法重连 | US-P8、C2 |
| onboard token 复用/过期 | 403，一次性语义 | US-P7 |
| 上游模型 5xx | 原样透传状态码 + 上游错误摘要 | US-P1 |

## 6. 观测与用量

- **API 侧**（US-P9/P10）：LiteLLM 全部请求落 Postgres；Admin UI（`/ui`，master key 登录）管理 Key/渠道/分组（分组 = deployment `tags` 与 Key `metadata.group` 的 CRUD，US-P13，提供「分组」页与「站点」页 provider↔分组 多选两个等价入口）、看用量与错误日志；普通用户凭自己的 Key 调 `/key/info` 只见自身用量（请求数 + token 数，无计费——基线口径）。
- **MCP 侧**：mcp-hub SQLite 按 Key 计次，`GET /mcp/usage` 自查；`analyze_image` 的 token 消耗因走 LiteLLM 通道自动并入 API 侧账本。
- 日志中 Key 仅记尾 4 位。

## 7. 安全基线（C1/C2/C5 落地）

- **公网攻击面仅三处**：443/tcp（Caddy）、51820/udp（WireGuard）、SSH（既有）。LiteLLM、Postgres、mcp-hub、onboardd 全部只绑 127.0.0.1。
- WG 对未注册公钥的 UDP 包**内核层静默丢弃**——端口扫描不可见；模型端口永不暴露公网（C2）。
- Key 分层：master key 仅用于 Admin UI / 管理 API / onboardd 注册 deployment；用户虚拟 Key 按人发放、可禁用（C3）；用户 Key 永不出网关（C5，实现口径见 §3.3）。
- onboard token 一次性 + 15 分钟 + 高熵；上传文件随机路径 + 30min TTL + 类型/大小白名单。
- 全部密钥（master key、外部 MCP 凭据、WG 私钥）走 `.env`/配置文件（0600）：模板入库（`.env.example`），实值进 `.gitignore`（配合本机 privacy-filter 提交钩子）。
- 文档与代码库中域名/密钥一律占位符（`<你的域名>`、`os.environ/*`）。

## 8. 验收测试（T1~T12，与故事一一对应）

| # | 故事 | 验证步骤与通过标准 |
|---|---|---|
| T1 | US-P1 | 手机热点环境 OpenAI SDK 流式对话 deepseek 成功；错 Key→401；站点侧停 wg → 请求秒级 503 不挂起 |
| T2 | US-P2 | 带图请求直选 qwen，返回识图结果 |
| T3 | US-P3 | Claude Code 仅配 base_url+Key，完成一次含工具调用（文件编辑）的会话 |
| T4 | US-P4 | URL 图一步识别；本地截图经 `/mcp/upload` 两步识别；无效 Key 注册/调用被拒；到期临时文件被清理 |
| T5 | US-P5 | 站点机重启 + 断网 1 分钟 → 隧道自愈、模型恢复，无人工介入 |
| T6 | US-P6 | 同名模型双 deployment（第二站点或同机第二端口模拟）并发 20 请求可见分流；停一站全部落健康站、客户端无感；全停→503 |
| T7 | US-P7 | `site-add` 输出一行命令，新机执行后自检全绿、模型即刻可调；token 二次使用→403 |
| T8 | US-P8 | `site-revoke` 后隧道即断、无法重连、`/v1/models` 与路由池摘除该站点 deployment |
| T9 | US-P9 | Admin UI 建/禁 Key 即时生效；启停渠道即时生效；用量按 Key/模型可筛 |
| T10 | US-P10 | 用户 Key 调 `/key/info` 仅见自身用量（请求数+token）；换别人的 Key 看不到 |
| T11 | US-P11 | Claude Code 用默认名 `claude-opus-5` 直接可用；`/v1/models` 见全部对外名；未知名→400/404 |
| T12 | US-P12 | 注册外部 vision MCP（如智谱）后 `tools/list` 现前缀工具且调用成功；错凭据→可判读错误；`/mcp/usage` 计数递增 |
| T13 | US-P13 | 经「分组」页建分组 home（仅 hq-office）并把某 Key 绑 home：请求 qwen 只落 hq-office（lab-2f 挂同名模型也不被选中）；在「站点与公钥」页调整某 provider 所属分组后，组内 Key 路由范围随之变更；请求组内无部署的模型→可判读错误；未绑组 Key 仍走全部 provider |

## 9. 部署形态与实施顺序

**VPS**：`docker compose`（LiteLLM + Postgres + Caddy）+ host systemd（`wg-quick@wg0`、onboardd、mcp-hub——后两者需执行 `wg` 命令/访问 WG 网络，host 运行免容器提权）。
**站点**：仅 `install.sh`（装 wireguard-tools + 写配置 + systemd 自启），零其他依赖。

**实施顺序（4 个里程碑）：**

| 里程碑 | 内容 | 打通故事 |
|---|---|---|
| D1 | 手工配 WG（首站点）+ LiteLLM + Caddy 上线 | US-P1/P2/P11（+US-P5 的 wg-quick 部分） |
| D2 | Claude Code 联调 + mcp-hub 内建视觉与上传 | US-P3/P4 |
| D3 | onboardd + site-add/site-revoke + 外部 MCP 代理 | US-P7/P8/P12 |
| D4 | 双站点分流演练 + 分组过滤（US-P13：分组页建组、站点页调成员、Key 绑组、客户端伪造 tag 无法越组）+ 全量验收 T1~T13 + runbook | US-P5/P6/P9/P10/P13 收口 |

**前置核对清单（部署前确认）：**

1. deepseek 的 vLLM 启动参数含 `--enable-auto-tool-choice --tool-call-parser <匹配值>`（US-P3 工具调用的上游前提；缺失则 T3 必败）；
2. qwen 服务确认接受 base64 图片消息（OpenAI vision 格式）；
3. VPS 域名 A 记录已指向、443/51820 端口放行；
4. 站点常开机器可 sudo、可出站 UDP。

## 10. 交付物清单（`execution/proto-remote-access/`）

```text
execution/proto-remote-access/
├── vps/
│   ├── docker-compose.yml          # litellm + postgres + caddy
│   ├── caddy/Caddyfile
│   ├── litellm/config.yaml
│   ├── litellm/group_routing.py    # ~30 行 pre-call 钩子：Key→组→路由 tag（US-P13）
│   ├── .env.example                # LITELLM_MASTER_KEY / DATABASE_URL 等占位
│   └── wireguard/wg0.conf.example
├── mcp-hub/                        # ~300 行 Python（fastmcp）
├── onboardd/                       # ~150 行 Python
├── site-tools/
│   ├── site-add.sh  site-revoke.sh  site-list.sh   # 合计 ~150 行
│   └── install.sh.tpl              # ~100 行
└── docs/runbook.md                 # 部署步骤 + T1~T13 验收记录表
```

## 11. 故事覆盖映射（设计 playback，drift 0）

| 故事/约束 | 承接设计章节 |
|---|---|
| US-P1/P2 | §3.1、§4 流1/流2、§5 |
| US-P3 | §4 流3（LiteLLM `/v1/messages` 协议转换） |
| US-P4 | §3.3、§4 流4（形态 A + 上传两步 + 凭据条款） |
| US-P5 | §3.4 install.sh 第4步（wg-quick 自启 + PersistentKeepalive） |
| US-P6 | §3.1 router_settings + 同名多 deployment |
| US-P7 | §3.4 site-add/install.sh/onboardd |
| US-P8 | §3.4 site-revoke + 站点清单 |
| US-P9/P10 | §6（Admin UI / `/key/info`） |
| US-P11 | §3.1 别名条目 + §5 未知模型 |
| US-P12 | §3.3 外部 MCP 注册与代理 |
| US-P13 | §3.1「分组即 tag」要点 + enable_tag_filtering；§3.5 Key 绑定分组 + 分组管理面（分组页 + 站点页 provider↔分组 多选）；§5 组内无部署报错 |
| C1~C5 | §7 安全基线（C3 = §3.5 Key 绑定模型/分组；C4 = §3.1 路由深度） |
| Non-goals | 未引入内容路由/GPU 调度/自助注册/本地 stdio 桥/计费缓存/分组回退/分组预算，均不在本设计 |

## 12. 未决风险

1. **上游 vLLM 工具调用解析质量**：deepseek 的 tool-call parser 与 Claude Code 高频工具调用的兼容性需 D2 实测；若解析不稳，回退方案是换 parser 或升级 vLLM（不影响架构）。
2. **LiteLLM Anthropic 入口的边角兼容**：Claude Code 的部分扩展头/参数可能被 `drop_params` 吞掉——D2 联调时按报错逐项放行。
3. **least-busy 策略在低并发下近似轮询**：属预期行为，不影响 US-P6 验收口径。
4. **US-P13 分组路由依赖一个自写钩子**：免费版 LiteLLM 的 `enable_tag_filtering` 按请求 tag 过滤、不按 Key 自带分组过滤（team-based tag routing 属 Enterprise）；本设计以 ~30 行 pre-call 钩子在网关侧注入组 tag 补齐，钩子须在 D4 双站点演练中验证「客户端伪造 `x-litellm-tags` 无法越组」。若上游免费版后续补齐 key 级 `allowed_tags`（见 litellm#22966），可移除钩子、回归纯配置。

## 13. 参考资料（设计输入）

- `planning/03-core/prototype_remote_model_access_baseline_proto-r5.md`（权威基线，含 r1~r5 修订链）
- `planning/02-working/new_api_analysis.md`（new-api 被否依据：AGPL + 加权随机 LB）
- `planning/02-working/vps_provider_registration.md`（单入口拓扑、SSE 反代要点、SSRF 边界思想）
- LiteLLM Tag Routing 文档 + [litellm#22966](https://github.com/BerriAI/litellm/issues/22966)（US-P13 实现依据：免费版 deployment `tags` + `enable_tag_filtering` 按请求 tag 过滤；Key 自带分组自动作用于路由属 Enterprise team-based tag routing，故加自写 pre-call 钩子补齐）
