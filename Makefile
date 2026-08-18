# 统一开发入口（issue #10）。用法：仓库根目录执行 `make <target>`。
#
# PYTHON：单测解释器。有本地 venv（console/e2e/.venv，含全部服务依赖）则优先用，
# 否则回退系统 python3——新环境先 `python3 -m venv .venv &&
# .venv/bin/pip install -r requirements-dev.txt` 再 `make test PYTHON=.venv/bin/python`。

PYTHON ?= $(shell test -x console/e2e/.venv/bin/python \
	&& echo console/e2e/.venv/bin/python || echo python3)

.PHONY: test test-unit test-e2e compose-validate lint

## test：跑全部 pytest 单测（compat + console + onboardd + mcp-hub）
test: test-unit

## test-unit：同 test；显式名单见 pytest.ini 的 testpaths
test-unit:
	$(PYTHON) -m pytest

## test-e2e：console 浏览器端到端（Playwright + mock LiteLLM，需真实浏览器，不进 CI）
test-e2e:
	@echo "== console E2E（真实浏览器，先装依赖：见 console/e2e/README.md）=="
	@echo "  cd console/e2e"
	@echo "  npm install && npx playwright install chromium   # 一次性"
	@echo "  python3 mocklitellm.py &                         # 127.0.0.1:4100"
	@echo "  env CONSOLE_PORT=8399 CONSOLE_DATA=/tmp/cdata LITELLM_BASE=http://127.0.0.1:4100 \\"
	@echo "      LITELLM_MASTER_KEY=sk-test-master ONBOARD_ADMIN_TOKEN=tok \\"
	@echo "      ADMIN_EMAIL=admin@test.local ADMIN_PASSWORD=test-pass-1 \\"
	@echo "      .venv/bin/python ../console.py &"
	@echo "  node keys-e2e.js                                  # 其余 *-e2e.js 同理"

## compose-validate：校验 vps/docker-compose.yml 语法与变量引用（不启动容器）
compose-validate:
	cd vps && docker compose --env-file .env.example config --quiet
	@echo "compose config OK"

## lint：仓库内全部 .py 语法编译检查（py_compile）。
## 范围 = git 追踪 + 未忽略的新文件（本地 planning、.venv、工具缓存等均被排除）
lint:
	@set -e; \
	files=$$(git ls-files --cached --others --exclude-standard -- '*.py'); \
	count=$$(echo "$$files" | grep -c . || true); \
	echo "py_compile $$count files"; \
	echo "$$files" | xargs $(PYTHON) -m py_compile
