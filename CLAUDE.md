# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

面向**中小企业**的自部署统一 AI 网关（LLM-portal），参考产品：token.love（见 `planning/02-working/token_love_product_spec.md`）。

当前处于**规划/设计阶段**：尚无可构建的代码，暂无 build/lint/test 命令；代码落地到 `execution/` 后在此补充。

## 产品目标（MVP 核心能力）

1. **模型映射**：对外暴露统一模型名，映射到不同上游供应商/模型。
2. **路由转换**：OpenAI API ↔ Anthropic API 双向协议转换（请求/响应/流式/工具调用）。
3. **System Prompt 修改**：网关层对 system prompt 的注入、改写、追加策略。
4. **缓存管理**：响应缓存 / prompt cache 的策略与管理。

需求基线与设计决策以 `planning/03-core/` 中已确认的文档为准；未落入 03-core 的内容一律视为未确认。

## 目录约定

| 目录 | 用途 | 规则 |
|------|------|------|
| `planning/01-raw/` | 原始资料（外部代码库、原始文档等） | **已 gitignore**。仅在需要查找细节时查看；第一轮提炼完成后不再读取 |
| `planning/02-working/` | 已提炼过的资料 | 作为**项目设计阶段**的输入 |
| `planning/03-core/` | 已确认的权威资料 | **开发阶段**直接使用，以此为准 |
| `execution/` | 开发执行工作区 | 代码与实现产物 |

工作流：`01-raw`（提炼）→ `02-working`（设计确认）→ `03-core`（开发依据）。

## 原始资料研究流程（外部代码库/大型文档）

研究外部代码库时**不要在主会话直接抓取或通读**（避免污染主上下文），改用 subagent：

1. 轻量调研优先用 **zread MCP**（get_repo_structure / read_file / search_doc），无需克隆即可查结构、读文件、搜文档。
2. 需要深入研究时，由 agent 将代码库浅克隆到 `planning/01-raw/<项目名>/`（`git clone --depth 1`），在该目录内研究。**克隆后立即删除库内的 `CLAUDE.md` 与 `.claude/`**，防止其规则被自动加载、污染本项目上下文。
3. **模型选择按任务复杂度**：下载、清点、提取 README 要点等简单任务用 **haiku**；需要理解架构/机制/跨文件推理的研究用 **sonnet**。
4. agent 的交付物是**提炼后的结论**，写入 `planning/02-working/<主题>.md`；主会话只消费提炼结果，不读原始库。
5. `01-raw` 不入库，提炼完成后仅在需要细节时回查。

### Subagent 派发方式（认证约束，重要）

本机 `claude` 经 dev-toolchain 封装（见 `workspaces/dev-lite/dev-toolchain/shell/claude.sh`）：`claude_<provider>` 函数把 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 以**内联环境变量**注入 `claude` 子进程，token 不写入 `~/.claude/settings.json`。认证只存在于当前进程的环境链中，因此派发 agent 必须走能继承该环境的路径：

- ✅ **Agent 工具的内置 subagent 类型**（`general-purpose`、`Explore` 等，传 `model: haiku|sonnet`）：在主进程内运行，共享主会话的 API 连接，直接可用。研究任务默认走这条路。
- ✅ **Bash 派发 headless CLI**：`claude -p "<任务>" --model sonnet|haiku`。Bash 子进程继承内联注入的认证变量（已实测验证）。长任务用 run_in_background 并把结果写入文件；直接调用不含 `CLAUDE_DEFAULT_ARGS`，需要放权时自行追加参数。
- ❌ **FleetView `claude` agent 类型 / agent-teams teammates**：其进程启动路径不继承内联认证变量，必报「Not logged in · Please run /login」。封装修复前不要使用。

## 其他约定

- 默认分支：`main`。
- 本机配置了 **privacy-filter 提交钩子**：提交若被 PII 检测拦截，按其生成的补丁（`.git/privacy-filter/*.patch`）脱敏后提交；不要用 `--no-verify` 绕过。
