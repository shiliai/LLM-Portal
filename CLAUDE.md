# CLAUDE.md / AGENTS 约定

本文件为 AI 编码助手（Claude Code 等）在本仓库工作时的指引。

## 项目定位

面向**中小企业**的自部署统一 AI 网关（LLM-portal）：公网 VPS 入口 + WireGuard 多站点隧道，
让散布在多个内网的私有推理模型以 OpenAI / Anthropic 兼容 API 统一对外，并提供虚拟 Key、
分组路由、用量计量与管理控制台。参考产品：token.love（见 `planning/02-working/`）。

当前状态：proto-remote-access MVP 已上线运行（见 `execution/proto-remote-access/README.md`），
需求基线以 `planning/03-core/user_story_baseline_r4.md` 为准。

## 常用命令

| 命令 | 用途 |
|------|------|
| `make test` | 全部 pytest 单测（compat + console + onboardd + mcp-hub） |
| `make lint` | 全部入库 .py 的 py_compile 语法检查 |
| `make compose-validate` | vps/docker-compose.yml 配置校验（不启动容器） |
| `make test-e2e` | console Playwright E2E 运行说明（需真实浏览器，不进 CI） |

新环境：`python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`，
然后 `make test PYTHON=.venv/bin/python`。

## 目录约定

| 目录 | 用途 | 规则 |
|------|------|------|
| `planning/01-raw/` | 原始资料（外部代码库等） | **已 gitignore**，不入库 |
| `planning/02-working/` | 已提炼的资料 | 作为设计阶段的输入 |
| `planning/03-core/` | 已确认的权威资料 | 开发阶段直接使用，以此为准 |
| `execution/` | 开发执行产物 | 代码与实现（当前主体在 `execution/proto-remote-access/`） |
| `docs/superpowers/` | 设计稿与高保真原型 | 设计依据 |

工作流：`01-raw`（提炼）→ `02-working`（设计确认）→ `03-core`（开发依据）。
未落入 `03-core/` 的内容一律视为未确认；基线只通过「升版」演进。

## 原始资料研究流程（外部代码库/大型文档）

研究外部代码库时不要在主会话直接通读（避免污染上下文），改用 subagent：
浅克隆到 `planning/01-raw/<项目名>/`（`git clone --depth 1`，不入库），交付物为提炼后的
结论并写入 `planning/02-working/<主题>.md`；主会话只消费提炼结果。

## 其他约定

- 默认分支：`main`；提交信息遵循 Conventional Commits（中文描述可）。
- **红线：任何真实密钥、真实域名/IP、内网拓扑不得入库**——示例一律用 `example.com`、
  `192.0.2.x`（TEST-NET）、`REPLACE_ME` 占位（见 CONTRIBUTING.md）。
- 代码注释用中文，解释「为什么」并保留取舍背景（本仓库的重要决策大量记录在注释里）。
