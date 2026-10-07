# 上下文优化 Key 级 canary

## 目标

在 Portal compat 层对已经解析的 OpenAI Chat Completions 和 Anthropic Messages 请求做确定性工具结果压缩。功能默认关闭，只对配置的 Key 生效；变换发生在协议规范化之后、发送上游之前。`/v1/messages/count_tokens` 走同一规则，避免计数与真实请求分叉。

当前 canary 支持两种模式：

- `safe`：只剥离工具结果文本中的 ANSI 控制序列，并折叠连续重复行；
- `bounded`：在 `safe` 基础上，对超过上限的工具结果保留头尾并写入省略标记。

system/developer、用户和 assistant 普通正文、JSON、代码、图片块、tool-call 参数、tool ID 和消息结构保持原样。候选 JSON 没有变小就回退到完整请求。

## 图片历史裁剪（issue #166，吸收 #98）

对「图片块保持原样」边界的受控放开——多轮 agent 会话每轮重发全部历史截图，累计图片数超过后端单请求上限（TensorFold GLM `max_images=50`、vLLM DSpark 16）时每一轮都会 400，会话死亡。规则分两档：

- **限额守卫（guard，全量 Key，独立于 canary 模式）**：模型组配置了 `max_images` 且请求图片总数超过时，从最旧开始把图片 part 原位替换为文字占位符，裁到上限。只改变本会 400 的请求——「成功的请求字节零变化」不变式保持成立。压缩救不了这种失败：上限按张数计、解码前触发，压得再小也是 51 张。
- **canary 裁剪（白名单 Key，safe/bounded 模式）**：触发同上，但裁到 `keep_last`（默认 8）。TensorFold GLM 的共享图片 token 池 `request_image_tokens=16384` 在 ≤8 张时每张保持 2,048 token 满额——一个默认值同时贴合两个后端。

规则细节：

1. 触发计数覆盖规范化后 messages 里的全部 image part（OpenAI `image_url` / Anthropic `image` block，含 tool 消息与 tool_result 内嵌、所有 role），未配置上限的模型组不裁。
2. 最后一条 user 消息的图片永不裁；唯一例外是它自身就超上限（留着必 400）——此时消息内最旧优先降级裁剪，指标带 `last_user_degraded` 标记。
3. 占位符走既有 `[IMAGE_…]` 约定：`[IMAGE_EVICTED: 会话第 k 张截图已移除，仅保留最近 N 张]`。k 为会话内图片序号、N 为保留数，跨轮字节稳定，因此淘汰边界单调（每轮最多前移一张），保护上游 prefix cache。模型可表述「之前的截图已被清理」，不会幻觉自己看过。
4. 被裁块上的 `cache_control` 断点保留在占位符块上。
5. never-worse 尺寸守卫对图片规则是空转的（换占位符必然变小），安全网来自结构不变量与测试；guard 档不做全量重序列化，差值按 part 算术精确计量。
6. 指标独立记 `compat.image_evict`（tier/model/max_images/total/pruned/kept/last_user_degraded/bytes_removed），不落图片内容或 base64；裁剪发生时响应带 `x-portal-images-pruned: <n>`。

## 配置

compat 容器使用以下环境变量：

```text
CONTEXT_OPTIMIZATION_MODE=off|safe|bounded
CONTEXT_OPTIMIZATION_KEYS=<sha256 key hash>,<sha256 key hash>
CONTEXT_OPTIMIZATION_MAX_TOOL_RESULT_BYTES=8192
CONTEXT_OPTIMIZATION_REPEAT_MIN_LINES=2
CONTEXT_OPTIMIZATION_HEAD_BYTES=4096
CONTEXT_OPTIMIZATION_TAIL_BYTES=4096
CONTEXT_OPTIMIZATION_IMAGE_LIMITS=GLM-5.3-Flash-EXL3:50,deepseek-v4-flash-0731:16
CONTEXT_OPTIMIZATION_IMAGE_KEEP_LAST=8
CONTEXT_OPTIMIZATION_IMAGE_GUARD=on
```

`CONTEXT_OPTIMIZATION_KEYS` 只接受完整 SHA-256 Key hash 或 `*`。空列表等同于关闭。Key 明文只在请求头中用于计算 hash，不写入日志或指标。回滚时把 mode 改为 `off` 并重启 compat，原请求字节透传。

`CONTEXT_OPTIMIZATION_IMAGE_LIMITS` 按模型组（litellm group 别名，即客户端请求里的 `model` 字段）配置单请求图片上限，`*:50` 可作未列出组的默认；留空等于全部不裁。`IMAGE_GUARD=off` 时限额守卫也关闭，超限请求交回上游按原生语义 400。

管理员也可以在“请求与用量 → 上下文优化”菜单中保存同一策略。菜单使用共享 monitor SQLite 持久化配置，Key 列表只提交 hash；保存后 compat 在最多一秒的策略缓存窗口内读取新版本。环境变量只作为首次初始化默认值（含 #166 新增图片列的迁移播种），菜单保存后以数据库策略为准。

## 请求流程

```text
client request
  -> protocol normalization (existing compat rules)
  -> key allowlist decision
  -> safe/bounded candidate transform
  -> image-evict（guard 全量 / canary 白名单，见上）
  -> compact JSON size guard
  -> upstream
```

指标只记录模式、节省字节、工具结果节省字节、规则命中和 never-worse 回退，不记录 prompt、response、Key 或 header。RTK CLI 继续作为 benchmark 对照工具，不在请求热路径中启动外部进程；生产 canary 使用同一保护边界的内置确定性规则。

## 性能与安全边界

- 只复制和遍历已解析 JSON；关闭或未命中 Key 时不复制、不重序列化。
- 所有文本操作限制在 tool result 上，工具 schema 和结构字段不参与变换。
- UTF-8 截断不会切断多字节字符；候选不小于原文时完整回退。
- 图片预扫只遍历 messages 数 part，不复制不序列化；未超阈值的请求零额外开销。guard 档裁剪不做全量重序列化。
- 图片 payload 不进入 collector 的 OPF 文本槽位，已有损坏图片快照由 replay 预检跳过。
- 当前 canary 仍需任务质量、工具执行成功率、恢复原文比例和 cache 隔离测试后才能扩大范围。

## 上线顺序

1. 以一个评估 Key 启用 `safe`，观察 `compat.context_optimized` 与上游成功率。
2. 对工具密集 Key 做 `bounded` 小比例对照，核对任务质量和需要恢复原文的比例。
3. 只有协议、延迟、质量和恢复指标稳定后，才扩大 Key 白名单；默认 mode 保持 `off`。
4. 图片裁剪按模型组灰度：先在 `IMAGE_LIMITS` 中加入真实撞墙的组（上限与后端对齐），guard 全量生效；`keep_last=8` 的质量档随白名单 Key 灰度，并用真实 agent 循环对照任务成功率与重复截屏次数。
