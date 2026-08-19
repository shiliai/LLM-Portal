# 贡献指南（Contributing Guide）

感谢参与 LLM-portal！本文档说明开发流程、代码风格与 PR 要求。项目定位与架构见根
[`README.md`](README.md)，部署与运维见 [`docs/runbook.md`](docs/runbook.md)，公开方向见
[`ROADMAP.md`](ROADMAP.md)。

## 开发流程

### Issue 与实现

- 新功能先开 issue，说明问题、期望行为、替代方案和安全影响；路线图条目不等于已承诺排期。
- Bug 报告应包含可复现步骤、当前 commit、部署模式和经过脱敏的日志。
- 获得方向确认后再提交实现 PR；一次 PR 聚焦一个可验证结果，避免混入无关重构。
- 用户可见行为、部署方式或安全边界变化时，同步更新 README、runbook 或 `ROADMAP.md`。

### 分支与提交

- 默认分支 `main`，所有改动经 PR 合入；
- 分支命名建议：`feat/<主题>`、`fix/<主题>`、`chore/<主题>`、`docs/<主题>`；
- 提交信息建议用 Conventional Commits 风格（`feat: …` / `fix: …` / `docs: …`），中文描述即可。

## 代码风格

- **中文注释惯例**：代码注释、文档、commit/PR 描述均使用中文；注释解释「为什么」而非复述代码。复杂决策（如安全取舍、协议兼容 hack、部署踩坑）请在注释中保留背景，这是本项目的核心资产之一。
- **Python**：标准库优先，依赖从简；服务代码为单文件 ASGI（starlette/uvicorn）风格，新增第三方依赖须写入对应服务的 `requirements.txt` 并说明理由。
- **测试**：pytest（测试文件与被测服务同目录或按现有测试布局放置）。提交前在仓库根目录运行 `make test`（见下）确保全部通过。
- **部署物**：改 `vps/` 下 compose / nginx / deploy.sh 时，保持「幂等可重复跑」性质，且不破坏 `deploy.sh` 步骤 7b 的公网收敛自检。

## 测试入口

根目录 Makefile 提供统一测试入口：

```bash
make test    # 根目录 pytest：自动发现各服务与部署工具测试
```

也可直接 `python -m pytest`。CI（`.github/workflows/ci.yml`）在 push / PR 到 `main` 时运行同样的测试，外加语法编译、compose 配置校验、密钥扫描与依赖审计。

## PR 要求

提交 PR 前逐项确认（模板见 [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)）：

1. **过 CI**：全部 job（unit-tests / py-syntax / compose-validate / secrets-scan / deps-audit）绿。
2. **不含真实凭据**——这是硬性红线：
   - **禁止提交任何真实密钥**：API Key（`sk-…`）、master key、postgres 密码、TOTP 密钥、WireGuard 私钥、token，无论出现在代码、注释、测试夹具还是文档里；
   - **禁止提交真实内网拓扑**：内部域名、内网 IP、服务器主机名、公司/个人可识别信息。示例一律用 `example.com`、`192.0.2.0/24`（TEST-NET）、`REPLACE_ME` 占位（参考 `vps/.env.example` 的写法）；
   - CI 的 gitleaks 全历史扫描会兜底，但请勿依赖它——泄露一旦进历史，清理成本极高；
   - 不慎提交过密钥的分支：不要 force-push 掩盖，立刻联系维护者轮换凭据。
3. 附带测试：bug 修复带回归测试，新功能带基本覆盖；
4. 中文描述改动动机与取舍，安全相关改动（权限 / 暴露面 / 凭据）请在 PR 里单独说明。

## 报告安全问题

安全漏洞**不要**开 issue / PR，走 [`SECURITY.md`](SECURITY.md) 的私有报告流程。
