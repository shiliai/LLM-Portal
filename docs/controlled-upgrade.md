# 受控升级与单 deployment 映射维护

本文面向已有分发目录的升级；该目录可以没有 `.git`。真实地址、Key、管理员凭据、
数据库备份和现场 receipt 一律保存在源码目录之外的受控私有目录，不进入公开仓库。
本文中的地址仅为 TEST-NET 示例。工具需要 Python 3.12+、`rsync`；不自动安装依赖。

## 边界与授权

`vps/controlled_release.py` 仅处理源码检查、恢复点、源码发布和源码恢复，不调用 Docker、
不修改模型/Key、不执行 `deploy.sh`、不恢复数据库，也不自动重启服务。默认 `check`
不创建目录或输出现场配置。三个写阶段都要求同时提供 `--apply` 与
`--ack SOURCE_WRITE_AUTHORIZED`；这只确认执行者已有任务授权，不能替代变更日志、
互斥登记、影响范围审批和可执行恢复方案。

`vps/model_mapping_plan.py` 仅离线生成单字段 PATCH 计划并验证前后快照，不访问网络。
默认只检查；输出私有计划时另需 `--apply --ack MAPPING_PLAN_WRITE_AUTHORIZED`。
两个工具的 stdout/stderr 仅返回状态与计数，解析失败不回显可能带凭据的输入。

## 先建立恢复条件

执行者须先冻结同一部署目录的并发发布、配置编辑和模型管理写入，记录当前文件摘要、
全部容器的实际镜像 ID/digest、端口、挂载、edge 模式和健康结果。不根据旧报告推断现场。
保持现有 PostgreSQL、LiteLLM、WireGuard 及非目标容器；本流程不是中间件版本迁移。

必须由执行者准备并实际验证两个独立、非空、仅所有者可访问的恢复文件：

- 数据库恢复文件：包含 LiteLLM 动态模型、Key、用量等数据；确认格式、对象和恢复命令。
  `pg_restore --list` 只证明备份可解析，不等于隔离环境恢复验收已通过。
- 持久状态恢复文件：覆盖 `/var/lib/private-llm`、`/etc/private-llm`、WireGuard 配置与
  实际 edge 目录；在线 SQLite 必须使用一致性备份机制，不能仅复制活跃 `.db` 而忽略 WAL。
  重点包括 sessions、keyvault 数据库及其密钥、onboardd、MCP registry/vision 配置、
  conversation-monitor。外部 nginx 的配置与证书按实际 edge 模式另建恢复点。

工具只核验这些文件的存在、私有权限、非空和 SHA256，**不能证明业务一致性或可恢复性**。
`--recovery-verified` 是执行者对上述验收的显式声明。恢复时不得未经裁决覆盖恢复点之后
新增的业务数据；源码恢复和数据恢复是两个独立决策。

## 检查、备份、源码发布

先按 [runbook §2.1](runbook.md#21-生产升级与回滚commit-包) 使用
`release-package.sh` 从明确的已合并 commit 生成四件套：tar、tar SHA256、文件清单、commit。
工具拒绝缺少部署文件、摘要不一致、重复清单、路径越界、链接或特殊文件的发布包。

所有命令中的 `TOOL` 指向已独立审核的辅助器，不依赖即将被覆盖的目标源码。

```bash
TOOL=/reviewed-tools/controlled_release.py
ROOT=$HOME/LLM-Portal
RELEASE_DIR=$HOME/private-llm-release
MERGE_SHA=<full-merged-commit-sha>
RECOVERY=$HOME/private-llm-recovery-<unique-stamp>

# 任务专属 TMPDIR 必须已存在；先核验实际挂载与可用空间，再传给所有子进程。
export TMPDIR=$(mktemp -d "$HOME/portal-upgrade.XXXXXX")
df -h "$TMPDIR"

python3 "$TOOL" --root "$ROOT" --release-dir "$RELEASE_DIR" --commit "$MERGE_SHA"

# 使用已验收的恢复文件；RECOVERY 必须尚不存在且位于 ROOT 之外。
python3 "$TOOL" backup --root "$ROOT" --release-dir "$RELEASE_DIR" --commit "$MERGE_SHA" \
  --receipt "$RECOVERY" --database-backup /private-backups/database.dump \
  --state-backup /private-backups/persistent-state.tar --recovery-verified \
  --apply --ack SOURCE_WRITE_AUTHORIZED

python3 "$TOOL" stage --root "$ROOT" --release-dir "$RELEASE_DIR" --commit "$MERGE_SHA" \
  --receipt "$RECOVERY" --apply --ack SOURCE_WRITE_AUTHORIZED
```

自动保护 `vps/.env`、其他已有 `.env*`（不含 `.env.example`）与四个标准 compose override
文件名，包括其原先不存在的状态；已有文件的字节、mode、uid/gid 必须保持不变。
`COMPOSE_FILE` 或命令行引用的非标准 override 必须在每次命令中显式重复
`--protect vps/custom.yml`。工具不解析/执行 `.env`，也不自动生成新随机配置。
`vps/.env` 必须已经是普通 `0600` 文件；符号链接部署目录结构不在支持范围。

`backup` 保存 `source.tar`、源码逐文件摘要和 `receipt.json`，备份与 receipt 为 `0600`，
目录为 `0700`，`.git` 不进入源码恢复包。`stage` 再次核验发布包、恢复文件与配置守恒，
使用 `rsync --delete` 发布源码；不能把现场未跟踪脚本当作长期配置，必要文件须显式保护。
只有发布摘要复核完成才写 `stage-complete.json`。失败时保留恢复点与暂存目录，
不会自动重试、恢复或重启；完成标记存在时拒绝重复执行同一写阶段。

## 审批后才执行运行时变更

源码 stage 不是部署完成。协调者须单独审核 compose 合并结果、镜像及挂载，明确本轮只重建
哪些 Portal 服务，再执行一次获准操作。不得通过全栈反复 restart 掩盖首次失败。
上游容器地址变化后须验证 nginx 的解析并按 runbook reload；不能为了 ChatBI 暴露
回环 LiteLLM 管理端口，业务客户端使用已批准的 `/v1` 入口与专属受限 Key。

当前 `deploy.sh` 不是单服务发布器，完整执行会涉及：

| 类别 | 实际动作和回滚约束 |
| --- | --- |
| 宿主与网络 | 可能停用旧 `console/mcp-hub/onboardd` systemd 和 `wg-quick`；引导 WireGuard 密钥/配置；创建 shared 网络；必须先核验归属与影响范围 |
| 持久状态 | 新建 conversation-monitor 目录，尝试 chown，修正 `monitor.db` 为 uid 10001、mode 0600；新库默认采集策略 off，但仍需权限和保留策略验收 |
| 容器 | 对项目执行完整 `docker compose up -d --build`，并非仅 console/compat；可能包含监控容器，须先审查端口占用与实际 override |
| PostgreSQL | `LiteLLM_SpendLogs` 增量添加 `portal_cached_tokens` STORED 生成列；不存在时可能锁表/重写，应评估表体积；收敛 SELECT-only `console_usage` 角色、密码与授权 |
| Edge | 按 standalone/external/offload 渲染配置、签证或 reload；external 保留 bind-mount inode；脚本内失败回滚只覆盖特定 nginx 修改，不是全栈事务 |
| 验收 | 核心容器、健康和管理面收敛检查；这些不证明真实模型推理或业务客户端成功 |

回退源码不会自动删除生成列/角色，也不会回退监控 SQLite schema。旧服务是否兼容增量
结构必须先验收；不得未经授权 DROP 列/角色或直接恢复整个数据库。降级到 issue #51 之前
仍须按 runbook 先隔离受限 MCP registry，不能绕过该安全步骤。

## 源码恢复

```bash
python3 "$TOOL" restore-source --root "$ROOT" --receipt "$RECOVERY" \
  --apply --ack SOURCE_WRITE_AUTHORIZED
```

工具验证恢复包、数据库/状态恢复文件及当前受保护配置后恢复源码，并比较原始文件清单。
它不会覆盖 `.env/override`、不会重启容器或恢复持久数据。失败后先完成受控源码恢复，
再由协调者执行获准的旧服务恢复动作并重新验收；镜像 ID 必须来自升级前的真实记录。
确认无进程引用之前，不清理暂存目录和验收数据。

## 单 deployment endpoint 维护

从受控回环管理入口保存更新前完整 `GET /model/info` 快照为 `0600`；不要使用只含模型名
的 `/v1/models` 或 Console 精简视图代替。明文 Key/master 由协调者经受控配置读取，
不进入命令参数、计划文件、日志或聊天。

```bash
python3 /reviewed-tools/model_mapping_plan.py --before /private-data/before.json \
  --deployment-id <exact-existing-id> --model demo-model \
  --old-api-base http://192.0.2.1/v1 --new-api-base http://192.0.2.2/v1

# 显式写出 0600 离线计划，不访问 API。
python3 /reviewed-tools/model_mapping_plan.py --before /private-data/before.json \
  --deployment-id <exact-existing-id> --model demo-model \
  --old-api-base http://192.0.2.1/v1 --new-api-base http://192.0.2.2/v1 \
  --plan /private-data/endpoint-plan.json --apply --ack MAPPING_PLAN_WRITE_AUTHORIZED
```

协调者先实时核验 LiteLLM 的 `PATCH /model/{model_id}/update` 支持部分参数合并，
再仅发送计划的 `body={"litellm_params":{"api_base":...}}`；禁止 delete/new 回退方案，
也不能从掩码快照重建 `api_key`。失败立即停止，保留快照与 HTTP 证据，不自动换接口。
计划保存原 endpoint，回滚使用相同 ID 的单字段 PATCH，执行前再次核验该 ID 归属。

```bash
python3 /reviewed-tools/model_mapping_plan.py verify --before /private-data/before.json \
  --after /private-data/after.json --plan /private-data/endpoint-plan.json
```

验证器按 ID 比较**全部 deployment 集合及字段**，只允许选定 endpoint 变化；仅忽略每个
deployment 顶层和 `model_info` 下的 `updated_at/updated_by`。其他非确定字段变化也会拒绝，
此时应先审查原因，不能扩大忽略列表来掩盖变更。Key 清单/权限和真实推理另行验收。

上下文预算不得从模型名猜测：以实际运行引擎的 serving context（例如 `/props` 的
`default_generation_settings.n_ctx`）和部署参数实测为依据，同时核验 LiteLLM
`model_info` 中现存的 `max_input_tokens/max_output_tokens/max_tokens` 语义。
运行上下文、最大生成 token、客户端输出上限是不同约束，必须留输入/模板/输出余量。
当前 Console 的模型重建/别名路径不保证保留这些额外字段；本次只更新 endpoint，
不碰预算、模型名、tags、rpm/tpm、timeout、模型总数或其他 Key。

## 交付证据

私有回执应包含恢复文件 SHA256、源码/镜像基线、授权与单次实际命令、退出码、配置守恒、
模型全量前后验证、非目标服务未变化，以及受限 Key 的正常推理和越权负例。
ChatBI 验收必须通过既有业务入口；源数据只读。健康检查、路由成功、真实生成与业务验收
分别记录，不相互替代。本公开文档和测试不得填入现场地址、主机名、客户标识或凭据。
