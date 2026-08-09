# 协议转换开源模块调研：可直接复用什么、参考什么、避开什么

> 来源：zread 仓库检索 + npm/公开资料交叉核对，2026 年 8 月 9 日
> 范围：OpenAI Chat Completions ↔ Anthropic Messages 双向转换（请求/响应/SSE 流式/工具调用），及整体网关框架的可复用性
> 关联文档：sub2api_analysis.md、new_api_analysis.md（单独成文）

## 一句话结论

**转换层不必从零手写。** npm 包 @musistudio/llms（MIT）提供了现成的 Anthropic ↔ OpenAI 转换器与统一中间格式，可 vendor 进自研代码库改造；Portkey 网关（MIT）与 Bifrost（Apache-2.0）的转换实现可自由参考并用作对照测试基准。没有任何现成框架同时满足我们的四个差异化需求（双原生入口 + system prompt 策略 + 缓存治理 + 字节稳定优化管道），**整体框架仍需自研，但最难的转换正确性问题有开源肩膀可站**。

## 候选总览

| 项目 | 语言 | 许可证 | Anthropic 原生入站 | 对我们的可用形态 |
|------|------|--------|-------------------|----------------|
| npm 包 @musistudio/llms | TypeScript | MIT | 有（/v1/messages） | **直接复用/vendor 转换器代码** |
| GitHub 仓库 Portkey-AI/gateway | TypeScript (Hono) | MIT | 未确认（入站归一为 OpenAI 格式） | 参考 + 抽取局部实现 |
| GitHub 仓库 maximhq/bifrost | Go | Apache-2.0 | 有（/anthropic/v1/messages） | 参考实现 + 行为对照 |
| LiteLLM | Python | MIT | 有（文档宣称 /v1/messages 统一入口） | 语义参照（栈不同，不可直接复用） |
| new-api | Go | AGPL-3.0 | 有 | 仅黑盒行为对照，代码不可搬运 |
| sub2api | Go | LGPL-3.0 | 有 | 参考 + 作为真实上游做集成测试 |
| 官方 SDK：openai、@anthropic-ai/sdk | TypeScript | Apache-2.0 / MIT | —— | **复用类型定义作编译期协议契约** |

## 重点：@musistudio/llms（最强复用候选）

- 出身：claude-code-router（CCR）的转换内核独立发包；CCR 生态已在真实 Claude Code 流量上验证过工具调用、流式、思维链等转换路径。
- 架构：Transformer 类实现 `transformRequestIn/Out`、`transformResponseIn/Out` 与 `endPoint`（Anthropic 即 `/v1/messages`），以 `UnifiedChatRequest/UnifiedChatResponse` 为中间枢纽——任意入站先归一、再出站，与我们的转换矩阵完全同构；内置 Fastify 服务壳，转换器与服务壳可分离使用。
- 生态：CCR 附带 20+ 转换器（各家供应商、cache_control 处理等），周下载约 2.7 千。
- 风险：**单一维护者**，版本尚在 1.0.x，社区 issue 中存在转换边缘用例的缺陷报告。
- **使用策略：vendor（拷入源码树）而非硬依赖**——MIT 允许；我们自建金样测试（golden tests）覆盖流式分帧、tool_use 往返、usage/stop_reason 映射，把它当「经过实战的起点」而不是「可信黑盒」。这同时满足 US-09 对序列化字节路径的完全控制。

## Portkey 网关（MIT，参考价值）

- Hono 构建，Node/Cloudflare Workers 双运行时；入站统一为 OpenAI 兼容 `/v1`，各供应商经 ProviderConfig 声明式参数映射 + requestTransforms/responseTransforms 出站。
- 有成熟的 tool_calls ↔ tool_use 转换与重试/回退实现，MIT 许可证允许抽取局部代码。
- 局限：入站协议单一（一切归一为 OpenAI 格式），Anthropic 原生入站未见文档确认——不满足我们 US-02「Claude Code 零改造直连」的双入口要求，故只作参考不作骨架。

## Bifrost(Apache-2.0) 与 LiteLLM(MIT)：行为对照基准

- Bifrost：Go，高性能定位；确认存在 `/anthropic/v1/messages` 原生入站，官方文档演示 Claude Code 经其转 OpenAI 上游。公开 issue 显示：① 各协议间尚无完备的 N×N 转换（Chat Completions/Responses/Messages 互转不全）；② 流式路径出现过 message_start 帧重复缺陷——**佐证 SSE 转换是高危区，必须金样测试**，也是我们黑盒对照的现成测试点。
- LiteLLM：Python 生态事实标准，映射语义（stop_reason/finish_reason、usage 字段）文档完备，适合当「语义仲裁参照」；技术栈不同不可复用代码。

## 结论与建议

1. **框架自研不动摇**：全部候选要么协议入口不全（Portkey）、要么许可证受限（new-api AGPL / sub2api LGPL）、要么赛道不同（sub2api 订阅池）；且没有一家为「字节稳定的内容优化 + 缓存互锁」预留控制点——这正是我们的差异化。
2. **转换层三层复用策略**：
   - 类型层：直接依赖官方 openai / @anthropic-ai/sdk 的 TS 类型作协议契约；
   - 实现层：vendor @musistudio/llms 的转换器为起点，按 C1 范围裁剪改造；
   - 验证层：以 Portkey/Bifrost/LiteLLM/new-api 为黑盒对照，构建跨实现一致性金样测试集。
3. 该策略天然偏向 **TypeScript 自研方案**（方案 A）：复用件全部是 TS/MIT，官方 SDK 类型也是 TS 一等公民；Go 路线（方案 B）则一切转换代码都要手写（Apache-2.0 的 Bifrost 可参考但其转换矩阵不全）。
