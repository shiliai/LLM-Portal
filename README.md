# LLM-portal

面向**中小企业**的自部署统一 AI 网关（Unified AI Gateway）。

对外提供 OpenAI / Anthropic / OpenAI Responses 兼容入口，统一模型名映射、密钥与计量，让企业用一套虚拟密钥和不变的业务代码接入任意上游模型（OpenAI 兼容或 Anthropic 原生），并在此之上治理 System Prompt、prompt cache 与 token 成本。

参考产品：token.love（见 `planning/02-working/token_love_product_spec.md`）。

> **状态**：当前处于**规划/设计阶段**。尚无可构建的代码，暂无 build/lint/test 命令；开发执行产物将落地到 `execution/`。需求基线与设计决策以 `planning/03-core/` 中已确认的文档为准。

## 核心能力

1. **模型映射**：对外暴露统一模型名，映射到不同上游供应商/模型，切换底层供应商时调用方无感知。
2. **路由转换**：OpenAI Chat Completions ↔ Anthropic Messages 双向协议转换（请求/响应/流式/工具调用）。
3. **多入口**：OpenAI `/v1/chat/completions`、Anthropic `/v1/messages`、OpenAI Responses（受管透传，供 Codex 等工具零改造接入）。
4. **主备容灾**：主上游故障/限流时自动切换到有序备选上游。
5. **System Prompt 策略**：按密钥/模型路由注入、追加、替换 system prompt。
6. **虚拟密钥与限额**：按团队/应用隔离用量，绑定模型范围、额度与速率限制，控制成本。
7. **用量与成本可见**：按密钥/模型/时间维度的 token 用量、估算成本与调用日志仪表盘。
8. **Prompt Cache 治理**：cache 断点透传、按策略注入、命中率与节省成本可见。
9. **内容优化管道（可选）**：对指定密钥保守优化工具输出块，降低 token 成本且不破坏缓存前缀。
10. **对话数据保存与外部消费（可选，默认关闭）**：按密钥开启正文保存，SSE 实时流 + REST 查询供外部 app 消费。

## 文档导航

| 内容 | 位置 |
|------|------|
| 用户故事基线（权威依据）r3 | `planning/03-core/user_story_baseline_r3.md` |
| 用户故事基线 r2 | `planning/03-core/user_story_baseline_r2.md` |
| 需求/分析精炼资料 | `planning/02-working/` |
| 原始资料（外部代码库等，已 gitignore） | `planning/01-raw/` |
| 开发执行工作区 | `execution/` |
| Claude Code 协作约定 | `CLAUDE.md` |

### 目录约定

| 目录 | 用途 | 规则 |
|------|------|------|
| `planning/01-raw/` | 原始资料（外部代码库、原始文档等） | **已 gitignore**。仅按需回查细节 |
| `planning/02-working/` | 已提炼的资料 | 作为项目设计阶段的输入 |
| `planning/03-core/` | 已确认的权威资料 | 开发阶段直接使用，以此为准 |
| `execution/` | 开发执行产物 | 代码与实现 |

工作流：`01-raw`（提炼）→ `02-working`（设计确认）→ `03-core`（开发依据）。

## 文档惯例

- **当前基线版本**：`user_story_baseline_r3.md`（r3，已批准冻结）。
- 需求基线只能通过「升版」演进：新版本在旧版本文本基础上追加批准记录，非本次修订的既有文本须逐字保留。
- 未落入 `03-core/` 的任何内容一律视为未确认。
