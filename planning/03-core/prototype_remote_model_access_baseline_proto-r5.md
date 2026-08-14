# 用户故事基线 proto-r5：远程访问私有推理模型（快速原型）

> 状态：**BASELINE_APPROVED（已冻结）**
> baseline_revision：`proto-r5`（取代 proto-r4，见文末修订说明）
> 权威来源：2026-08-14 会话用户原始需求 + 澄清问答 + 五轮范围扩展指示（多站点/一键隧道/公钥管理；管理页/用量自查；模型映射/网关托管视觉 MCP；凭据模型/外部 MCP 注册；Key 绑定 provider 分组）
> 批准者：用户（Chris Wang）
> 批准证据：2026-08-14 会话用户指示「用户 api key不能只和模型绑定，也要和 provider 进行绑定，类似 sub2api 里面的 group 这个分层」+ AskUserQuestion「Group 口径」用户选择「A：Group=provider 分组，与模型白名单正交」；r1~r4 批准证据见对应文件
> 范围声明：独立于 LLM-portal 旧 PRD 基线（r2~r4），互不引用、互不约束。快速原型专用。

## 背景事实（用户环境）

- 局域网站点内私有推理服务（无公网 IP，不可被公网直接访问）。首个站点 192.168.88.181：
  - `deepseek-v4-flash-0731`：文本模型，1M 上下文，OpenAI 兼容接口 `http://192.168.88.181:8890/v1`（上游无鉴权）
  - `qwen3.6-35b-a3`：多模态模型，OpenAI 兼容接口 `http://192.168.88.181:8004/v1`（上游无鉴权）
- 未来存在**多个局域网站点**，各站点均有可常开、可主动外连 VPS 的机器。
- 用户拥有一台可公网访问的 VPS，**有域名**。
- 各局域网与 VPS 之间**无既有隧道/组网**，方案需包含内网穿透设计。
- 机制共识（2026-08-14 澄清）：agent 的 toolcall 参数由主模型生成，只含路径/URL 短字符串，不含图片字节；base64 转换发生在 MCP 服务进程内。远程托管 MCP 读不到客户端本地文件，本地图片需先上传换 URL。
- 参考口径（2026-08-14 澄清）：「provider 分组」的分层语义参照 sub2api 的 Group——上游凭据/账号归入 Group，API Key 归属 Group，调度只在组内进行。本原型中 provider = 站点（上游部署），未来含外部云上游。

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

### US-P4 网关托管视觉 MCP（r4 增补凭据模型条款）

As a Claude Code 用户 / I want to 客户端仅注册网关的 MCP 地址（与 API 同域名、同一 Key），由网关把已注册的多模态模型封装为视觉工具 / So that 零本地安装、主对话留在 deepseek 的同时 agent 能看图。

- Given 客户端以 `https://<域名>/mcp` + 同一 API Key 注册网关 MCP（Streamable HTTP，注册形态同智谱 vision-mcp-server），When agent 对图片 URL 调用 `analyze_image(url, 问题)`，Then 网关用注册的多模态模型（qwen3.6-35b-a3）识别并返回结果。
- Given 图片在本地，When agent 先经网关上传接口（同一 Key 鉴权）换取限时临时 URL，再调用工具，Then 识别正常；临时文件到期自动清理。
- Given 用户以自己的网关 Key 调用视觉工具，When 网关向实际提供方发起调用，Then：私有 qwen3.6（无鉴权上游）直连、不带任何 Key；需凭据的外部服务使用网关注册时保存的凭据；用户的 Key 只用于网关鉴权与记账，永不发往任何上游（r4 增补）。
- 用量归属：视觉工具的模型消耗计入调用者自己的 Key（与 US-P10 口径一致）。
- 边界：无效 Key 注册/调用 → 拒绝；参数无有效图片 → 明确报错。
- 客户端也可自由注册其他公网 MCP 服务（如智谱的），与本网关互不影响（不属网关职责）。

### US-P5 站点隧道常驻自愈

As a 所有者 / I want to 每个局域网站点的常开机器主动外连 VPS 维持加密反向通道、开机自启 / So that 无公网 IP 的各站点模型服务持续可达。

- Given 站点隧道客户端已部署，When 站点机器重启或网络闪断，Then 隧道自动重连、该站点模型恢复可用。

### US-P6 多站点聚合与调度

As a 所有者 / I want to 网关聚合多个局域网站点注册的模型，同名模型按负载分流、站点故障自动熔断切流 / So that 多处算力经统一入口使用、单点故障不影响服务。

- Given 同一对外模型名在 ≥2 个站点有部署，When 并发请求到达，Then 按在途请求数/延迟分流到各站点。
- Given 某站点宕机或隧道中断，When 请求到达，Then 自动路由到健康站点，客户端无感知；全部站点不可用时返回可判读 5xx。
- 可为每站点设并发/速率上限。

### US-P7 一键站点接入

As a 所有者 / I want to 在网关侧一条命令生成新站点的一次性接入命令，到新局域网机器上执行一行即完成隧道部署 / So that 新站点接入零手工配置。

- Given 管理员在 VPS 执行 `site-add <站点名>` 得到限时一次性安装命令，When 在站点机器执行，Then 自动完成：生成密钥对→公钥注册→隧道建立→systemd 自启→模型连通性自检并回显结果。

### US-P8 站点公钥管理

As a 所有者 / I want to 以每站点一把公钥来标识、列出、吊销站点 / So that 站点身份可控可撤销。

- Given 站点以公钥注册，When 吊销该公钥，Then 其隧道立即断开且无法重连，其模型自动从路由池摘除。

### US-P9 管理页面

As a 管理员 / I want to 一个简单管理页面来操作和查看网关（站点/模型渠道/用户 Key/用量与请求日志）/ So that 日常运维不用登服务器改配置。

- Given 管理员登录管理页，When 新增或禁用一个用户 Key、启停一个上游模型渠道，Then 立即生效。
- Given 管理页用量视图，When 按 Key 或模型筛选，Then 可见请求数、token 用量与近期错误。

### US-P10 用户用量自查

As a 普通用户 / I want to 凭自己的 API Key 查看自己的用量 / So that 掌握自己的消耗。

- Given 用户持有效 Key，When 访问用量查询入口（页面或 API），Then 只能看到自己这把 Key 的用量，不能看到他人。
- 用量口径：请求数 + token 数（取上游 usage 字段），不含计费。

### US-P11 模型名映射

As a 管理员 / I want to 把对外模型名（如 `claude-opus-5`）映射到实际上游模型（如 `deepseek-v4-flash-0731`）/ So that 客户端只需配好 url/key 即可用默认模型名直接工作，无需逐个改模型配置。

- Given 管理员配置了映射 `claude-opus-5 → deepseek-v4-flash-0731`，When Claude Code 仅设置 base_url + Key（保持其默认模型名）发起请求，Then 请求实际由 deepseek-v4-flash-0731 处理并正常返回（流式/工具调用同 US-P3）。
- Given 同一上游模型，When 配置多个别名（如 `claude-opus-5`、`gpt-4o` 都指向 deepseek），Then 各别名同时可用；`/v1/models` 列表可见全部对外名。
- 边界：请求未映射且不存在的模型名 → 返回明确的「模型不存在」错误（400/404 语义），不误路由。
- 别名与直选名并存：US-P1/P2 的真实模型名 `deepseek-v4-flash-0731`、`qwen3.6-35b-a3` 保持直选可用。

### US-P12 外部 MCP 服务注册（r4 新增）

As a 管理员 / I want to 在网关注册外部远程 MCP 服务（如智谱 vision-mcp-server）及其访问凭据 / So that 用户用同一个网关地址 + 自己的一把 Key 就能使用外部 MCP 能力，无需人手一份外部服务的 Key。

- Given 管理员在网关注册了外部 MCP（URL + 该服务的 API Key），When 用户经网关 MCP 端点调用其工具，Then 网关以注册凭据向外部服务转发并返回结果；工具清单聚合透出（`tools/list` 可见内建视觉工具 + 各外部 MCP 工具，命名加前缀防冲突）。
- Given 外部 MCP 凭据失效或服务不可达，When 调用，Then 返回可判读错误。
- 用量口径：MCP 工具调用按次计入调用者 Key；token 用量仅对经网关模型通道的调用可得（外部 MCP 不保证回报 token）。

### US-P13 Key 绑定 provider 分组（r5 新增）

As a 管理员 / I want to 把上游 provider（本期即站点，未来含外部云上游）归入命名分组（Group，provider 与分组多对多），并将每把用户 Key 绑定到一个分组 / So that 不同 Key 的请求只在授权的 provider 范围内路由与分流，调整分组成员即对组内全部 Key 批量生效（分层语义同 sub2api 的 group）。

- Given 管理员建立分组 `home`（仅含 hq-office 站点）且某 Key 绑定 `home`，When 该 Key 请求 `qwen3.6-35b-a3`，Then 请求只在 home 组内 provider 的部署上分流与故障转移，绝不落到组外站点。
- Given 该 Key 请求的模型在其分组内无任何部署，When 请求到达，Then 返回明确的可判读错误，不误路由到组外 provider。
- Given Key 未绑定任何分组，When 请求，Then 按 `default` 分组（全部 provider）路由。
- 正交性：分组与既有的按 Key 模型白名单独立设定、互不耦合——Key 的可用模型 = 其分组内 provider 部署的模型 ∩ 模型白名单（未设白名单即不裁剪）。
- 同权口径：分组对 API 与网关托管 MCP 同时生效（视觉工具以调用者 Key 回调模型通道，天然继承其分组）。

## 约束（冻结）

- C1：公网入口仅 HTTPS（域名 + 自动证书）+ 静态 API Key 鉴权；无 Key/错 Key 拒绝。
- C2：模型服务端口不暴露公网；隧道采用 WireGuard（UDP 端口公开但仅持有已注册密钥对的站点可建联）；上游模型仅经 WG 私有地址被 VPS 访问。
- C3：单管理员 + 多用户 API Key；Key 由管理员在管理页创建、分发、禁用，本期不开放用户自助注册。
- C4：调度深度 = 请求级负载均衡 + 故障转移。
- C5（r4 新增）：上游凭据（外部 MCP Key 等）仅保存在网关侧配置；用户虚拟 Key 永不出网关。

## Non-goals（冻结）

- 网关层「含图自动改道 qwen」内容路由（由直选模型 + 网关托管视觉 MCP 替代）。
- GPU 指标感知调度（二期候选）。
- 用户自助注册 Key、用户申请模型开通（**明确列为下一阶段**）。
- 本地 stdio 视觉桥（形态 B 被否，纯网关托管）。
- 计费/充值、响应缓存、prompt cache 治理。
- 旧 PRD r4 的企业功能（按 Key 生效的映射策略、system prompt 策略等；US-P11 仅为全局静态别名映射）。
- 分组回退（主组无可用 provider 时委托辅助组，sub2api 有此机制）与分组级预算/配额（r5 明确排除，二期候选）。

## 澄清与修订记录

| 轮次 | 问题/指示 | 用户决定 |
|---|---|---|
| 1 | 客户端协议 | OpenAI 兼容客户端 + Claude Code（Anthropic 协议）都要 |
| 2 | 组网现状 | 无既有隧道，方案一并设计 |
| 3 | 路由语义 | 模型直选；网关不做含图自动路由 |
| 4 | 安全基线 | 有域名：HTTPS + 静态 API Key |
| 5 | 范围扩展（用户主动） | 多局域网聚合与算力调度；一键反向隧道部署；公钥管理 |
| 6 | 调度深度 | 负载均衡 + 故障转移（GPU 感知调度二期） |
| 7 | 范围扩展（用户主动） | 简单管理页面；用户凭 Key 自查用量；自助注册/申请模型留下一阶段 |
| 8 | 范围扩展（用户主动） | 模型名映射（claude-opus-5 → deepseek 等别名） |
| 9 | 视觉 MCP 位置（用户主动）+ toolcall 机制澄清 | 网关托管（Streamable HTTP，同智谱形态）；确认 toolcall 只传路径/URL 后选定形态 A |
| 10 | 凭据模型（用户主动） | 用户侧一把网关 Key 通吃 API+MCP；私有 qwen 上游无鉴权直连；外部 MCP（如智谱）的 Key 在网关注册时录入、由网关代持代转 |
| 11 | 范围扩展（用户主动）：Key 需与 provider 绑定，类似 sub2api 的 group 分层 | 选 A：Group=provider 分组（站点集合，多对多），Key 绑一个组（默认 default 组=全部 provider），与模型白名单正交；分组回退与分组预算排除 |

**r4→r5 修订说明**：新增 US-P13（Key 绑定 provider 分组：命名分组、default 兜底、组内路由、组内无部署报错、与模型白名单正交、API/MCP 同权）；背景事实增补 sub2api group 参考口径；non-goals 增补「分组回退与分组级预算/配额」。US-P1~P12、C1~C5 逐字保留自 proto-r4。
