# #190 首阶段：排队 / 活动任务查看

## 范围与用户故事

baseline_revision: `190-view-v1`。权威来源为用户 2026-11-12 的收敛指示「先做排队/活动任务查看」，
沿用 #190 US-Q1，将活动请求查看纳入同一个查看故事；US-Q2 调度延后。本次交付层级是代码评审 PR，
不包含合并、生产部署、清理既有请求或发版验收。

**US-Q1 查看**：作为 Portal 管理员，我希望看到正在等待的具体请求、来源与等待时长，以便知道谁在排队、卡在哪里。

- Given 经 compat 的请求正在等待或接收响应，When 管理员打开「活动任务」，Then 能看到逐请求
  ID、Key 别名/不可逆指纹、分组、模型、协议、来源 IP、耗时和可确认的响应阶段，并能搜索和筛选。
- Given 推理后端只提供汇总 gauge，When 查看排队与运行，Then 逐实际 endpoint 去重展示后端统计；
  等待响应不冒充后端确认排队，接收响应不冒充 scheduler 运行，未知指标显示 —。
- Given 未启用追踪、worker 重启/失联、采集失败或身份无权限，Then 不显示健康的零请求；
  未登录与普通 Key 无实时 API 访问权限，列表不含对话/响应内容或 Key 明文。

## 实现与可见边界

- `live_requests.py`：兼容入口为每次 HTTP 调用生成独立 ID（客户端重复 request ID 不覆盖），
  接收阶段、等响应阶段、接收响应阶段分别追踪；在整个 ASGI 响应退出时移除，包括 SSE、异常与取消。
  metadata-only 的每 worker SQLite 快照由后台合并发布、5 秒心跳维持；请求/首段响应不等待写盘。
  30 秒后不计入当前活动数，1 小时旧记录由 compat 写入及 console 每分钟维护清理，即使没有新请求。
  正常停服删除自身心跳，控制台不会把停服显示成健康零任务。
- compose 沿用现有 conversation-monitor 共享卷，用单独 `live-requests.db`，与原文采集策略独立。
  Dockerfile 同时打包共享模块。数据库不可写不会抛错阻断推理；console 读取失败返回不可用状态。
- `/console/api/requests/live` 与 `/requests/backends` 均仅管理员 GET，响应 no-store。Key 别名/分组从
  既有 Key 清单关联；不把请求给出的 Key 当作已鉴权成功，未匹配时明确标注。
- 模型 endpoint 仅取匹配已登记站点且通过 literal-IP/path 校验的 deployment；直接采集 `/metrics`，同地址模型别名合并，TensorFold 同义 gauge
  选一个指标族，缺失/非有限值不作为零。这里不使用可能滞后的 VM 样本替代实时结果。
- 页面每 5 秒刷新；可暂停、手动刷新、按模型/Key/ID/来源搜索并按响应阶段筛选。

覆盖限于新版本 compat 经手的新请求。内部服务直连 LiteLLM、绕过 Portal 的调用与启用前的在途请求
只有可能包含在后端汇总数里；无法反推其逐条归属。当前引擎没有已验证的逐请求排队 API，
所以实际部署、队列顺序、ETA 和每条请求在后端的排队/运行状态均不猜测。
开始发送响应头仍保持等待响应，直到收到非空响应 body；body 也可能只是 SSE prelude，
故阶段名称始终为「接收响应」，不声称已经开始生成 token。

## 必需功能验证与证据

```bash
python -m pytest -q console/test_queue_api.py compat/test_queue_lifecycle.py \
  compat/test_compat.py test_conversation_monitor_compat_contract.py
cd console/e2e
PYTHON=/path/to/python PLAYWRIGHT_EXECUTABLE_PATH=/path/to/chromium npm run queue
```

首轮修正了两处测试夹具：登录桩只允许 master 访问 global/spend；401 夹具使用尚未消费的原始流。
修正后 42 项针对性检查通过。真实 Chromium 本地 HTTP 链路验收通过：12 等待 → 12 接收 → 0 活动；
管理员入口导航、搜索、阶段筛选、重复后端别名、刷新失败、过期、深浅色与 780px 窗口均有断言。
截图与 `result.json` 位于本地 `output/playwright/queue/`，未把截图当作生产环境验收。

边界审查发现并修复三项：写库耦合请求路径、指标目标未限制到登记站点、正常停服短暂健康零任务。
增量复审清除原 findings；增加阻塞存储时 12 请求首段响应、非法 Unicode、地址拒绝、停服与
无新 publisher 时过期清理回归。48 项针对性检查及新增站点登记失败回归通过（合计 49 项），Compose 配置与修改 Python 语法检查通过。
使用 Python 3.13 / Starlette 1.10.0 / httpx 0.28.1 / pytest 9.2.0，浏览器为本机 Chrome。
浏览器窄窗口复验发现 sticky 顶栏遮挡自动滚动到的刷新按钮，已为该页控件设置滚动安全距离。
最终 Chromium 本地 HTTP 链路复验 PASS，包含窄窗口刷新与全部上述交互；完整集成测试与发版由主 agent 在任务 PR 合并后负责，
既有 CI/分支保护不绕过。上线前的旧等待请求无法恢复逐条归属；现场部署身份和验收另记于 PR 部署回执。

本次查看阶段 playback：US-Q1 以上 Given/When/Then 由针对性 API/lifecycle 检查与真实浏览器流覆盖，
Complete，drift 0，ALIGNED（限代码评审交付及受控本地链路）。具体后端逐请求排队归属依然未知；
本阶段不承诺引擎未提供的排队顺序、实际 deployment 或 ETA，不包含 US-Q2 调度。nasubuntu 验收见后续部署回执。

## nasubuntu 候选部署授权与依赖门禁

用户后续授权「部署到 nasubuntu，我评审没有问题就可以合并了」：允许部署待合并候选供实地评审，
合并继续等待用户评审通过。本授权更新交付环境，不扩大到调度功能或合并授权。

为解除依赖审计门禁，将 console 与 mcp-hub 的 FastMCP 锁定统一升级至同一主版本的 3.4.10，
开发依赖下限同步。此版本修复 [GHSA-7xgj-8392-5q4r](https://github.com/advisories/GHSA-7xgj-8392-5q4r)。
升级后已执行默认完整 pytest（254 项）与本地真实浏览器活动任务链路，均通过；仍须等待新提交 CI。
现场只更新本功能所需 console/compat，保留部署本地 env/Compose override/模型路由与原 MCP 服务运行状态；
不把源码依赖版本同步当作其他服务已升级的证据。生产替换前保存镜像、源码与入口配置回退点，
验证候选源码/镜像身份；部署后检查管理员真实查看流程及匿名/普通 Key 拒绝路径。

2026-11-12 23:19（Asia/Singapore）已在 nasubuntu 候选 d3ce32d 通过真实 HTTPS 浏览器验收：
管理员活动任务页与构建版本一致，匿名 401 / 普通 Key 403；一条 64-token 的真实 qwen 流式请求
`live_8fbee3199f5c4d778b471eae1799d957` 出现在列表中并在结束后移除，临时 Key 已删除。
实际数据暴露了长模型别名列表挤压计数列的布局问题；已修正单元格换行并扩充浏览器长别名回归。
最终候选身份及复核结果随 PR 部署回执保留；以上真实运行证据没有使用模拟后端。

## 节点计数直达入口（用户评审补充）

用户补充「能不能在活动/排队这里对着数字直接就可以点击进去」，作为 US-Q1 查看故事的入口细化，
继续引用 `190-view-v1`，不扩大到调度或确认后端逐请求归属。

- Given 节点性能页有可用的活动 / 排队数字，When 点击数字或以键盘 Enter 打开，Then 直接进入
  活动任务页，保留节点；活动入口展示全部响应阶段，排队入口选中「等待响应」。0 同样可进入，
  未采集的 — 不伪造为可用计数。
- 目标页仅保留该节点的后端汇总，请求按该节点关联模型筛选，明确说明实际执行节点与排队状态
  未确认、同模型多节点可能同时出现；顶部卡片注明为 Portal 全局统计。阶段筛选写回 URL，支持
  重新加载、浏览器返回和「查看全部节点」清除上下文；关联信息不可用时不显示健康空列表。
- 针对性验证：`cd console/e2e && PYTHON=/path/to/python PLAYWRIGHT_EXECUTABLE_PATH=/path/to/chromium npm run queue`。
  Chromium 本地真实 console/compat 链路通过12等待→12接收→0活动，新增入口键盘、活动/排队、
  节点与阶段保留、重新加载、0/未知、清除筛选断言。节点 overview/history 使用受控夹具，目标页
  请求和后端 API 使用真实本地链路。证据位于 `output/playwright/queue/`。

本次代码入口细化 playback：US-Q1 Complete，drift 0，ALIGNED（本地功能验证）；待当前提交 CI 与
nasubuntu 同版本点击入口验证后更新部署回执。合并仍等待用户评审通过。
