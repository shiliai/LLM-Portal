# LLM-portal MVP 设计文档

> **状态**：待批准
> **日期**：2026 年 8 月 13 日
> **需求基线**：`planning/03-core/user_story_baseline_r4.md`（r4，已批准冻结，sha256:94aba9798bb3601a501d14a224ab10950ab377bb16dd3d55a069fe997634ae2a），US-01~US-13、C1~C9、Non-Goals
> **评审过程**：9 个设计节经 Visual Companion 浏览器评审逐节确认（终端答复「确认第 N 节」，2026 年 8 月 13 日）
> **技术路线**：方案 A′ —— TypeScript 全栈自研 + vendor MIT 转换层

## 0. 设计输入（planning 工件）

| 工件 | 用途 |
|---|---|
| `planning/03-core/user_story_baseline_r4.md` | 权威需求基线，所有章节 playback 依据 |
| `planning/02-working/protocol_conversion_oss_survey.md` | 技术路线选型（vendor `@musistudio/llms`、黑盒对照 oracle、许可证排除） |
| `planning/02-working/vps_provider_registration.md` | Provider 三元组抽象、C8 SSRF 双点校验、反向代理 SSE 配置 |
| `planning/02-working/lexdata_ai_analysis.md` | 不可变调用快照、C9 安全基线、元数据/正文分离 |
| `planning/02-working/rtk_analysis.md` | US-09 确定性优化规则 |
| `planning/02-working/sub2api_analysis.md` / `new_api_analysis.md` | urlvalidator 思路、命名转换器思路（LGPL/AGPL 仅参考不搬码） |
| `planning/02-working/token_love_product_spec.md` | 产品对标 |
| GitHub issue #2（shiliai/LLM-Portal） | US-13 故障证据、规范化规则与实测矩阵素材 |

---

## 1. 总体架构与部署形态

单 Node.js 进程（Fastify）承载**数据面、管理面、持久层**三个面，单容器交付，TypeScript 全栈。

```
┌─ Docker 容器（单实例 C6，非 root 最小权限 C9）────────────────────────────┐
│  Node.js (Fastify)                                                       │
│                                                                          │
│  ├─ 数据面（3 个协议入口，虚拟密钥鉴权域）                                  │
│  │   /v1/chat/completions ──┐ OpenAI 入口                                │
│  │   /v1/messages ──────────┤ Anthropic 入口                             │
│  │   /v1/messages/count_tokens ┘（与 /v1/messages 同管线，US-13）          │
│  │        ▼                                                              │
│  │   完整管线：鉴权 → 【US-13 入口规范化(仅 Anthropic 入口)】               │
│  │        → 模型映射 → System Prompt 策略 → 内容优化(可选)                 │
│  │        → 协议转换(converter) → 上游适配器(主备 fallback)                │
│  │        →（出站前 resolved-IP 校验 C8）→ 计量落库                        │
│  │                                                                       │
│  │   /v1/responses ─→ 旁路管线（受管透传 US-11）：                         │
│  │       鉴权 → 模型名改写 → 透传(仅 Responses 能力上游) → usage 计量       │
│  │                                                                       │
│  │   两条通道完成后 ─► 对话存储（按密钥，默认关；US-12/C7）                  │
│  │                     └ [独立任务] 保留期清理（默认 7 天）                 │
│  │                                                                       │
│  ├─ 数据消费接口（数据访问凭据鉴权域，C7/C9）                               │
│  │   SSE 事件流（Last-Event-ID 续传）／ REST 历史查询（分页）               │
│  │                                                                       │
│  ├─ 管理面（管理员会话鉴权域；默认拒绝中间件 C9）                            │
│  │   /admin/api/*（单管理员 C2）＋ React 控制台 SPA（同进程托管静态文件）    │
│  │   Provider 注册表单：保存时 SSRF 校验（C8）                             │
│  │                                                                       │
│  └─ 持久层：SQLite（WAL，better-sqlite3）                                  │
│      配置/密钥哈希/加密凭据 ＋ 调用日志(不可变快照) ＋ 正文存储(独立表)       │
└── 数据卷 /data ──────────────────────────────────────────────────────────┘
```

### 四个关键决策

| # | 决策 | 说明 |
|---|------|------|
| 1 | **Monorepo 三 workspace** | `server`（网关 + 管理 API）、`console`（React SPA）、`converter`（vendor 改造后的协议转换层）。converter 独立成包，**金样测试只针对它跑**。**US-13 规范化器归属 `server` 的 Anthropic 入口层，不进 converter**——它是协议内修复，非跨协议转换。 |
| 2 | **交付形态** | 单镜像、单 compose 服务、单数据卷。首次启动**强制**进入初始化向导：设置管理员密码 → 配置第一个上游 → 创建映射 → 发放密钥（US-10 验收路径；无默认凭据、无跳过路径，C9）。 |
| 3 | **配置热生效** | 模型映射 / 密钥 / 策略全部存 SQLite；进程内存缓存，写操作即时失效缓存，变更**无需重启**（US-03 验收项）。 |
| 4 | **PostgreSQL 后门** | 持久层走薄查询层（手写 SQL、方言隔离），MVP 只发 SQLite、不引入重 ORM；数据量增长时可平移 PostgreSQL。 |

### 容量预期（SQLite 决策依据）

50 名开发者 × 峰值场景下，用量日志约 **50MB/天**，一年约 **18GB**；WAL 模式下单实例写入吞吐是峰值负载的百倍以上；仪表盘查询走**小时/日级 rollup 汇总表**，并配套 **90 天**原始日志保留策略；对话正文（US-12）压缩后约原文 10%~20%，7 天保留期滚动下数 GB 量级，**独立表存放**，不干扰用量统计查询路径。

---

## 2. 请求管线与协议转换层

### 管线阶段（数据面完整管线）

```
入口 handler（协议解析 / schema 校验，按入口协议）
  → 鉴权（虚拟密钥 → 密钥上下文：授权模型、策略配置、US-13 模式开关）
  → 【仅 Anthropic 入口】US-13 规范化器（兼容/严格，规则版本化）
  → 模型映射解析（对外名 → provider + 上游模型名 + 主备链）
  → US-05 System Prompt 策略
  → US-09 内容优化（可选，仅工具输出块）
  → 协议转换层 converter（按「入口协议 × 上游协议」选择命名转换器）
  → 上游适配器（主备 fallback 循环；出站前 C8 resolved-IP 校验）
  → 响应回程：流式/非流式逆向转换 + usage 提取
  → 计量落库（不可变快照）→ 【US-12 开启时】对话存储（异步，不阻塞客户端流）
```

上游错误（4xx/429/5xx）统一按**入口协议**的错误 schema 返回（见第 9 节）。

### 转换矩阵（C1 范围）

| 入口 ↓ ／ 上游 → | OpenAI 兼容上游 | Anthropic 原生上游 |
|---|---|---|
| `/v1/chat/completions` | **passthrough+**（同协议） | `openai_chat_to_anthropic_messages` |
| `/v1/messages`（含 count_tokens） | `anthropic_messages_to_openai_chat` | **passthrough+**（同协议） |
| `/v1/responses` | 受管透传（US-11 旁路，无转换） | 明确错误：该映射不支持此入口 |

- **命名转换器 ID**（可枚举、可单测）；每请求把实际经过的**转换链**记入调用日志快照（C4 可审计）。
- **passthrough+（同协议）**：请求侧仅做模型名改写与策略应用；响应侧**不解码重编码 SSE**——字节原样转发，旁路 tee 解析提取 usage 与事件计量。这是 US-08 缓存语义与流式正确性的最低风险路径。
- **私有扩展参数**：同协议透传给上游；跨协议转换时丢弃并在调用日志记录被丢弃字段名（C1）。

### converter 包（vendor 改造，monorepo 独立 workspace）

| 层 | 内容 |
|---|---|
| 类型契约 | 直接依赖官方 `openai` / `@anthropic-ai/sdk` 的 TS 类型作编译期协议契约 |
| 实现基底 | vendor `@musistudio/llms`（MIT）的转换器进源码树，按 C1 范围裁剪改造；不作 npm 运行时依赖 |
| 中间模型 | UnifiedChatRequest / UnifiedChatResponse 仅用于**跨协议**路径；同协议 passthrough+ 不经过中间模型 |
| 流式状态机 | thinking / text / tool_use 块序转换；每个非空 delta 不丢、块索引正确、`message_start` 不重复（Bifrost 现网缺陷的教训）；`stop_reason ↔ finish_reason` 与 usage 字段映射表文档化 |
| 验证 | **金样测试只针对 converter 包跑**；以 LiteLLM / Bifrost / new-api 作黑盒对照构建跨实现一致性用例（见第 9 节） |

**字节稳定性约束**（US-08/C5）：converter 的序列化路径必须确定性——字段顺序、空白、Unicode 转义全部固定；同输入恒同输出，保证多轮重发历史时转换后前缀逐字节稳定、prompt cache 前缀匹配不被破坏。

### US-13 规范化器（`server` 内，Anthropic 入口专属）

- **位置**：鉴权后、模型映射与一切策略之前；`/v1/messages` 与 `/v1/messages/count_tokens` 共用同一实现（自动压缩看到一致的有效 prompt）。
- **兼容模式（默认）**：将每条内联 `messages[].role=system` 的内容合并进**最近的前一条 user 消息**；无前置 user 消息时生成一条确定性合成 user 条目。实现层面以**结构化内容块操作**完成（不做模糊字符串拼接）；内容块顺序、`tool_use`/`tool_result` ID、`cache_control`、thinking 块全部保持。
- **严格模式（按密钥）**：含内联 system 即按 Anthropic 错误格式 400 拒绝，不转发上游。
- **顶层 `system` 字段**不受规范化影响；US-05 策略在其后独立应用，二者在调用日志中是不同的事件类型，不混淆。
- **规则版本化**：规范化规则带版本（`us13-v1`），版本参与缓存有效性判断（互锁语义同 C5）；每次变换记录脱敏事件（规则版本、受影响消息索引、动作），未开启 US-12 的密钥不落正文。
- **边界**（Non-Goal）：只处理文档化的 Claude Code 内联 system 规则；其他非法角色一律按所选模式的错误路径处理，不做启发式修复。

---

## 3. Provider 注册、模型映射与主备容灾

### 两级模型：Provider 与 ModelMapping 解耦

**先注册 provider，再建映射**——本地 vLLM 与远程上游是同一种东西，只差安全标记。

```
Provider（上游）                      ModelMapping（映射）
├─ name / base_url                   ├─ 对外模型名（唯一）
├─ 协议能力（多选）：                  └─ 有序目标列表：
│    chat_completions                     [0] provider_id + 上游模型名   ← 主
│    anthropic_messages                   [1] provider_id + 上游模型名   ← 备选1
│    responses                            [2] …                        ← 备选2
├─ 凭据（主密钥加密存储，C9）
├─ 安全标记：allow_private_loopback / allow_insecure_http（默认均关，C8）
└─ 「测试连接」动作（保存时可选，验证凭据与连通性）
```

**协议能力决定入口可路由性**（避免静默失败）：

| 入口 | 可路由的 provider 能力 | 方式 |
|---|---|---|
| `/v1/chat/completions` | chat_completions ／ anthropic_messages | 同协议 passthrough+ ／ 跨协议转换 |
| `/v1/messages` | anthropic_messages ／ chat_completions | 同协议 passthrough+ ／ 跨协议转换 |
| `/v1/responses` | **仅** responses | 受管透传（US-11） |

请求命中的映射目标不满足入口要求时，返回可诊断错误（说明该映射不支持此入口）。

### C8 SSRF 校验：双点实施（独立 url-validator 模块）

**保存时**（管理面 provider 表单）：
1. scheme：默认仅 HTTPS；HTTP 需显式开启 `allow_insecure_http`（表单提示：仅限本地/内网可信端点）
2. host 解析 → 回环 / 私网 / 链路本地 / 未指定 IP **默认拒绝**
3. 显式开启「允许私网/回环」后放行（仅管理员可操作，C2 天然满足）
4. 校验失败返回具体原因（哪个 IP、哪类网段、开哪个开关可放行）——可诊断错误（US-03 验收）

**请求时**（上游适配器出站前）：DNS 解析后对 **resolved IP** 复核，防 DNS rebinding（TOCTOU）；已显式开启「允许私网/回环」的 provider 跳过复核。思路借鉴 sub2api urlvalidator（LGPL，只借鉴思路不搬代码）。

### 主备容灾（US-04）

- **触发切换**：连接失败 / 超时 / 5xx / 429（可重试类）。4xx 业务错误（如 schema 校验失败）**不切换**，直接按入口协议返回。
- **流式边界**：向客户端发出第一个 SSE 事件**之前**失败可切换到备选；之后中断按错误结束（无法对已开始的流重试）。
- **fallback 重入点**：从转换层重入——备选 provider 协议可能与主不同，每次尝试按目标协议重新出站转换（策略层 US-05/US-09/US-13 的结果复用，不重复应用）。
- **Responses 入口**：仅在同样具有 responses 能力的备选之间 fallback（US-11 边界）。
- **切换事件**：时间、原因、源/目标 provider、每次尝试的状态 → 写入调用日志的不可变尝试链快照（第 6 节），并进控制台事件流（US-04 验收）。
- **全部失败**：返回最后一个上游的错误 + 聚合诊断（各次尝试的 provider 与错误摘要）。
- **缓存计量交叉**：切换后新上游是冷缓存，cache usage 按实际上游返回记录，不做跨上游换算（第 6 节计量口径）。

**不做**（YAGNI，Non-Goals 对齐）：主动健康探测轮询、成本/延迟加权选路、熔断器。被动 fallback + 事件流 + 仪表盘错误率已满足 US-04 全部验收。

### 热生效与凭据告警

- provider / 映射的增删改即时生效（进程内缓存写时失效；US-03 验收）。
- 保存映射时校验目标 provider 凭据存在性；缺失或「测试连接」失败 → 控制台可见告警（US-03 验收）。

---

## 4. 内容策略：System Prompt / 内容优化 / 缓存治理

### 总则：统一的「前缀稳定性」框架（C4 + C5）

网关仅有的三个请求修改点 **US-13（规范化）→ US-05（system 策略）→ US-09（内容优化）** 都作用在缓存前缀上，共用同一套纪律：

1. **确定性**：纯函数变换——同输入恒同输出，不依赖时间戳/随机数/请求上下文；
2. **版本化**：各自独立版本号（`us13-vN` / 策略配置版本 / `us09-vN`），**版本三元组记入每条调用日志快照**（C4 可审计）；
3. **缓存互锁**：任一版本变更 = 前缀改变 = 宣告一次缓存失效，事件被记录且控制台可见（C5）；
4. **管线顺序固定**（第 2 节）：US-13 → US-05 → US-09 → 转换，后一级的输入是前一级的输出。

### US-05 System Prompt 策略

- **抽象层**：策略作用于「有效 system prompt」规范化表示——OpenAI 入口取 `role=system` 消息，Anthropic 入口取顶层 `system` 字段（US-13 已把内联 system 并入 user，不属于本层输入）；应用后按上游协议写回。
- **三种动作**：**注入**（策略内容前置，用户原文保留）／**追加**（用户原文后附加）／**替换**（只保留策略内容）。
- **优先级规则**（文档化，US-05 验收）：**密钥策略 > 模型路由策略，取最具体的一个生效，不叠加**。控制台在两者同时配置时显示实际生效者。
- **建模**：两级策略同存 `policies` 表（第 6 节），行以 `virtual_key_id` **或** `model_mapping_id` 二选一外键定位挂载点，避免两套建模。
- **缓存交互**：策略内容对同密钥恒定 → 注入/替换后前缀仍稳定；用户块上已有的 `cache_control` 原样保留，策略注入块本身不带断点（断点归 US-08 管）。策略内容修改 = 配置版本 +1 → 触发 C5 失效宣告。

### US-09 内容优化管道（默认关，按密钥开）

- **三条确定性规则**（rtk 调研凝练，规则集版本 `us09-v1`）：
  1. ANSI 转义序列剥离；
  2. 连续重复行折叠（N 行相同 → 1 行 + `[重复 N 次]` 标注）；
  3. 超大工具输出块截断（保头保尾 + 截断标记，阈值可配，默认给出保守值）。
- **作用范围**：仅 `tool_result` 块（Anthropic）／`role=tool` 消息（OpenAI）。system / user / assistant 消息与工具调用**参数**一律不动（US-09 验收）。
- **多轮字节稳定**：客户端每轮重发原始历史时，相同历史块得到逐字节相同的变换结果——纯函数保证，无需缓存变换结果。
- **观测先行**：token 构成统计（工具输出占比、超大块 Top-N）对**所有**密钥被动收集（只统计尺寸，不留正文），仪表盘展示，供管理员决策开启；开启后展示该密钥优化前后 token 对比（估算口径第 6 节）。

### US-08 Prompt Cache 治理

- **透传**：Anthropic 入口 → Anthropic 上游走 passthrough+，`cache_control` 断点字节原样到达上游；响应 usage 的 `cache_creation_input_tokens` / `cache_read_input_tokens` 记入计量。OpenAI 兼容上游的缓存自动生效（无断点概念），`prompt_tokens_details.cached_tokens` 同样记录。
- **跨协议缓存语义**：OpenAI 入口 → Anthropic 上游依赖 converter 的字节稳定序列化保证多轮前缀匹配；OpenAI 协议无断点字段，断点靠下述自动注入。
- **自动注入策略**（按密钥/路由，仅对 Anthropic 原生上游生效）：客户端未携带 `cache_control` 时按策略注入断点，选项：
  - `off`（默认）——不注入；
  - `system`——system 尾部注入 1 个断点；
  - `system+tail`——system 尾部 + 最后一个完整对话轮次边界各 1 个（长会话滚动复用）。
  - 注入位置由请求形状确定性推导；断点总数不超过 Anthropic 上限 4。
- **仪表盘**：命中率 =（缓存读 token ／ 总输入 token），节省估算按上游定价折扣（读 0.1×、写 1.25×，价格表挂在模型配置，第 6 节口径）；C5 失效宣告事件在控制台时间线可见。

**不做**（YAGNI / Non-Goals 对齐）：精确响应缓存与语义缓存；LLM 二次摘要；rtk 式 Agent 端优化（网关仅度量）；启发式/概率性的任何变换。

---

## 5. 密钥、鉴权与限额

### 三个相互独立的鉴权域（C9）

| 鉴权域 | 保护面 | 凭据形态 | 存储 |
|---|---|---|---|
| 管理员会话 | `/admin/api/*` + 控制台 | 用户名/密码（C2 单管理员）→ 会话 token | 密码 **Argon2id**；会话表存 SQLite（重启不掉线，空闲 24h 过期） |
| 虚拟调用密钥 | 数据面 3 入口 | `sk-portal-…`（可识别前缀） | **HMAC(服务端 pepper) 哈希** + 前缀预览，明文创建后不可再读 |
| 数据访问凭据 | US-12 SSE / REST 数据接口 | `dk-portal-…`（独立前缀） | 同虚拟密钥的哈希方案 |

- 三域凭据互不通用：拿虚拟密钥调管理 API、拿数据凭据调数据面，一律 401。
- 管理 API 走**默认拒绝中间件**：路由未显式标记为公开（仅登录、初始化向导、健康检查），一律要求管理员会话。
- 登录接口做固定窗口失败限速（暴力破解防护，C9 精神的低成本补充）。

### 主密钥（master key）

首次启动在 `/data` 卷生成随机主密钥文件（权限 0600），派生两个用途：
1. 上游 provider 凭据的 **AES-256-GCM** 加密（C9：加密存储、任何输出不回显）；
2. 虚拟密钥／数据凭据哈希的 **HMAC pepper**（拖库后无法离线碰撞）。

主密钥随数据卷备份迁移；丢失则上游凭据不可恢复（文档明示，重新录入即可）。

### 虚拟密钥模型（US-06）

```
VirtualKey
├─ name / 前缀预览（sk-portal-ab12…，列表识别用）
├─ 状态：active / revoked（吊销即软删除，历史计量经 key stable ID 保留）
├─ 模型范围：授权的对外模型名列表（或全部）
├─ 速率限制：RPM / TPM（滑动窗口，进程内计数器，单实例 C6 足够）
├─ 额度：周期内估算成本上限（周期：日／月／总计）
└─ 按密钥策略挂载点：US-05 策略、US-08 自动注入、US-09 开关、
   US-12 保存开关、US-13 兼容/严格模式
```

- **鉴权路径**：`Authorization: Bearer`（OpenAI 入口）／`x-api-key`（Anthropic 入口）→ HMAC 哈希查表 → 密钥上下文（授权模型 + 限额状态 + 全部按密钥策略）。上下文进程内缓存、写时失效——**吊销立即生效**（US-06 验收）。
- **越权模型**：请求模型不在授权范围 → 按入口协议返回权限错误（US-01 验收复用同一路径）。

### 限额与速率（US-06「可区分」验收）

| 类型 | 检查时机 | 超限响应 |
|---|---|---|
| 速率（RPM/TPM） | 请求前预检 | HTTP 429，错误 code `rate_limit_exceeded`（按入口协议错误 schema） |
| 额度（成本上限） | 请求前预检（基于已累计值） | HTTP 429，错误 code `insufficient_quota`，消息明确区分于限流 |

- usage 在响应完成后累计（流式按聚合计），允许最后一笔请求越界——预检口径文档化，避免为精确卡线引入分布式计数复杂度。

### 日志红线（C9）

调用日志与应用日志只记录 **key stable ID + 前缀预览**；Authorization 头、x-api-key、完整密钥、上游凭据永不落日志、错误信息与导出。

**不做**（YAGNI）：密钥自动轮换、密钥级 IP 白名单、多管理员/RBAC（Non-Goals）、外部 IdP/SSO。

---

## 6. 数据模型与计量

### SQLite 表族总览（WAL，薄查询层）

| 组 | 表 | 说明 |
|---|---|---|
| 配置 | `providers` / `model_mappings` / `virtual_keys` / `policies` / `prices` / `settings` | 可变、热生效；写操作即时失效进程内缓存 |
| 计量 | `call_logs`（**不可变快照**） | 每请求一行，只插入不更新（终态一次写入） |
| 汇总 | `rollup_hourly` / `rollup_daily` | 按（密钥 × 模型 × 时段）聚合，仪表盘只查这里 |
| 事件 | `events`（typed） | US-04 切换、C5 缓存失效宣告、US-13 变换（脱敏）、US-12 持久化失败 |
| 正文 | `bodies` | US-12 压缩正文，独立表（第 7 节）；与 `call_logs` 一对一可空 |
| 鉴权 | `admin_sessions` / `data_credentials` | 第 5 节 |

### 不可变调用快照（lexdata P0 教训）

`call_logs` 每行**冻结请求发生时刻的全部归因事实**，历史展示永不回查当前配置：

```
call_logs
├─ 身份：key_stable_id + key_prefix_preview（密钥可吊销，快照仍可读）
├─ 路由：对外模型名、命中映射快照、实际上游 provider 名/上游模型名、
│        尝试链 JSON（每次尝试的 provider、错误摘要、切换原因 → US-04）
├─ 转换：converter 链 ID、版本三元组（us13 / us05 策略版本 / us09）→ C4 审计
├─ 用量：input / output / cache_read / cache_write tokens、
│        usage_estimated 标记、优化前后 token 对（US-09 开启时）
├─ 成本：估算成本 + 计价时用的价格版本（成本请求时算定并冻结，不随价格表变动重算）
├─ 观测：工具输出字节占比、超大块 Top-N 尺寸（仅尺寸，无正文 → US-09 决策数据）
└─ 状态：HTTP 状态、错误类别、入口协议、流式标记、延迟（TTFB/总耗时）、时间戳
```

### 计量口径（US-07 验收）

- **上游 usage 为准**：Anthropic `usage.*` / OpenAI `usage.*`（含 `prompt_tokens_details.cached_tokens`）直接记录。
- **缺失时估算**：字符数 ÷ 4 的文档化启发式，行记 `usage_estimated=true`，仪表盘标注「估算值」。
- **价格表**：管理员按映射目标（provider × 上游模型）配置每百万 token 单价（输入/输出/缓存读/缓存写，默认缓存读 0.1×、写 1.25× 输入价可一键带出）；价格修改产生新价格版本，只影响之后的请求。
- **缓存指标**（US-08）：命中率 = cache_read ÷ 总输入；节省 = cache_read ×（输入价 − 缓存读价）；按密钥/模型/日聚合进 rollup。

### 写路径与汇总

- **热路径不等库**：计量写入走进程内队列，批量事务落库（WAL 单写者友好）；队列积压/失败计数暴露给控制台。
- **rollup 生成**：定时任务（每小时）从 `call_logs` 增量聚合到 `rollup_hourly`，日表由小时表二次聚合；仪表盘查询只打 rollup，调用日志明细页才查原始表。
- **保留策略**：原始 `call_logs` 默认保留 90 天（可配），到期清理任务删除；rollup 长期保留（体积极小）；`bodies` 按 C7 独立保留期（默认 7 天，第 7 节）。

### 仪表盘与日志查询（US-07 验收路径）

- 按密钥 / 模型 / 日的请求数、token、估算成本（rollup）；
- 调用日志列表按密钥 / 模型 / 状态 / 时间筛选分页（原始表，走索引 `(key_stable_id, ts)`、`(model, ts)`、`(status, ts)`）；
- 单条日志详情展开：尝试链、版本三元组、usage 明细；已存正文时（US-12）内嵌只读查看（第 7 节）。

**不做**（YAGNI）：预算告警推送、专门的账单导出子系统（CSV 导出留作 rollup 表的平凡查询）、跨实例聚合（C6 单实例）。

---

## 7. 对话保存与数据消费接口

### 正文存储（按密钥开启，默认关）

- **采集点**：管线末端 tee——非流式取完整请求/响应体；流式用与计量共用的聚合器把 SSE 增量聚合为完整响应（US-12「按聚合后的完整响应计」）。
- **存储**：`bodies` 表与 `call_logs` 一对一可空；请求体与响应体分别 **gzip 压缩**存储（压后约原文 10%~20%）；检索索引（密钥/模型/时间/状态）全部借道 `call_logs`，正文表只按 log id 取。
- **异步落库，永不反压客户端**（r4 新增验收）：客户端流完成后才入持久化队列；写失败**不中断、不回滚**已完成的客户端响应，只递增持久化失败计数（`events` 表 + 控制台可见）。
- **未开启的密钥**：正文一个字节不落（C7），只有 US-07 元数据快照。

### 保留与清理（C7）

- 保留期全局可配，默认 **7 天**；清理任务（与 90 天日志清理同一调度框架）按到期时间删除正文行；
- 控制台显示：当前正文存储用量（表体积统计）、保留期设置、持久化失败计数。

### 数据消费接口（独立数据访问凭据域，C9）

两个接口挂在 `dk-portal-` 凭据下，与虚拟密钥/管理员会话完全隔离：

**SSE 实时事件流 `GET /data/v1/stream`**

- 新保存记录产生事件：`id`（**单调递增序列号**，取 `bodies` 自增主键）、`event: conversation.saved`、data 为记录元数据 + 正文引用（或内联正文，按 `?include_body=true`）；
- 过滤参数：`?key=<stable_id>&model=<name>`（可多值）；
- **断线续传**（r4 新增验收）：重连携带 `Last-Event-ID` → 服务端从该序列号之后**查库回放**错过的事件，再切换到实时推送；回放窗口即正文保留期（7 天），超窗事件已被清理、从现存最早记录续起（文档化）；
- 心跳注释行（`: ping`）每 15s，防中间层空闲断连。

**REST 历史查询 `GET /data/v1/conversations`**

- 参数：时间范围、密钥、模型、分页（基于序列号的 keyset 分页，避免深翻页）；
- 单条详情 `GET /data/v1/conversations/:id` 返回解压正文；
- MVP：数据凭据可访问全部已保存记录；按凭据再细分范围留作后续，不在基线。

### 控制台只读查看（US-12 验收）

调用日志详情页：该记录存有正文时显示「查看对话」，解压渲染只读视图（消息列表形态）；无正文时不显示入口。管理面走管理员会话域，与数据凭据域互不越权（C9）。

**不做**（YAGNI / Non-Goals）：正文全文检索、语义检索、按数据凭据细分数据范围、导出打包、对话单条编辑/删除（只有保留期整体清理）。

---

## 8. 安全与部署

### 交付物（US-10 / C3）

- **单镜像**：多阶段构建——`console` SPA 构建产物由 Fastify 托管静态文件；`converter` 与 `server` 同进程打包。
- **单 compose 服务 + 单数据卷**：

```yaml
services:
  llm-portal:
    image: llm-portal:latest
    ports: ["8080:8080"]          # 仅一个端口：数据面 + 管理面 + 数据接口
    volumes: ["./data:/data"]     # SQLite + 主密钥文件，重启全量保留
    restart: unless-stopped
    # 无 privileged / 无 host network / 无 host PID / 无 docker.sock（C9）
```

- 环境变量只放非机密项（端口、日志级别）；机密（主密钥）生成于 `/data` 卷内，不进 env 与镜像层。
- **升级**：换镜像重启；启动时自动跑**编号式 schema 迁移**（前向迁移，启动失败即回滚镜像）。
- **备份**：文档给出两种方式——WAL checkpoint 后复制 `/data`（冷备），或 `sqlite3 .backup` 在线备份（热备）；主密钥文件随卷同备。

### 强制初始化向导（US-10 r4 验收 + C9）

- 数据库无管理员记录 = **未初始化态**：所有管理页面与管理 API 均重定向到向导，**无默认凭据、无跳过路径**；数据面入口在未初始化时直接 503（尚无密钥可用，明确提示先完成初始化）。
- 向导四步：设置管理员密码（Argon2id）→ 注册第一个上游（含 C8 校验与「测试连接」）→ 创建第一条映射 → 发放第一把密钥；
- 完成页展示**可复制的示例命令**（OpenAI curl 与 Anthropic curl 各一条，指向本网关），跑通即验收（US-10「半小时内第一个请求」）。

### 容器与运行时加固（C9，lexdata 反例逐条对治）

| lexdata 现网风险 | 本设计 |
|---|---|
| root 容器 | 镜像内建非 root 用户（固定 UID），文件系统只写 `/data` 与 `/tmp` |
| host network | 普通 bridge 网络，仅暴露 8080 |
| Docker socket 挂载 | 禁止；compose 模板不含任何宿主敏感挂载 |
| 默认凭据 | 不存在（强制向导） |
| 日志泄漏 | 结构化日志红线：正文、Authorization/x-api-key、上游凭据、完整虚拟密钥永不输出（第 5 节红线的运行时实施） |

- `/healthz` 公开（默认拒绝中间件的显式豁免，仅返回进程与 DB 可用性，不含配置信息）。

### 反向代理指引（文档交付）

网关容器内提供 HTTP，生产建议前置 nginx/caddy 做 TLS。文档附样例，关键项：

```nginx
location / {
  proxy_pass http://127.0.0.1:8080;
  proxy_buffering off;            # SSE 必需：禁缓冲
  proxy_read_timeout 3600s;       # 长流式响应不被掐断
  proxy_set_header Connection ""; # HTTP/1.1 keep-alive
  underscores_in_headers on;      # 带下划线的自定义头不被丢弃
}
```

- caddy 样例同附（默认即 SSE 友好）；
- 文档明示：C6 单实例——不要在代理后挂多个网关实例做负载均衡（SQLite 单写者 + 进程内限流计数器均不支持）。

**不做**（YAGNI / Non-Goals）：高可用集群、K8s chart、自动 TLS（交给前置代理）、内置防火墙/WAF、信创/等保适配。

---

## 9. 错误处理与测试策略

### 统一错误模型

内部错误一律先归类为**错误类别枚举**，再按**入口协议**渲染出站格式——处处一致（US-01「可诊断、不静默失败」的系统性保证）：

| 类别 | 触发 | HTTP | 渲染 |
|---|---|---|---|
| `authentication` | 密钥无效/吊销/跨域使用 | 401 | OpenAI：`{error:{message,type,code}}`；Anthropic：`{type:"error",error:{type,message}}` |
| `permission` | 模型不在授权范围 | 403 | 同上，按入口 schema |
| `invalid_request` | schema 校验失败、US-13 严格模式拒绝、映射不支持该入口 | 400 | 同上（US-13 按 Anthropic 格式） |
| `not_found_model` | 模型名无映射 | 404 | 消息含模型名与「未配置映射」提示 |
| `rate_limit` / `insufficient_quota` | 第 5 节 | 429 | code 区分（US-06） |
| `upstream_error` | 全部上游失败（第 3 节聚合诊断） | 透传最后上游状态码 | 消息含每次尝试摘要；原始上游错误全文进调用日志 |
| `internal` | 网关自身异常 | 500 | 通用消息 + 日志关联 ID（正文/凭据红线仍生效） |

**流式中途错误**：首事件发出前失败走标准 HTTP 错误（可 fallback，第 3 节）；流已开始后失败按入口协议发**流内错误事件**（Anthropic `event: error`；OpenAI 发含 error 的 chunk 后断流），调用日志记「部分完成」状态。

**超时**：上游连接 / 首字节 / 流空闲三个超时独立可配，默认保守（10s / 60s / 120s 为示意值，实现阶段定稿；US-09 截断阈值同此约定），超时归为可重试类参与 fallback。

### 测试分层（自内向外）

| 层 | 对象 | 要点 |
|---|---|---|
| **1. 金样测试** | `converter` 包（独立跑） | 请求/响应/流式事件序列的固定输入输出对；覆盖工具调用、图像、thinking、stop_reason↔finish_reason、usage 映射；**字节稳定性断言**（同输入重复转换 byte-equal，US-08/C5） |
| **2. 黑盒对照** | converter vs LiteLLM / Bifrost / new-api | 研究阶段用三个开源实现作 oracle 生成/校准金样（吸收其修复史，如 Bifrost message_start 重复缺陷）；对照脚本不进 CI 依赖 |
| **3. 单元测试** | US-13 规范化器（issue #2 全部规则+边界）、US-05 优先级、US-09 变换（确定性/幂等/字节稳定 property 测试）、url-validator（C8 用例表：回环/私网/链路本地/rebinding）、限额计数、密钥哈希 | 纯函数层，快速全覆盖 |
| **4. 集成测试** | Fastify inject + **mock 上游 stub**（模拟两协议的正常/流式/错误/慢响应/畸形 SSE） | 管线端到端：鉴权三域、映射、fallback 尝试链、计量快照落库、US-12 存储与 SSE `Last-Event-ID` 续传、初始化向导强制态 |
| **5. 安全断言** | C8/C9 | 默认拒绝中间件路由审计（枚举全部路由断言豁免表）；日志红线扫描（集成测试注入已知密钥/正文，断言日志输出零泄漏） |
| **6. E2E 工具矩阵** | 真实客户端 | **Claude Code** → 网关 → Anthropic 上游 / OpenAI 兼容上游（含长会话内联 system 复现，指向严格 schema 的 vLLM，US-13 验收）；**Codex** → Responses 透传（US-11）；OpenAI/Anthropic SDK 冒烟 |

- **CI**：层 1/3/4/5 每次提交必跑；层 6 为发布前人工清单（真实凭据不进 CI）。

**不做**（YAGNI）：性能压测基准套件（容量估算见第 1 节，MVP 不建基准框架）、混沌工程、模糊测试（畸形 SSE 用例并入 stub 即可）。

---

## 10. 基线验收 ↔ 设计章节 ↔ 测试层 映射

| 基线条目 | 设计章节 | 测试层 |
|---|---|---|
| US-01 OpenAI 入口 | §2 §9 | 1, 4, 6 |
| US-02 Anthropic 入口 | §2 | 1, 4, 6 |
| US-03 映射管理 + provider 注册 | §3 | 3（url-validator）, 4, 6 |
| US-04 主备容灾 | §3 §6 | 4（mock 上游错误注入） |
| US-05 System Prompt 策略 | §4 | 3, 4 |
| US-06 密钥与限额 | §5 | 3, 4 |
| US-07 用量与成本 | §6 | 4（快照落库断言） |
| US-08 Prompt Cache | §4 §6 | 1（字节稳定）, 4, 6（真实上游命中） |
| US-09 内容优化 | §4 §6 | 3（property）, 4 |
| US-10 部署与初始化 | §8 | 4（强制初始化态）, 6（compose 部署清单） |
| US-11 Responses 透传 | §2 §3 | 4, 6（Codex） |
| US-12 对话保存与消费 | §7 | 4（存储 + SSE 续传） |
| US-13 入口规范化 | §2 | 3（规则全覆盖）, 4, 6（Claude Code + vLLM） |
| C1 协议范围 | §2 | 1 |
| C2 单管理员 | §5 | 4 |
| C3 自部署单租户 | §8 | 6 |
| C4 修改边界（版本三元组审计） | §2 §4 §6 | 3, 4 |
| C5 优化与缓存互锁 | §4 | 3, 4 |
| C6 单实例 | §1 §8 | —（文档约束） |
| C7 对话保存边界 | §7 | 4 |
| C8 SSRF 边界 | §3 | 3（用例表）, 4 |
| C9 安全基线 | §5 §8 | 5（路由审计 + 红线扫描） |

实现阶段要求：每条 Given/When/Then 至少落在上表对应的一层测试中，Task 验收引用本表。

---

## 11. 未决风险与实现注意事项

1. **vendor 改造工作量**：`@musistudio/llms` 转换器裁剪进源码树的实际工作量存在不确定性；以金样测试倒逼裁剪范围，先跑通 US-01/US-02 主路径再扩边界能力。
2. **Responses 透传的 usage 提取**：依赖上游 Responses 流事件中 usage 的出现位置与格式一致性；tee 解析器需容忍缺失（落 `usage_estimated`）。
3. **计量队列积压**：极端峰值下进程内队列可能积压；已设计积压/失败计数暴露控制台，实现时补背压上限（丢弃优先级：先丢 rollup 增量、不丢原始快照）。
4. **协议演进**：两家协议新增字段时金样需更新；金样目录按协议版本组织，新增字段先进 passthrough 路径观察再进转换矩阵。
5. **E2E 环境成本**：层 6 需要真实凭据与本地 vLLM 环境；发布前清单以脚本半自动化，结果人工确认。

---

## 批准记录

- 逐节确认：第 1~9 节经 Visual Companion 浏览器评审 + 终端逐节确认（2026 年 8 月 13 日，本会话）。
- 最终文档批准：待用户对渲染后的本文档（SHA-256 见批准时记录）作出明确批准后，此处补记批准证据。
