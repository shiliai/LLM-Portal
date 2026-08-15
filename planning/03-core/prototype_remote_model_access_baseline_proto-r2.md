# 用户故事基线 proto-r2：远程访问私有推理模型（快速原型）

> 状态：**BASELINE_APPROVED（已冻结）**
> baseline_revision：`proto-r2`（取代 proto-r1，见文末修订说明）
> 权威来源：2026-08-14 会话用户原始需求 + 6 次澄清问答 + 两轮范围扩展指示（多站点聚合/一键隧道/公钥管理；管理页面/用户用量自查）
> 批准者：用户（Chris Wang）
> 批准证据：2026-08-14 会话 AskUserQuestion「r2v2 基线批准」，用户选择「批准，冻结 proto-r2」；proto-r1 批准证据见 r1 文件
> 范围声明：独立于 LLM-portal 旧 PRD 基线（r2~r4），互不引用、互不约束。快速原型专用。

## 背景事实（用户环境）

- 局域网站点内私有推理服务（无公网 IP，不可被公网直接访问）。首个站点 192.0.2.10：
  - `deepseek-v4-flash-0731`：文本模型，1M 上下文，OpenAI 兼容接口 `http://192.0.2.10:8890/v1`
  - `qwen3.6-35b-a3`：多模态模型，OpenAI 兼容接口 `http://192.0.2.10:8004/v1`
- 未来存在**多个局域网站点**，各站点均有可常开、可主动外连 VPS 的机器。
- 用户拥有一台可公网访问的 VPS，**有域名**。
- 各局域网与 VPS 之间**无既有隧道/组网**，方案需包含内网穿透设计。

## 用户故事（冻结，逐字引用，不得改写）

### US-P1 远程直连文本模型

As a 私有模型所有者 / I want to 在任意地点的电脑上通过统一公网入口调用局域网内的 deepseek-v4-flash-0731 / So that 随时随地用上自己的 1M 上下文模型。

- Given 客户端配置了 `https://<你的域名>` + API Key，When 用 OpenAI 兼容客户端直选 `deepseek-v4-flash-0731` 发起对话（含流式），Then 请求经 VPS→隧道到达局域网 8890 服务并正常流式返回。
- 边界：无/错 API Key → 401 拒绝。
- 失败路径：隧道断开 → 返回可判读的 5xx 错误，不无限挂起。

### US-P2 远程直连多模态模型

As a 用户 / I want to 同一入口直选 `qwen3.6-35b-a3` 发送带图请求 / So that 在外也能用私有模型识图。

- Given 同 US-P1 的入口配置，When 发送含图片的 chat 请求且模型选 qwen，Then 返回 qwen 识图结果。

### US-P3 Claude Code 使用私有模型

As a Claude Code 用户 / I want to 把 Claude Code 指向自己的网关、以 deepseek 为主模型 / So that agent 工作流跑在自有模型上。

- Given Claude Code 配置网关 base_url + Key，When 发起 `/v1/messages` 会话（流式、工具调用），Then 网关完成 Anthropic↔OpenAI 协议转换，会话与工具调用可用。

### US-P4 Agent 经 MCP 工具识图

As a Claude Code 用户 / I want to 本地 MCP 工具把图片交给 qwen3.6 识别 / So that 主对话留在 deepseek 的同时 agent 能看图。

- Given 本机注册了识图 MCP server（指向网关），When agent 对本地图片路径/URL 调用工具并附问题，Then 返回 qwen 识别结果供主模型继续。

### US-P5 站点隧道常驻自愈（r2 修订）

As a 所有者 / I want to 每个局域网站点的常开机器主动外连 VPS 维持加密反向通道、开机自启 / So that 无公网 IP 的各站点模型服务持续可达。

- Given 站点隧道客户端已部署，When 站点机器重启或网络闪断，Then 隧道自动重连、该站点模型恢复可用。

### US-P6 多站点聚合与调度（r2 新增）

As a 所有者 / I want to 网关聚合多个局域网站点注册的模型，同名模型按负载分流、站点故障自动熔断切流 / So that 多处算力经统一入口使用、单点故障不影响服务。

- Given 同一对外模型名在 ≥2 个站点有部署，When 并发请求到达，Then 按在途请求数/延迟分流到各站点。
- Given 某站点宕机或隧道中断，When 请求到达，Then 自动路由到健康站点，客户端无感知；全部站点不可用时返回可判读 5xx。
- 可为每站点设并发/速率上限。

### US-P7 一键站点接入（r2 新增）

As a 所有者 / I want to 在网关侧一条命令生成新站点的一次性接入命令，到新局域网机器上执行一行即完成隧道部署 / So that 新站点接入零手工配置。

- Given 管理员在 VPS 执行 `site-add <站点名>` 得到限时一次性安装命令，When 在站点机器执行，Then 自动完成：生成密钥对→公钥注册→隧道建立→systemd 自启→模型连通性自检并回显结果。

### US-P8 站点公钥管理（r2 新增）

As a 所有者 / I want to 以每站点一把公钥来标识、列出、吊销站点 / So that 站点身份可控可撤销。

- Given 站点以公钥注册，When 吊销该公钥，Then 其隧道立即断开且无法重连，其模型自动从路由池摘除。

### US-P9 管理页面（r2 新增）

As a 管理员 / I want to 一个简单管理页面来操作和查看网关（站点/模型渠道/用户 Key/用量与请求日志）/ So that 日常运维不用登服务器改配置。

- Given 管理员登录管理页，When 新增或禁用一个用户 Key、启停一个上游模型渠道，Then 立即生效。
- Given 管理页用量视图，When 按 Key 或模型筛选，Then 可见请求数、token 用量与近期错误。

### US-P10 用户用量自查（r2 新增）

As a 普通用户 / I want to 凭自己的 API Key 查看自己的用量 / So that 掌握自己的消耗。

- Given 用户持有效 Key，When 访问用量查询入口（页面或 API），Then 只能看到自己这把 Key 的用量，不能看到他人。
- 用量口径：请求数 + token 数（取上游 usage 字段），不含计费。

## 约束（冻结）

- C1：公网入口仅 HTTPS（域名 + 自动证书）+ 静态 API Key 鉴权；无 Key/错 Key 拒绝。
- C2（r2 精化）：模型服务端口不暴露公网；隧道采用 WireGuard（UDP 端口公开但仅持有已注册密钥对的站点可建联）；上游模型仅经 WG 私有地址被 VPS 访问。
- C3（r2 修订）：单管理员 + 多用户 API Key；Key 由管理员在管理页创建、分发、禁用，本期不开放用户自助注册。
- C4（r2 新增）：调度深度 = 请求级负载均衡 + 故障转移。

## Non-goals（冻结）

- 网关层「含图自动改道 qwen」内容路由（由直选模型 + MCP 工具替代）。
- GPU 指标感知调度（二期候选）。
- 用户自助注册 Key、用户申请模型开通（**明确列为下一阶段**）。
- 计费/充值、响应缓存、prompt cache 治理。
- 旧 PRD r4 的企业功能（模型映射策略引擎、system prompt 策略等）。

## 澄清与修订记录

| 轮次 | 问题/指示 | 用户决定 |
|---|---|---|
| 1 | 客户端协议 | OpenAI 兼容客户端 + Claude Code（Anthropic 协议）都要 |
| 2 | 组网现状 | 无既有隧道，方案一并设计 |
| 3 | 路由语义 | 模型直选；qwen3.6 包装为 MCP 工具；网关不做含图自动路由 |
| 4 | 安全基线 | 有域名：HTTPS + 静态 API Key |
| 5 | 范围扩展（用户主动） | 多局域网聚合与算力调度；一键反向隧道部署；公钥管理 |
| 6 | 调度深度 | 负载均衡 + 故障转移（GPU 感知调度二期） |
| 7 | 范围扩展（用户主动） | 简单管理页面；用户凭 Key 自查用量；自助注册/申请模型留下一阶段 |

**r1→r2 修订说明**：US-P5 由「frp 单站点隧道」语义改为「多站点 WireGuard 通道」；新增 US-P6~P10；C2 由 frp 精化为 WireGuard；C3 由单 Key 改为管理员管理的多 Key；non-goals 移除「管理 UI」、新增「GPU 感知调度」「自助注册（下一阶段）」。US-P1~P4 逐字保留自 proto-r1。
