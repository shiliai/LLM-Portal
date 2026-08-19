# 路线图

以下功能来自初始 Spec，当前 MVP 尚未实现：

- 通用 Provider 管理：注册本地或远程上游、托管上游凭据，并对远程地址执行 SSRF 与 DNS rebinding 防护。
- 原生 Anthropic 上游：支持 OpenAI Chat Completions 到 Anthropic Messages 的反向协议转换。
- 有序主备路由：为模型配置主上游和备选上游，并记录、展示故障切换事件。
- Provider 并发限制：按私有节点算力配置最大并发，只向有空余容量的节点路由；全部可用节点达到上限时返回 `429 Too Many Requests` 和 `Retry-After`，通知客户端稍后重试。
- System Prompt 策略：按 Key 或模型配置注入、追加和替换规则。
- Key 配额与限流：按 Key 设置额度、请求速率和 token 速率限制。
- 成本与 Prompt Cache 治理：配置模型价格，展示估算成本、缓存命中率和节省金额，并支持自动注入缓存断点。
- 内容优化管道：按 Key 对工具输出执行 ANSI 剥离、重复行折叠和超大块截断，并展示优化前后的 token 差异。
- 首次启动向导：在浏览器内完成管理员设置、首个上游、模型映射和 Key 发放。
- OpenAI Responses API：受管透传 `/v1/responses`，包含鉴权、模型映射、流式工具调用和用量计量。
- 对话数据保存与消费：按 Key 选择性保存正文，提供保留期清理、控制台查看、SSE 事件流和分页 REST 查询。
- Anthropic 兼容模式控制：按 Key 切换兼容或严格模式，并在调用日志中记录脱敏的规范化事件。
