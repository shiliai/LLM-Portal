# 上下文优化 Key 级 canary

## 目标

在 Portal compat 层对已经解析的 OpenAI Chat Completions 和 Anthropic Messages 请求做确定性工具结果压缩。功能默认关闭，只对配置的 Key 生效；变换发生在协议规范化之后、发送上游之前。`/v1/messages/count_tokens` 走同一规则，避免计数与真实请求分叉。

当前 canary 支持两种模式：

- `safe`：只剥离工具结果文本中的 ANSI 控制序列，并折叠连续重复行；
- `bounded`：在 `safe` 基础上，对超过上限的工具结果保留头尾并写入省略标记。

system/developer、用户和 assistant 普通正文、JSON、代码、图片块、tool-call 参数、tool ID 和消息结构保持原样。候选 JSON 没有变小就回退到完整请求。

## 配置

compat 容器使用以下环境变量：

```text
CONTEXT_OPTIMIZATION_MODE=off|safe|bounded
CONTEXT_OPTIMIZATION_KEYS=<sha256 key hash>,<sha256 key hash>
CONTEXT_OPTIMIZATION_MAX_TOOL_RESULT_BYTES=8192
CONTEXT_OPTIMIZATION_REPEAT_MIN_LINES=2
CONTEXT_OPTIMIZATION_HEAD_BYTES=4096
CONTEXT_OPTIMIZATION_TAIL_BYTES=4096
```

`CONTEXT_OPTIMIZATION_KEYS` 只接受完整 SHA-256 Key hash 或 `*`。空列表等同于关闭。Key 明文只在请求头中用于计算 hash，不写入日志或指标。回滚时把 mode 改为 `off` 并重启 compat，原请求字节透传。

## 请求流程

```text
client request
  -> protocol normalization (existing compat rules)
  -> key allowlist decision
  -> safe/bounded candidate transform
  -> compact JSON size guard
  -> upstream
```

指标只记录模式、节省字节、工具结果节省字节、规则命中和 never-worse 回退，不记录 prompt、response、Key 或 header。RTK CLI 继续作为 benchmark 对照工具，不在请求热路径中启动外部进程；生产 canary 使用同一保护边界的内置确定性规则。

## 性能与安全边界

- 只复制和遍历已解析 JSON；关闭或未命中 Key 时不复制、不重序列化。
- 所有文本操作限制在 tool result 上，工具 schema 和结构字段不参与变换。
- UTF-8 截断不会切断多字节字符；候选不小于原文时完整回退。
- 图片 payload 不进入 collector 的 OPF 文本槽位，已有损坏图片快照由 replay 预检跳过。
- 当前 canary 仍需任务质量、工具执行成功率、恢复原文比例和 cache 隔离测试后才能扩大范围。

## 上线顺序

1. 以一个评估 Key 启用 `safe`，观察 `compat.context_optimized` 与上游成功率。
2. 对工具密集 Key 做 `bounded` 小比例对照，核对任务质量和需要恢复原文的比例。
3. 只有协议、延迟、质量和恢复指标稳定后，才扩大 Key 白名单；默认 mode 保持 `off`。
