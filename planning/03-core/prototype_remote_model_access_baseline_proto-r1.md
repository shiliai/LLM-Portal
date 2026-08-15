# 用户故事基线 proto-r1：远程访问私有推理模型（快速原型）

> 状态：**BASELINE_APPROVED（已冻结）**
> baseline_revision：`proto-r1`
> 权威来源：2026-08-14 会话中用户原始需求陈述 + 4 次澄清问答（客户端协议 / 组网现状 / 路由语义 / 安全基线）
> 批准者：用户（Chris Wang）
> 批准证据：2026-08-14 会话 AskUserQuestion「基线批准」问题，用户选择「批准，冻结 proto-r1」
> 范围声明：本基线独立于 LLM-portal 旧 PRD 基线（r2~r4），互不引用、互不约束。快速原型专用。

## 背景事实（用户环境）

- 局域网内私有推理服务（无公网 IP，不可被公网直接访问）：
  - `deepseek-v4-flash-0731`：文本模型，1M 上下文，OpenAI 兼容接口 `http://192.0.2.10:8890/v1`
  - `qwen3.6-35b-a3`：多模态模型，OpenAI 兼容接口 `http://192.0.2.10:8004/v1`
- 用户拥有一台可公网访问的 VPS，且**有域名**。
- 局域网内有可常开、可主动外连 VPS 的机器（如 192.0.2.10 本身）。
- 局域网与 VPS 之间**无既有隧道/组网**，方案需包含内网穿透设计。

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

### US-P5 隧道常驻自愈

As a 所有者 / I want to 局域网内常开机器主动外连 VPS 维持反向通道、开机自启 / So that 无公网 IP 的模型服务持续可达。

- Given 隧道客户端已部署，When 局域网机器重启或网络闪断，Then 隧道自动重连、入口恢复。

## 约束（冻结）

- C1：公网入口仅 HTTPS（域名 + 自动证书）+ 静态 API Key 鉴权；无 Key/错 Key 拒绝。
- C2：模型服务与隧道端口不直接暴露公网；上游服务仅对 VPS 回环（或等价私有链路）可达。
- C3：单用户、单 Key 的快速原型。

## Non-goals（冻结）

- 网关层「含图自动改道 qwen」内容路由（用户明确选择不做，由直选模型 + MCP 工具替代）。
- 多用户/多 Key 管理、用量统计与计费、响应缓存、管理 UI。
- 旧 PRD r4 的企业功能（模型映射策略引擎、system prompt 策略、缓存治理等）。

## 澄清记录（决策来源）

| 问题 | 用户决定 |
|---|---|
| 客户端协议 | OpenAI 兼容客户端 + Claude Code（Anthropic 协议）都要 |
| 组网现状 | 无既有隧道，方案一并设计；局域网有可常开外连机器 |
| 路由语义 | 模型对外直选；qwen3.6 另包装为 MCP 工具给 agent；网关不做含图自动路由 |
| 安全基线 | 有域名：HTTPS + 静态 API Key |
