# sub2api 分析：订阅池网关的定位、架构与对 LLM-portal 的可复用性

> 来源：GitHub 仓库 Wei-Shaw/sub2api（zread 结构与文件检索 + 公开资料，2026 年 8 月 9 日）
> 研究方式：主会话轻量调研，未克隆仓库；结论供技术方案选型使用
> 许可证：LGPL-3.0（已核对仓库 LICENSE 原文）

## 一句话结论

sub2api 解决的是「把 Claude Pro/Max、ChatGPT 等**订阅账号**池化成 API 端点」的问题，与 LLM-portal「企业统一 API 网关」属于**不同赛道**；其部署依赖（PostgreSQL 15+ 与 Redis 7+）同我们的单容器 SQLite 目标冲突，LGPL-3.0 也不适合把代码拷入闭源代码库。**定位建议：参考实现 + 潜在上游，不作二开基座。**

## 项目定位

- 核心价值：管理一池 OAuth 订阅账号（Claude Pro/Max、OpenAI/ChatGPT、Gemini、Grok 等），对外暴露标准 API；含账号健康管理、粘性会话、故障切换、用量计费。
- 用户画像：手里有多个订阅账号、想把订阅额度转成 API 供程序调用的个人/团队——与「企业已有 API Key，需要统一治理」的 LLM-portal 场景正交。
- 社区规模：约 3.4 万 star；open issue 约 1.7 千，含计费倍率类缺陷报告，工程质量有波动。

## 技术栈与架构要点

- 后端 Go：Gin + Ent ORM + Wire 依赖注入；前端 Vue3 + TypeScript。
- **硬依赖 PostgreSQL 15+ 和 Redis 7+**——与 LLM-portal「单容器、SQLite、半小时跑通」的部署约束直接冲突。
- 双 Handler 设计：GatewayHandler 承接 Anthropic/Gemini/Antigravity 平台，OpenAIGatewayHandler 承接 OpenAI/Grok；同一 `/v1/messages` 路径按密钥组所属平台分发。
- Codex 桥接：把 Claude `/v1/messages` 请求转换为 OpenAI `/v1/responses` 协议，是一条可参考的跨协议转换实现路径。
- 故障切换时做 cache_read 计费换算——印证「failover 与 prompt cache 计量必须联合设计」，与我们 US-04/US-08 的交叉点一致。

## 对 LLM-portal 的三种可能关系

| 关系 | 评估 |
|------|------|
| 二开基座 | 否。问题域不同（订阅池化 vs 企业网关）、重依赖冲突、LGPL 拷贝代码有传染义务 |
| 参考实现 | 是。`/v1/messages` 双协议分发、粘性会话、failover + 缓存计费换算的处理思路值得对照阅读 |
| 潜在上游 | 是。用户当前已在运行 sub2api；LLM-portal 的上游适配器把它当作一个「Anthropic 原生 / OpenAI 兼容」上游指向即可，两者天然串联 |

## 许可证边界（LGPL-3.0）

- 独立进程调用（网关把 sub2api 当上游 HTTP 服务）完全无义务。
- 把其 Go 源码拷入我们代码库则该部分需保持 LGPL 且允许替换/再链接，对闭源商用产品运维成本高——只读源码参考思路，不搬运代码。

## 结论

1. 不作为二开对象；其存在反而简化我们的兼容性验证——把 sub2api 作为一个真实上游做集成测试。
2. 值得精读的参考点：同路径双协议分发、failover 中的缓存计费换算、订阅账号粘性会话（我们虽无账号池，粘性思想可用于主备切换的会话一致性）。
3. 用户现网已部署 sub2api，MVP 首个演示场景可直接用「LLM-portal → sub2api → 上游」链路验证 US-01/US-02。
