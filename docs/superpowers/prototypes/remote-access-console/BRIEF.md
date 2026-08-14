# 远程模型网关控制台高保真原型 · 开发规范（BRIEF）

> 本文件是本原型的统一契约。产品名称：**远程模型网关**（局域网私有推理模型的统一公网入口）。
> 设计依据：`docs/superpowers/specs/2026-08-14-remote-model-access-prototype-design.md`；
> 需求基线：`planning/03-core/prototype_remote_model_access_baseline_proto-r4.md`（US-P1~P12、C1-C5）。
> 视觉风格与组件语言直接沿用同目录下 `../llm-portal-console/`（风格 A · 企业蓝）——**两者是不同产品**，仅共享视觉与组件库，不共享导航、数据或业务含义。

## 0. 与 llm-portal-console 的关系（重要）

- 本原型是**独立产品**：面向「把局域网里已经在跑的私有推理模型，安全地开放给公网/异地客户端调用」这一场景（WireGuard 站点接入 + LiteLLM 路由 + mcp-hub），而不是旧原型的「多云 Provider 网关」场景。
- **只复制视觉**：`assets/portal.css`、`assets/portal.js` 是从 `llm-portal-console/assets/` 复制后按本产品需要**增量扩展**得到的（新增 4 组 class，未删改任何旧 class，见 §3）。
- **未修改** `llm-portal-console/` 下任何文件。
- 两个原型的页面、路由、导航、假数据互不引用、互不依赖。

## 1. 技术约束

- 纯静态：HTML + CSS + 原生 JS，零构建、零外部依赖（无 CDN、无外部字体/图片；图标用内联 SVG 或字符）。
- 页面间用普通 `<a href>` 跳转；可直接以本地文件方式打开，也可用任意静态服务器（如 `python3 -m http.server`）访问。
- 全部 UI 文案中文；域名占位符统一用 `gw.example.com`；密钥只显示尾 4 位或明确标注为一次性展示的假值；公钥展示为 44 字符、`=` 结尾的假 base64 指纹样式。
- 无真实网络请求、无表单真实提交；交互通过 `portal.js` 助手函数 + 页面级 `<script>` 完成（弹窗开合、倒计时、行内状态切换、表格筛选、折叠展开）。

## 2. 文件布局

```
docs/superpowers/prototypes/remote-access-console/
├── assets/
│   ├── portal.css   ← 复制自 llm-portal-console，末尾追加 4 组新组件（§3）
│   └── portal.js    ← 结构与助手函数不变，仅替换 NAV 导航表 + logo 文案
├── login.html        管理员登录（Master Key，无壳）
├── index.html        仪表盘
├── sites.html        站点与公钥管理（US-P7/P8）
├── models.html       模型与别名（US-P6/P11）
├── keys.html         用户 Key 管理（US-P9/C3）
├── usage.html        管理员用量视图（US-P9）
├── my-usage.html     普通用户自查视图（US-P10）
├── mcp.html          MCP 管理（US-P4/P12）
└── BRIEF.md          本文件
```

## 3. 设计令牌与新增组件

色板 / 字体 / 间距 / 表格 / 按钮等基础令牌与 `llm-portal-console/BRIEF.md` §3 完全一致（未修改 `portal.css` 中任何既有规则），此处不重复列出。**本原型在 `portal.css` 末尾新增了 4 组组件**（仅追加，未删除任何旧类）：

| 组件 | class | 用途 |
|---|---|---|
| 纯 CSS 条形图 | `.pf-bar-item` / `.pf-bar-label` / `.pf-bar-track` / `.pf-bar-fill`（+`.violet`/`.green`）/ `.pf-bar-value` | `usage.html` 「按 Key 请求占比」 |
| 弱化行 | `tr.pf-row-muted` | 已吊销站点 / 已禁用 Key 的整行降重 |
| 攻击面/状态一览行 | `.pf-surface-row` / `.pf-surface-main` / `.pf-surface-icon` / `.pf-surface-title` | 仪表盘「站点隧道状态」「公网攻击面」 |
| 折叠行（deployment 展开） | `.pf-collapse-row` / `.pf-collapse-body` / `.pf-dep-item` / `.pf-dep-main` / `.pf-btn-icon`（+`.expanded`） | `models.html` 模型 → deployment 展开 |

## 4. 页面壳与助手（portal.js 约定）

结构与 `llm-portal-console` 完全一致：页面写 `<body data-page="key">` + `<template id="page">…</template>`，`portal.js` 读取 `data-page` 注入侧边栏（logo「远程模型网关」+ 导航 + 底部 `管理员 admin`）与顶栏，并把模板内容移入 `.pf-content`；`login.html` 不写 `data-page`/`#page`，不注入壳，但助手函数仍可用。

导航表（key → 标题 → 文件），共 7 项，**不含 login**：

| key | 标题 | 文件 |
|---|---|---|
| dashboard | 仪表盘 | index.html |
| sites | 站点与公钥 | sites.html |
| models | 模型与别名 | models.html |
| keys | 用户 Key | keys.html |
| usage | 用量总览 | usage.html |
| my-usage | 我的用量 | my-usage.html |
| mcp | MCP 管理 | mcp.html |

助手函数（全局导出，行为与旧原型一致）：`pfTabs(container)`、`pfDrawer(id)`/`pfDrawerClose()`、`pfModal(id)`/`pfModalClose()`、`pfToast(msg)`；声明式绑定 `[data-drawer]`/`[data-modal]`/`[data-close]`/`[data-toast]`；`Esc` 关闭浮层；`.pf-code` 自动注入「复制」按钮。两处「仅显示一次」弹窗（`keys.html` 新建密钥、`sites.html` 新增站点安装命令）复用 `llm-portal-console/keys.html` 的两态切换模式（一个 JS 函数按布尔值切换表单态/结果态两组元素的 `display` 与底部按钮）。

## 5. 共享假数据契约（跨页一致，不得改动）

### 5.1 站点（sites.html / index.html / models.html）

| 站点名 | WG IP | 公钥指纹（假） | 最近握手 | 模型部署 | 状态 |
|---|---|---|---|---|---|
| hq-office | 10.77.0.11 | `8f3nJ0kL2mN4…9hJk=` | 23 秒前 | deepseek-v4-flash-0731:8890、qwen3.6-35b-a3:8004 | 在线 |
| lab-2f | 10.77.0.12 | `rT9mK2pL5nQ8…5nPqx=` | 1 分钟前 | deepseek-v4-flash-0731:8890 | 在线 |
| old-site | 10.77.0.9 | `zA1bC4dE5fG9…A1bCd=` | 27 天前 | 无（已摘除） | 已吊销（2026-07-18），全页面灰化 |

### 5.2 模型与别名（models.html / index.html / usage.html / my-usage.html）

| 对外模型名 | 类型 | Deployment | 路由策略 |
|---|---|---|---|
| deepseek-v4-flash-0731 | 直选 | hq-office（10.77.0.11:8890，rpm120/tpm300000，健康）+ lab-2f（10.77.0.12:8890，rpm60/tpm150000，冷却中 43s） | least-busy |
| qwen3.6-35b-a3 | 直选 | hq-office（10.77.0.11:8004，rpm60/tpm120000，健康） | least-busy |
| claude-opus-5 | 别名 → deepseek-v4-flash-0731 | 同上 2 个 deployment | 继承 |
| gpt-4o | 别名 → deepseek-v4-flash-0731 | 同上 2 个 deployment | 继承 |

### 5.3 用户 Key（keys.html / usage.html / my-usage.html / mcp.html）

| 备注名 | Key 尾 4 位 | 创建时间 | 状态 |
|---|---|---|---|
| 默认管理员测试 | a1b2 | 2026-06-02 | 启用 |
| chris-laptop | 9f3e | 2026-07-10 | 启用 |
| 家人共用 | 77c0 | 2026-07-22 | 已禁用（2026-08-05），今日用量恒为 0 |

### 5.4 用量数字（自洽，务必跨页对账）

- **仪表盘/usage.html 合计（今日）**：请求 1,842（↑9%）；Token 4.60M（输入 3.68M / 输出 0.92M）。
- **usage.html 明细**（四行，求和 = 上条合计）：
  - 默认管理员测试 + deepseek-v4-flash-0731：612 次 / 入 1,224,000 / 出 306,000
  - 默认管理员测试 + qwen3.6-35b-a3：58 次 / 入 116,000 / 出 29,000
  - chris-laptop + claude-opus-5：740 次 / 入 1,480,000 / 出 370,000
  - chris-laptop + gpt-4o：432 次 / 入 864,000 / 出 216,000
- **按 Key 占比条形图**：chris-laptop 1,172 次（100% 基准）；默认管理员测试 670 次（57%）；家人共用 0 次。
- **my-usage.html（chris-laptop 自查）**：请求 1,172、输入 2,344,000、输出 586,000 —— 精确等于 usage.html 中属于 chris-laptop 的两行之和。
- **近期错误**（跨 index.html / usage.html 共用同 3 条“今日核心错误”，usage.html 额外追加 2 条同类重复项用于凸显模式）：
  1. 14:52:07 · `…77c0` · deepseek-v4-flash-0731 · 401 · Key 已禁用（家人共用）
  2. 14:30:15 · `…9f3e` · `gpt-4.1` · 404 · 模型不存在（未注册的对外模型名）
  3. 13:58:42 · `…a1b2` · qwen3.6-35b-a3 · 503 · 全部站点不可用
  4.（仅 usage.html）11:12:39 · `…77c0` · deepseek-v4-flash-0731 · 401 · 禁用后仍尝试调用
  5.（仅 usage.html）09:47:02 · `…9f3e` · `claude-opus-6` · 404 · 对外名拼写错误

### 5.5 MCP 调用计数（mcp.html / my-usage.html，务必对账）

| Key | analyze_image（内建） | zhipu_vision_caption（外部） | zhipu_vision_ocr（外部） | 合计 |
|---|---|---|---|---|
| 默认管理员测试 | 12 | 0 | 0 | 12 |
| chris-laptop | 23 | 4 | 1 | 28 |
| 家人共用 | 0（已禁用） | 0 | 0 | 0 |

外部 MCP 注册：`zhipu vision-mcp-server`，URL `https://open.bigmodel.cn/api/mcp/vision`，凭据「已保存 · 尾 4 位 9c2d」，工具前缀 `zhipu_`，状态正常。

## 6. 各页面必备要素与故事映射

- **login.html**：Master Key 登录（无壳）；说明 Master Key 仅用于控制台与站点接入注册、不作为客户端调用凭据；底部攻击面提示「公网仅开放 443/tcp · 51820/udp」。
- **index.html（仪表盘）**：4 统计卡（今日请求/Token/站点隧道 2/3/模型部署健康 2/3）；「站点隧道状态」+「公网攻击面」双卡（仅 443/51820，C1/C2 基线）；「模型 Deployment 健康」表；「最近错误」表。
- **sites.html（US-P7/P8）**：站点表（站点名/公钥指纹/WG IP/最近握手/模型数/状态/操作）；「新增站点」两态弹窗（表单 → 一次性 15 分钟倒计时安装命令 `curl … token`）；「吊销确认」弹窗（明示隧道立即断开 + 路由池摘除 + 不可逆），确认后就地把该行降级为已吊销灰态。
- **models.html（US-P6/US-P11）**：可展开表格（直选/别名 chip、deployment 数、least-busy 路由、健康摘要），点击行展开每个 deployment 的 api_base/限流/状态；「路由与解析说明」kv 卡；「新建别名」弹窗。
- **keys.html（US-P9/C3）**：Key 列表 + 启用/禁用即时切换；「新建密钥」两态弹窗（表单 → 一次性展示假 `sk-gw-…` 全文）。
- **usage.html（US-P9）**：Key/模型筛选工具栏（真实 `<select>` change 事件过滤表格行 + 空态行）；用量明细表（含 tfoot 合计）；按 Key 占比条形图；近期错误表。
- **my-usage.html（US-P10）**：顶部 `.pf-banner.info` 明示「只能查看自己 Key 的用量」（模拟身份 chris-laptop）；API 用量明细 + MCP 用量明细两张表，数字与 §5.4/§5.5 对账。
- **mcp.html（US-P4/US-P12）**：内建工具卡 `analyze_image`（standing on qwen3.6-35b-a3，说明凭据边界与本地图片上传前置步骤）；外部 MCP 注册表（zhipu 示例）+「注册外部 MCP」弹窗；聚合 `tools/list` 预览（`.pf-code` JSON）；按 Key 调用计数表；`POST /mcp/upload` 接口说明卡（30 分钟 TTL、≤10MB、jpg/png/webp/gif、`.pf-code` curl 示例）。

## 7. 一致性红线

- 不改 §5 假数据；跨页引用同一实体（站点/模型/Key/数字）必须同名同值，尤其是 usage.html ↔ my-usage.html ↔ mcp.html 三处数字对账关系（§5.4、§5.5）。
- 颜色/圆角/字号只用 §3 及 `llm-portal-console/BRIEF.md` §3 令牌；新增组件只追加、不覆盖旧类。
- 密钥只显示尾 4 位，公钥只显示指纹样式，唯一例外是「仅显示一次」弹窗内的一次性明文展示。
- 域名占位符统一 `gw.example.com`；不引入真实域名、真实 IP（WG 段固定用 10.77.0.x）。
- 每页 `<title>` 与顶栏标题一致；侧边栏激活项正确；不修改 `llm-portal-console/` 下任何文件。
