# LLM-portal 控制台高保真原型 · 开发规范（BRIEF）

> 本文件是原型开发的统一契约。所有页面开发 agent 必须先读本文件再动手。
> 设计依据：`docs/superpowers/specs/2026-08-13-llm-portal-mvp-design.md`（基线 r4）。
> 用户已选定风格 **A：企业蓝 · 清爽专业**（类 Ant Design 中后台）。

## 1. 技术约束

- **纯静态**：HTML + CSS + 原生 JS，零构建、零外部依赖（无 CDN、无外部字体、无图片文件；图标用内联 SVG 或字符）。
- 多页面站点，页面间用普通 `<a href>` 跳转。
- 所有 UI 文案中文。数据为写实假数据（见 §5，跨页面必须一致）。
- 每页可交互的部分：Tab 切换、抽屉、弹窗、向导步进（用 §4 的助手函数）。表单不需要真实提交，按钮点击可弹提示或切换静态状态。

## 2. 文件布局

```
docs/superpowers/prototypes/llm-portal-console/
├── assets/portal.css      ← 设计令牌 + 全部通用组件（foundation agent 负责）
├── assets/portal.js       ← 壳注入 + 交互助手（foundation agent 负责）
├── index.html             仪表盘
├── providers.html         上游 Provider
├── mappings.html          模型映射
├── keys.html              虚拟密钥（含按密钥策略）
├── logs.html              调用日志（列表 + 详情抽屉）
├── prices.html            价格表
├── credentials.html       数据凭据
├── events.html            事件
├── settings.html          设置
├── login.html             登录（无壳布局）
└── wizard.html            初始化向导（无壳布局）
```

## 3. 设计令牌（风格 A，与用户选定 mockup 一致）

| 令牌 | 值 |
|---|---|
| 页面底色 | `#f0f2f5` |
| 卡片/面板 | `#fff`，边框 `1px solid #f0f0f0`，圆角 `4px`（外层大容器 6px） |
| 主色 | `#1677ff`；主色浅底 `#e6f4ff`；主色深 `#0958d9` |
| 文字 | 主 `#1f1f1f`，次 `#595959`，弱 `#8c8c8c`，禁用 `#bfbfbf` |
| 分隔线 | `#f0f0f0`；输入框边框 `#d9d9d9` |
| 成功 | 字 `#389e0d` 底 `#f6ffed` 框 `#b7eb8f` |
| 警告 | 字 `#d46b08` 底 `#fff7e6` 框 `#ffd591` |
| 错误 | 字 `#cf1322` 底 `#fff1f0` 框 `#ffa39e` |
| 信息/缓存 | 字 `#0958d9` 底 `#e6f4ff` 框 `#91caff` |
| 特殊/切换 | 字 `#531dab` 底 `#f9f0ff` 框 `#d3adf7` |
| 字体 | 系统栈；代码/密钥/URL 用 `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` |
| 表格 | 表头 `#fafafa` 底、11-12px；行分隔 `#f5f5f5`；hover 行 `#fafafa` |
| 侧边栏 | 白底右边框；菜单项 12.5px `#595959`，激活 `#e6f4ff` 底 + `#1677ff` 字 + 500 字重，圆角 4px |
| 顶栏 | 白底下边框，页面标题 15px/600 |
| 按钮 | 主按钮 `#1677ff` 白字；次按钮白底 `#d9d9d9` 框；危险按钮 `#ff4d4f`；高度 30px 圆角 4px |
| 间距 | 内容区 padding 16-20px；卡片间 gap 12px |

## 4. 页面壳与助手（portal.js 约定）

**控制台页面**（除 login/wizard）统一结构：

```html
<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>页面名 · LLM-portal</title>
<link rel="stylesheet" href="assets/portal.css"></head>
<body data-page="providers"><!-- data-page = 导航 key -->
<template id="page">
  …页面内容（只写内容区，不写侧边栏/顶栏）…
</template>
<script src="assets/portal.js"></script></body></html>
```

`portal.js` 读取 `data-page`，注入：左侧边栏（logo「LLM-portal」+ 导航 + 底部 `管理员 admin` chip）、顶栏（导航项对应标题 + 右侧管理员头像 `A`），并把 `#page` 模板内容放入 `.pf-content`。导航（key → 标题 → 文件）：

| key | 标题 | 文件 |
|---|---|---|
| dashboard | 仪表盘 | index.html |
| providers | 上游 Provider | providers.html |
| mappings | 模型映射 | mappings.html |
| keys | 虚拟密钥 | keys.html |
| logs | 调用日志 | logs.html |
| prices | 价格表 | prices.html |
| credentials | 数据凭据 | credentials.html |
| events | 事件 | events.html |
| settings | 设置 | settings.html |

**助手函数**（portal.js 提供，全局）：
- `pfTabs(container)`：`.pf-tabs > .pf-tab[data-tab]` 与同级 `.pf-tabpane[data-tab]` 联动；页面加载时自动对全部 `.pf-tabs` 初始化。
- `pfDrawer(id)` / `pfDrawerClose()`：打开/关闭右侧抽屉 `#<id>.pf-drawer`（带遮罩，宽 560px）。
- `pfModal(id)` / `pfModalClose()`：居中弹窗 `#<id>.pf-modal`（带遮罩，宽 520px）。
- `pfToast(msg)`：右上角 2 秒浅色提示（用于「已保存」「已复制」等假交互）。

**通用组件 class**（portal.css 提供）：`.pf-card`（面板）、`.pf-table`、`.pf-chip`（+`.ok/.warn/.err/.info/.violet/.gray`）、`.pf-btn`（+`.primary/.danger/.sm`）、`.pf-input/.pf-select/.pf-switch/.pf-checkbox`、`.pf-form-row`（label 左 140px + 控件）、`.pf-desc`（灰色说明字）、`.pf-kv`（详情键值对）、`.pf-code`（等宽代码块，深底 `#1f1f1f` 白字，带复制按钮）、`.pf-key`（等宽密钥片）、`.pf-empty`（空态）、`.pf-stat`（统计卡）、`.pf-page-head`（内容区头：说明文字 + 右侧主操作按钮）、`.pf-steps`（向导步进条）。

## 5. 共享假数据（跨页一致，不得改动）

**上游 Provider（4 个）**
| 名称 | base_url | 协议能力 | 安全标记 | 状态 |
|---|---|---|---|---|
| openai-main | https://api.openai.com | chat_completions、responses | — | 正常（上次测试 2 分钟前）|
| anthropic-main | https://api.anthropic.com | anthropic_messages | — | 正常 |
| deepseek-backup | https://api.deepseek.com | chat_completions | — | 正常 |
| vllm-internal | http://192.168.1.50:8000/v1 | chat_completions | 已允许私网/回环 + 明文 HTTP（警示标签）| 正常 |

**模型映射（4 条）**
| 对外模型名 | 主目标 | fallback 链 | 入口可路由 |
|---|---|---|---|
| gpt-4o | openai-main / gpt-4o | deepseek-backup / deepseek-chat | OpenAI ✓ Anthropic ✓ Responses ✓ |
| claude-sonnet | anthropic-main / claude-sonnet-4-5 | 无 | OpenAI ✓ Anthropic ✓ Responses ✗ |
| deepseek-chat | deepseek-backup / deepseek-chat | 无 | OpenAI ✓ Anthropic ✓ Responses ✗ |
| qwen-local | vllm-internal / Qwen2.5-72B-Instruct | 无 | OpenAI ✓ Anthropic ✓ Responses ✗ |

**虚拟密钥（4 把）**
| 密钥（前缀预览） | 备注名 | 授权模型 | 关键策略 | 本月用量 | 状态 |
|---|---|---|---|---|---|
| sk-portal-a3f4…9k2e | 研发部-Claude Code | claude-sonnet, qwen-local | US-13 兼容模式；对话保存开；限流 60 RPM | ¥86.40 | 启用 |
| sk-portal-7bc2…m1qf | 市场部-内容组 | gpt-4o | system prompt 注入「品牌口径 v3」；月度限额 ¥500（已用 42.9%）| ¥214.60 | 启用 |
| sk-portal-c9d1…t7x2 | 测试-Codex | gpt-4o | Responses 受管透传；工具输出优化 us09-v1 观察模式 | ¥12.75 | 启用 |
| sk-portal-f2e8…w4hj | 旧-临时排查 | — | — | ¥0.00 | 已吊销（2026-08-01）|

**数据凭据**：`dk-portal-5h8s…p3q9`「数据分析管道」，SSE 连接活跃（3 分钟前），已消费事件 1,204 条。

**价格表**（¥/百万 token，缓存读默认 0.1×、写 1.25× 输入价）
| 目标 | 输入 | 输出 | 缓存读 | 缓存写 | 价格版本 |
|---|---|---|---|---|---|
| openai-main / gpt-4o | 18.00 | 72.00 | 1.80 | 22.50 | v3（2026-08-02 生效）|
| anthropic-main / claude-sonnet-4-5 | 21.60 | 108.00 | 2.16 | 27.00 | v2（2026-07-15）|
| deepseek-backup / deepseek-chat | 4.00 | 16.00 | 0.40 | 5.00 | v1 |
| vllm-internal / Qwen2.5-72B-Instruct | 0 | 0 | 0 | 0 | v1（自建，不计费）|

**仪表盘核心数字**：今日请求 12,847（+18%）；Token 8.4M（输入 6.1M / 输出 2.3M）；估算成本 ¥214.60；缓存命中率 37.2%（节省 ¥58.21）。

**调用日志样例**（≥10 行，混合以下形态）：200 成功；200 + 「已切换 deepseek-backup」紫色徽章（US-04）；200 + 「缓存命中」蓝徽章；429 `rate_limit_exceeded`；429 `insufficient_quota`；400 `invalid_request`（US-13 严格模式拒绝）；502 `upstream_error`；「部分完成」（流中断）。列：时间 / 密钥 / 入口协议 / 模型 / 上游 / 状态 / Tokens(入/出) / 耗时 / 成本。

**日志详情抽屉**（点击行打开，展示 gpt-4o 切换那条）：
- 概要：入口 OpenAI `/v1/chat/completions`、流式、TTFB 420ms、总耗时 8.1s；
- 尝试链：① openai-main / gpt-4o — 连接超时 10.2s → 切换；② deepseek-backup / deepseek-chat — 成功；
- 版本三元组：`us13-v1` / 策略版本 `policy-v3` / `us09-v1`；
- 用量：input 3,204 / output 812 / cache_read 1,024 / cache_write 0；`usage_estimated=false`；
- 成本：¥0.087（价格版本 v3，请求时冻结）；
- 「查看对话」按钮（该密钥开了 US-12）→ 切换到只读消息列表（system/user/assistant 气泡各 1-2 条）。

**事件样例**：US-04 上游切换（openai-main→deepseek-backup，附错误摘要）；C5 缓存失效宣告（`us09-v1 → us09-v2` 版本变更，影响密钥 2 把）；US-13 变换计数（今日 312 次，内联 system 提升 287 / 空 content 规范化 25）；US-12 持久化失败计数 +1（磁盘写入超时）。

## 6. 各页面必备要素（对应设计文档章节）

- **index.html 仪表盘**：4 统计卡 + 7 日趋势 SVG 折线卡 + 最近调用表（8-10 行，含徽章）+ 右侧小卡：队列积压 0 / 持久化失败 1（点击跳 events）。
- **providers.html**：列表卡（表格：名称/base_url/协议能力 chips/安全标记/状态/操作「测试连接·编辑·删除」）+「新建 Provider」弹窗表单：名称、base_url（带 C8 提示文案：默认拒绝私网/回环地址，需显式勾选）、协议能力多选 checkbox ×3、凭据（密码框 + “保存后加密，不再回显”说明）、`允许私网/回环` `允许明文 HTTP` 两个勾选 + 警示、「测试连接」按钮。
- **mappings.html**：列表（对外模型名/主目标/fallback 链/入口可路由三 chip/操作）+ 新建/编辑弹窗：对外模型名、主目标（provider+上游模型两级选择）、fallback 多条排序列表（可增删）、自动计算的「入口可路由」预览（说明：由目标协议能力推导）。
- **keys.html**：列表 + 「新建密钥」弹窗（备注名、授权模型多选、生成后弹「密钥仅显示一次」全量展示 + 复制）；点击行进入**密钥详情抽屉**，Tab×4：`基础`（备注/授权模型/状态/吊销按钮）、`限额与限流`（RPM 限流、月度金额限额 + 进度条、429 两种 code 说明文案）、`内容策略`（US-05 system prompt 策略：注入/追加/替换单选 + 文本域 + 版本号；US-09 工具输出优化：观察模式/开启 + us09-v1 说明；US-08 缓存自动注入：off/system/system+tail 单选 + “仅 Anthropic 原生上游”说明；US-13 兼容/严格单选）、`数据`（US-12 对话保存开关 + 保留期说明）。
- **logs.html**：筛选栏（密钥/模型/状态/时间范围）+ 分页表 + 详情抽屉（见 §5）。
- **prices.html**：按 §5 表格 + 行内编辑弹窗（4 个单价 + “缓存读 0.1× / 写 1.25× 一键带出”按钮 + 价格版本说明：修改产生新版本，只影响之后请求）。
- **credentials.html**：数据凭据列表 + 新建（dk-portal- 仅显示一次）+ 接口文档卡两张：`GET /data/v1/stream`（SSE，Last-Event-ID 续传说明 + 示例 curl）与 `GET /data/v1/conversations`（keyset 分页 + 示例 curl），用 `.pf-code` 展示。
- **events.html**：typed 事件表（时间/类型 chip/详情摘要），类型筛选；§5 的 4 类事件都要出现。
- **settings.html**：分组卡片：日志保留（90 天）/ 正文保留（7 天）/ 三超时（连接 10s、首字节 60s、流空闲 120s，标注「示意默认值」）/ US-13 全局默认（兼容）/ 管理员密码修改 / 备份说明（冷备 + `sqlite3 .backup` 热备文案）。
- **login.html**：居中卡片（logo、密码输入、登录按钮），无壳。底部灰字「LLM-portal · 自部署统一 AI 网关」。
- **wizard.html**：无壳，顶部 `.pf-steps` 四步（管理员密码 → 首个上游 → 首条映射 → 发放密钥）+ 完成页（两条可复制 curl：OpenAI `/v1/chat/completions` 与 Anthropic `/v1/messages`，指向 `http://<网关>:8080`）；步骤间「上一步/下一步」可点击切换（JS 静态切换）；页面顶部横幅说明：「系统未初始化：完成向导前，管理接口与数据面不可用」。

## 7. 一致性红线

- 不改 §5 数据；页面间引用同一实体必须同名同值。
- 颜色/圆角/字号只用 §3 令牌；不引入新主色。
- 全部密钥只显示前缀预览（`sk-portal-xxxx…xxxx`），除“仅显示一次”弹窗场景。
- 每页 `<title>` 与顶栏标题一致；侧边栏激活项正确。
