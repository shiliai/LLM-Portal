# LLM-portal

> 自部署统一 AI 网关，面向中小企业的 LLM API 入口标准化与企业治理层。
>
> **项目状态**：规划 / 设计阶段。尚无可构建的代码，**无 build / lint / test 命令**——代码落地到 `execution/` 后在此补充。

## 它是什么

LLM-portal 在企业内部署一个统一 API 网关，开发者用标准 OpenAI / Anthropic 协议调用，由网关完成：

1. **模型映射** —— 对外暴露统一模型名，映射到不同上游供应商/模型。
2. **协议转换** —— OpenAI Chat Completions / Responses 与 Anthropic Messages 双向互转（请求、响应、SSE 流式、工具调用）。
3. **System Prompt 策略** —— 在网关层对 system prompt 注入、改写、追加，按密钥或路由配置。
4. **缓存治理** —— 响应缓存与 prompt cache 的策略、命中率与节省成本可见。

参考产品：[token.love](https://token.love)（产品定位见 `planning/02-working/token_love_product_spec.md`）。

## 需求基线

权威需求基线：**`planning/03-core/user_story_baseline_r2.md`**（已批准、已冻结）。

> 设计决策与需求澄清以 `planning/03-core/` 中已确认的文档为准；未落入 03-core 的内容一律视为未确认。

主要用户故事摘要：

| ID | 标题 | 一句话 |
|----|------|--------|
| US-01 | OpenAI 入口统一调用 | 用 OpenAI 协议/SDK 通过虚拟密钥调用网关 |
| US-02 | Anthropic 入口统一调用 | Claude Code / Anthropic SDK 零改造接入 |
| US-03 | 模型映射管理 | 对外模型名 → 上游供应商/模型 的 Web 维护 |
| US-04 | 主备容灾 | 主上游故障时自动切换到有序备选 |
| US-05 | System Prompt 策略 | 按密钥 / 路由配置注入 / 追加 / 替换 |
| US-06 | 虚拟密钥与限额 | 密钥、可用模型范围、额度、速率隔离 |
| US-07 | 用量与成本可见 | 仪表盘按密钥 / 模型 / 时间维度查看 token 与成本 |
| US-08 | Prompt Cache 治理 | prompt cache 命中率与节省成本可见 |
| US-09 | 内容优化管道（可选） | 长上下文的字节稳定优化 |
| US-10 | 故障切换语义（可选） | failover 与缓存计量的联合设计 |
| US-11 | Responses 受管透传 | OpenAI Responses 协议作为受管入口 |
| US-12 | 对话数据保存 | 按密钥可配置是否落库 |

完整 Given/When/Then 见基线文档。

## 仓库目录

| 目录 | 用途 | 规则 |
|------|------|------|
| `CLAUDE.md` | 项目级 Claude Code 工作约定 | 与本 README 一并阅读 |
| `planning/01-raw/` | 原始资料（外部代码库、原始文档） | **已 gitignore**。仅在需要查找细节时查看 |
| `planning/02-working/` | 已提炼过的资料 | 作为**项目设计阶段**的输入 |
| `planning/03-core/` | 已确认的权威资料 | **开发阶段**直接使用，以此为准 |
| `execution/` | 开发执行工作区 | 代码与实现产物（当前为空） |

工作流：`01-raw`（提炼）→ `02-working`（设计确认）→ `03-core`（开发依据）。

### 已有的关键工作文档

- **产品定位**：`planning/02-working/token_love_product_spec.md`
- **用户故事基线**：`planning/03-core/user_story_baseline_r2.md`
- **OSS 协议转换模块调研**：`planning/02-working/protocol_conversion_oss_survey.md`
- **对标项目分析**（`sub2api`、`new-api`、`NeMo Switchyard`）：见 `planning/02-working/` 下 `*_analysis.md`

## 角色与场景

- **管理员**：中小企业 IT / 运维负责人。在 Web 控制台维护模型映射、密钥、限额、缓存策略、system prompt 策略；查看用量、故障切换事件、prompt cache 命中率。
- **开发者**：企业内部使用网关虚拟密钥的应用或工程师。**不改业务代码**，按既有 OpenAI / Anthropic SDK 把 base URL 指向网关即可。

## 许可证

待定（代码落地时与开源协议评审一并决定）。
