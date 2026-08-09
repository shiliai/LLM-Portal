# LLM-portal

面向**中小企业**的统一 AI 网关（参考产品：token.love，见 `planning/02-working/token_love_product_spec.md`）。

## 产品目标（MVP 核心能力）

1. **模型映射**：对外暴露统一模型名，映射到不同上游供应商/模型。
2. **路由转换**：OpenAI API ↔ Anthropic API 双向协议转换（请求/响应/流式/工具调用）。
3. **System Prompt 修改**：网关层对 system prompt 的注入、改写、追加策略。
4. **缓存管理**：响应缓存 / prompt cache 的策略与管理。

后续可扩展（参考 token.love spec）：密钥与配额、用量计量、成本拆分、可观测性。

## 目录约定

| 目录 | 用途 | 规则 |
|------|------|------|
| `planning/01-raw/` | 原始资料（代码库、原始文档等） | **已 gitignore**。仅在需要查找细节时查看；第一轮提炼完成后不再读取 |
| `planning/02-working/` | 已提炼过的资料 | 作为**项目设计阶段**的输入 |
| `planning/03-core/` | 已确认的权威资料 | **开发阶段**直接使用，以此为准 |
| `execution/` | 开发执行工作区 | 代码与实现产物 |

工作流：`01-raw`（提炼）→ `02-working`（设计确认）→ `03-core`（开发依据）。

## 约定

- 资料未进入 `03-core` 之前视为未确认，开发不得直接依赖 `01-raw` / `02-working` 中的内容。
- 默认分支：`main`。
