# LLM-portal 用户故事基线 r4

> **baseline_revision**: r4（已批准，已冻结）
> **权威来源**:
> - planning/03-core/user_story_baseline_r3.md (sha256:1c77d51968…)——US-01~12、C1~C8 逐字继承
> - planning/02-working/token_love_product_spec.md (sha256:b2f8e340b5f6…)
> - planning/02-working/rtk_analysis.md (sha256:d7cdfa67ebc6…)
> - planning/02-working/vps_provider_registration.md（r3 吸收，C8 来源）
> - GitHub issue #2（shiliai/LLM-Portal，body sha256:ffaa1fb217…）——US-13 的故障证据与验收素材
> - planning/02-working/lexdata_ai_analysis.md (sha256:34bbb591d7…)——C9 安全清单来源
> - r4 新增需求问答记录（本会话，2026 年 8 月 13 日，3 项决策：US-13 入基线并修订 C4 / 默认兼容模式、按密钥可切严格 / 安全清单凝练为新约束 C9）
> **角色定义**: 「管理员」= 中小企业 IT/运维负责人；「开发者」= 企业内部使用网关虚拟密钥的开发人员或应用

---

## US-01 OpenAI 入口统一调用

As a **开发者**, I want to **用 OpenAI 协议/SDK 通过虚拟密钥调用网关暴露的统一模型名**, so that **不改业务代码即可使用任意上游模型**。

- **Given** 管理员已配置模型映射（对外名 → 上游供应商/模型）并发放虚拟密钥
  **When** 开发者以 OpenAI 协议请求 `/v1/chat/completions`（含非流式、SSE 流式、tool calls、文本与图像内容）
  **Then** 网关路由到映射上游（无论其为 OpenAI 兼容还是 Anthropic 原生），返回合法 OpenAI 协议响应，流式增量与 tool_calls 语义正确
- **Given** 请求的模型名未配置映射 **When** 发起调用 **Then** 返回明确的错误码与可诊断信息（不静默失败）
- **Given** 密钥未被授权使用该模型 **When** 发起调用 **Then** 返回权限错误

## US-02 Anthropic 入口统一调用

As a **开发者**, I want to **把 Anthropic 生态工具（Claude Code、Anthropic SDK）的 base URL 指向网关直接使用**, so that **Anthropic 工具链零改造接入统一网关**。

- **Given** 已配置映射与密钥
  **When** 以 Anthropic 协议请求 `/v1/messages`（含 SSE 流式、tool_use、system 字段、文本与图像内容）
  **Then** 返回合法 Anthropic 协议响应；上游为 OpenAI 兼容时字段双向正确转换（含 stop_reason/finish_reason、usage 映射）
- **Given** Claude Code 以网关为 base URL 运行 **When** 执行一次带工具调用的会话 **Then** 会话正常完成，工具调用往返无损

## US-03 模型映射管理

As a **管理员**, I want to **在 Web 控制台维护「对外模型名 → 上游供应商/模型」映射**, so that **切换底层供应商时调用方无感知**。

- **Given** 管理员登录控制台 **When** 新增/修改/删除映射并保存 **Then** 变更即时生效（无需重启），后续请求按新映射路由
- **Given** 映射指向的上游凭据缺失或无效 **When** 保存 **Then** 控制台给出可见告警
- **Given** 管理员注册上游，其 base URL 为本地 vLLM（`127.0.0.1`/内网）或远程 HTTPS 端点 **When** 保存 **Then** 远程默认拒绝回环/私网地址且仅允许 HTTPS；本地显式开启「允许私网/回环」开关后放行，两者均在保存时给出可诊断校验错误

## US-04 主备容灾

As a **管理员**, I want to **为对外模型名配置主上游和有序备选上游**, so that **单一上游故障/限流时业务不中断**。

- **Given** 已配置主备 **When** 主上游返回可重试错误（5xx/超时/429） **Then** 网关自动切换到下一备选完成本次请求，并记录切换事件（时间、原因、源/目标上游）
- **Given** 全部上游失败 **When** 发起调用 **Then** 返回最后一个上游的错误与聚合诊断信息
- **Given** 发生过切换 **When** 管理员查看控制台 **Then** 能看到切换事件流

## US-05 System Prompt 策略

As a **管理员**, I want to **按密钥或模型路由配置 system prompt 的注入/追加/替换**, so that **统一管控合规话术与角色设定，开发者无须改动代码**。

- **Given** 某密钥配置了「注入」策略 **When** 该密钥发起请求 **Then** 上游收到的 system prompt 前部包含注入内容，用户原文保留
- **Given** 配置了「替换」策略 **When** 请求携带任意 system prompt **Then** 上游只收到策略指定的内容
- **Given** 密钥与模型路由同时配置策略 **Then** 生效优先级有明确文档化规则且行为一致

## US-06 虚拟密钥与限额

As a **管理员**, I want to **发放/吊销虚拟密钥，绑定可用模型范围，设置额度与速率限制**, so that **按团队/应用隔离用量并控制成本**。

- **Given** 管理员创建密钥并绑定模型范围与限额 **When** 开发者使用该密钥 **Then** 只能调用授权模型
- **Given** 密钥达到额度或速率上限 **When** 继续调用 **Then** 返回明确的限额错误（可区分限流 vs 额度耗尽）
- **Given** 密钥被吊销 **When** 再次调用 **Then** 立即拒绝

## US-07 用量与成本可见

As a **管理员**, I want to **在仪表盘按密钥/模型/时间维度查看 token 用量与成本**, so that **对内分摊成本、对外核对账单**。

- **Given** 网关处理过请求 **When** 管理员打开仪表盘 **Then** 可见按密钥、按模型、按日的请求数/token 数/估算成本，以及调用日志（可按密钥/模型/状态筛选）
- **Given** 上游返回 usage 数据 **Then** 计量以上游 usage 为准；缺失时有文档化的估算规则并标注为估算值

## US-08 Prompt Cache 治理

As a **管理员**, I want to **网关正确处理 prompt cache 并展示命中率与节省**, so that **长上下文 Agent 场景的成本可控可见**。

- **Given** 客户端请求携带 cache_control **When** 路由到 Anthropic 原生上游 **Then** 缓存断点透传，响应中的缓存 usage（写入/命中）被记录
- **Given** 密钥/路由配置了自动注入策略 **When** 客户端未携带 cache_control **Then** 网关按策略在稳定前缀处注入断点
- **Given** 发生缓存写入与命中 **When** 查看仪表盘 **Then** 可见命中率与节省成本估算
- **Given** OpenAI 入口 → Anthropic 上游 **Then** 协议转换保留缓存语义（转换正文的字节稳定性不破坏前缀匹配）

## US-09 内容优化管道（可选）

As a **管理员**, I want to **对指定密钥开启保守的请求内容优化（ANSI 剥离/连续重复行折叠/超大工具输出块截断）并查看节省效果**, so that **编码 Agent 流量的 token 成本下降且不引入正确性风险**。

- **Given** 优化默认关闭 **When** 未显式开启 **Then** 请求内容逐字节原样转发
- **Given** 对某密钥开启优化 **When** 请求含工具输出（tool_result/tool 角色消息） **Then** 仅这些块被确定性变换（同输入恒同输出），system/用户/助手消息与工具调用参数不被修改
- **Given** 开启优化且多轮会话 **When** 客户端每轮重发原始历史 **Then** 变换后前缀逐字节稳定，prompt cache 命中不受破坏；规则版本变更时缓存失效被记录并在控制台可见
- **Given** 优化开启 **When** 查看仪表盘 **Then** 可见该密钥优化前后 token 对比
- **Given** 网关观测到工具输出占比 **Then** 仪表盘展示 token 构成（工具输出占比、超大块 Top-N），供管理员决策是否开启优化

## US-10 部署与初始化

As a **管理员**, I want to **用 Docker 一键部署并在浏览器完成初始化**, so that **无需专业运维也能半小时内跑通第一个请求**。

- **Given** 一台有 Docker 的服务器 **When** 执行文档中的单条 compose 命令 **Then** 网关与控制台启动
- **Given** 首次访问控制台 **When** 完成初始化向导（设置管理员密码 → 配置第一个上游 → 创建映射 → 发放密钥） **Then** 用向导展示的示例命令能成功完成一次真实调用
- **Given** 服务重启 **Then** 全部配置与用量数据持久保留
- **Given** 首次启动尚未初始化 **When** 访问任何管理页面或管理 API **Then** 只能进入初始化向导且必须完成管理员密码设置（无默认凭据、无跳过路径）

## US-11 OpenAI Responses 入口（受管透传，Codex 接入）

As a **开发者**, I want to **把 Codex 等使用 OpenAI Responses API 的工具的 base URL 指向网关**, so that **在统一密钥、映射与计量之下使用 Responses 生态工具**。

- **Given** 对外模型名映射到支持 `/v1/responses` 的 OpenAI 兼容上游
  **When** 以 Responses 协议请求（含非流式、SSE 流式、工具调用）
  **Then** 网关做受管透传：虚拟密钥鉴权、模型名按映射改写、请求/响应体不做协议转换，返回合法 Responses 协议响应，usage 被计量
- **Given** 映射的上游不支持 Responses（如 Anthropic 原生上游） **When** 请求 `/v1/responses` **Then** 返回明确错误与可诊断信息，说明该映射不支持此入口（不静默失败）
- **Given** Codex 以网关为 base URL **When** 执行一次带工具调用的会话 **Then** 会话正常完成
- **边界**：此入口不做跨协议转换；US-05 System Prompt 策略、US-08 缓存治理、US-09 内容优化管道均不作用于此入口；主备 fallback 仅在同样支持 Responses 的备选上游间生效

## US-12 对话数据保存与外部消费

As a **管理员**, I want to **对指定密钥开启对话内容保存（保留期固定可配置），并让外部 app 通过实时流与历史查询接口消费这些数据**, so that **团队可以在自己环境内查看、审计与二次加工 AI 对话数据**。

- **Given** 某密钥开启了对话保存 **When** 请求完成（流式请求按聚合后的完整响应计） **Then** 完整请求体与响应体压缩存储，并带密钥/模型/时间/状态索引
- **Given** 密钥未开启（默认） **Then** 不保存任何对话正文，仅保留 US-07 的调用日志元数据
- **Given** 外部 app 以专用数据凭据订阅 SSE 事件流（可按密钥/模型过滤） **When** 新请求完成 **Then** app 实时收到该条记录事件
- **Given** 外部 app 调用 REST 历史查询接口 **Then** 可按时间范围/密钥/模型分页拉取已保存对话
- **Given** 记录超过保留期 **When** 清理任务运行 **Then** 过期正文被自动删除；控制台可见当前存储用量与保留期设置
- **Given** 管理员在控制台打开某条调用日志 **When** 该记录已保存正文 **Then** 可展开查看对话内容（只读）
- **Given** 外部 app 的 SSE 连接中断 **When** 携带 `Last-Event-ID` 重连 **Then** 从断点继续接收错过的事件（事件携带稳定 ID）
- **Given** 正文持久化失败 **When** 客户端请求已完成 **Then** 不中断、不回滚已完成的客户端响应流；持久化失败计数在控制台可见

## US-13 Anthropic 入口兼容规范化

As a **开发者**, I want to **让 Claude Code 等 Anthropic 生态客户端产生的非标准请求（如长会话内联的 `messages[].role=system` 条目）在网关入口被确定性规范化**, so that **只改 base URL 接入的工具在严格校验 schema 的上游（如本地 vLLM）上也能稳定完成长会话**。

- **Given** 密钥处于默认的「Claude Code 兼容模式」 **When** 请求 `/v1/messages` 且 `messages[]` 含内联 `role=system` 条目 **Then** 网关把每条内联 system 内容确定性合并进最近的前一条 user 消息（无前置 user 消息时生成一条确定性合成 user 消息）；转发上游的请求只含 `user`/`assistant` 角色，内联内容恰好保留一次，内容块顺序、`tool_use`/`tool_result` ID、`cache_control`、thinking 块均不变
- **Given** 密钥被切换到「严格模式」 **When** 请求含内联 `role=system` **Then** 按 Anthropic 错误格式拒绝，不转发上游
- **Given** 请求携带合法的顶层 `system` 字段 **Then** 该字段不受规范化影响；规范化在入口层执行，先于协议转换与 US-05 策略，二者不混淆
- **Given** 同一密钥调用 `/v1/messages/count_tokens` **Then** 采用与生成完全相同的规范化规则（客户端自动压缩看到一致的有效 prompt）
- **Given** 规范化发生 **When** 查看调用日志 **Then** 可见脱敏的变换事件（规则版本、受影响消息索引、动作）；未开启 US-12 保存的密钥不记录正文
- **Given** 多轮会话客户端每轮重发历史 **Then** 变换确定性、版本化，变换后前缀逐字节稳定，不破坏 US-08 缓存命中（互锁语义同 C5）

---

## Constraints（约束）

- **C1 协议兼容范围**：Chat Completions 与 Messages 的主流能力——非流式/SSE 流式、工具调用、system、多轮消息、文本与图像输入、usage 返回、温度类通用参数。供应商私有扩展参数透传给同协议上游、跨协议时丢弃并记录。OpenAI Responses 入口（US-11）为受管透传，不在本条协议转换能力范围内。
- **C2 单管理员账号**：控制台仅一个管理员登录（用户名/密码），无多用户/角色。
- **C3 自部署单租户**：Docker Compose 交付；上游凭据、日志、用量数据全部留在客户环境。
- **C4 内容修改边界**：网关对请求的任何主动修改仅限 (a) system prompt 策略（US-05）、(b) 显式开启的内容优化管道（US-09，且仅工具输出块）与 (c) Anthropic 入口兼容规范化（US-13，默认兼容模式、按密钥可切严格）。三者均确定性、版本化、可审计（变更记录于调用日志）。
- **C5 优化与缓存互锁**：US-09 变换必须版本化；规则版本参与缓存有效性判断，版本升级即宣告缓存失效一次。
- **C6 单实例部署**：MVP 不要求多实例横向扩展与高可用集群。
- **C7 对话保存边界**：对话正文保存默认关闭、按密钥开启；保留期可配置（默认 7 天），到期自动清理；正文压缩存储，存储用量在控制台可见；外部数据接口（SSE/REST）使用独立的数据访问凭据鉴权，与虚拟密钥、管理员会话分离。
- **C8 provider 注册安全边界**：上游 base URL 的注册/修改必须做 SSRF 出站校验——默认拒绝回环/私网/链路本地/未指定 IP 并要求 HTTPS；注册本地 provider（`127.0.0.1`/内网，如本机 vLLM）须显式开启「允许私网/回环」开关，且该操作仅限管理员；实际发请求时对解析后 IP 再做一次校验以防 DNS rebinding（远程若允许 HTTP 仅限显式开启且用于本地/内网可信端点）。
- **C9 管理面与凭据安全基线**：管理 API 统一经过默认拒绝的鉴权中间件；管理员会话、虚拟调用密钥、US-12 数据访问凭据为三个相互独立的鉴权域。首次启动强制进入初始化向导设置管理员密码，不存在内置默认凭据；密码使用 Argon2id 或同级自适应哈希。虚拟密钥仅保存带服务端 pepper 的哈希与可识别前缀，创建后不可再次读取明文。上游凭据以主密钥加密存储，日志、错误信息与导出均不回显。容器默认非 root、最小权限运行；禁止 host PID、host network、Docker socket 挂载。日志与指标不记录对话正文、Authorization 头、上游凭据或完整虚拟密钥。

## Non-Goals（非目标）

以下能力属于 MVP 明确排除的范围：

- 多租户 SaaS、注册/计费/结算/发票
- 精确响应缓存、语义缓存、LLM 二次摘要
- 成本/延迟加权的策略引擎选路（仅主备 fallback）
- 多用户 RBAC、审计合规包、信创/等保适配
- 图像之外的多模态（音频/视频/文件输入）
- Embeddings、图像生成等非对话类 API 代理
- Agent 端优化工具（rtk 路线，建议客户直接采用开源 rtk，网关仅负责度量）
- OpenAI Responses 的跨协议转换与服务端会话存储（store/previous_response_id 语义），仅受管透传（US-11）
- 支持 Claude Code 内联 system 之外的任意非法消息角色（US-13 仅实现文档化的兼容规则）

---

## 批准记录

### r1

- 批准者：用户（项目所有人）
- 批准时间：2026 年 8 月 9 日
- 批准证据：本会话需求梳理问答，AskUserQuestion「用户故事基线 r1 是否批准？」→「批准 r1」
- 修订说明：批准后为通过 privacy-filter 预提交检查，调整了日期书写格式、来源 URL 写法与 Non-Goals 标题措辞，语义不变（详见 git 历史）

### r2

- 批准者：用户（项目所有人）
- 批准时间：2026 年 8 月 10 日
- 批准证据：三项范围决策的 AskUserQuestion 答复（Responses 仅受管透传到 OpenAI 兼容上游 / 对话保存按密钥开关且默认关闭 / SSE 实时流 + REST 历史查询）＋ 浏览器评审屏幕 baseline-r2-additions ＋ 终端答复「批准 r2」
- 修订说明：新增 US-11（OpenAI Responses 入口，受管透传）、US-12（对话数据保存与外部消费）与约束 C7；C1 增补 Responses 透传范围说明；Non-Goals 增补 Responses 跨协议转换与服务端会话存储。US-01~US-10、C2~C6 及其余文本逐字保留自 r1。

### r3

- 批准者：用户（项目所有人）
- 批准时间：2026 年 8 月 13 日
- 批准证据：VPS provider 注册研究与升版请求（本会话，基于 <PRIVATE_DATE> 与 <PRIVATE_ADDRESS>）
- 修订说明：US-03 增补「provider 注册」验收（本地 vLLM/内网显式放行、远程默认拒绝回环/私网且仅 HTTPS、保存时可诊断校验错误）；新增约束 C8（provider 注册安全边界，SSRF 出站校验：默认拒绝回环/私网并要求 HTTPS，本地显式放行、仅管理员，请求时 resolved-IP 防 DNS rebinding）。US-01、US-02、US-04~US-12、C1~C7 及其余文本逐字保留自 r2。另：批准证据中的 `<PRIVATE_DATE>` 与 `<PRIVATE_ADDRESS>` 为 privacy-filter 对原始引用的掩码占位符，语义见本段与 `02-working/vps_provider_registration.md`。

### r4

- 批准者：用户（项目所有人）
- 批准时间：2026 年 8 月 13 日
- 批准证据：三项范围决策的 AskUserQuestion 答复（US-13 入基线并修订 C4 / 默认兼容模式、按密钥可切严格 / 安全清单凝练为新约束 C9）＋ AskUserQuestion「用户故事基线 r4 增量是否批准？」→「批准 r4」
- 修订说明：新增 US-13（Anthropic 入口兼容规范化，来源 GitHub issue #2）；C4 增补第 (c) 类允许修改；新增约束 C9（管理面与凭据安全基线，来源 lexdata 现网分析）；US-10 增补首启动强制初始化验收；US-12 增补 SSE `Last-Event-ID` 断线续传与正文持久化失败语义两条验收；Non-Goals 增补「仅支持文档化的 US-13 兼容规则」。US-01~US-09、US-11、C1~C3、C5~C8 及其余文本逐字保留自 r3。
