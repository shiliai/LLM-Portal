<!-- PR 模板：三节必填（改动摘要 / 测试 / 安全自查）。提交前请读 CONTRIBUTING.md。 -->

## 改动摘要

<!-- 本 PR 做了什么、为什么；涉及取舍的说明理由（背景决策请留在代码注释里）。 -->

-

## 测试

<!-- 如何验证；CI 必须全绿（unit-tests / py-syntax / compose-validate / secrets-scan / deps-audit）。 -->

- [ ] `make test`（根目录 pytest）通过
- [ ] CI 全部 job 通过
- [ ] 手动验证（描述步骤与结果；bug 修复附回归测试）

<!-- 可选：涉及的验收项（若对应 runbook T1~T13 / 需求基线条目，请注明） -->

## 安全自查

<!-- 硬性红线：禁止提交任何真实密钥与内网拓扑。 -->

- [ ] 不含真实密钥 / 凭据（API Key、master key、密码、TOTP、WG 私钥、token；一律 `REPLACE_ME` 占位）
- [ ] 不含真实内网信息（内部域名 / IP / 主机名；示例用 `example.com`、`192.0.2.x`）
- [ ] 未改变安全边界；若改变了（权限 / 公网暴露面 / 凭据流向），已在改动摘要说明并知会维护者
- [ ] compose / nginx 改动不破坏 deploy.sh 步骤 7b 公网收敛自检（未带 Key 的管理端点仍 404）
