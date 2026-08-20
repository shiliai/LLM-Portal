# consoled E2E 测试环境（Playwright + mock LiteLLM）

真实浏览器跑通控制台前端全链路，不依赖 VPS / 真 LiteLLM。2026-08-15 因
「用户浏览器启发式缓存旧页面」问题搭建（commit 2bd834f），此后页面改动都应先过这里。

## 组成

- `mocklitellm.py`：本地 LiteLLM 桩。`/key/list` 回放 `keylist.json`（从生产
  `curl 127.0.0.1:4000/key/list?return_full_object=true` 导出的真实形状数据，
  含密钥哈希——**该文件不入库**，需在 VPS 上重新导出）；`/key/update` 改内存态。
- `keys-e2e.js`：用户 Key 页全交互脚本（登录 → 列表 → 行内分组 → 搜索粘完整密钥
  → reload → 使用弹窗 → 编辑/禁用/新建全链路），收集 console/pageerror。

## 运行

```bash
# 一次性准备（本目录）
# playwright 版本已固定在 package.json；浏览器二进制装到 ~/.cache/ms-playwright
npm install && npx playwright install chromium
python3 -m venv .venv && .venv/bin/pip install starlette uvicorn httpx segno cryptography

# 导出生产形状数据（VPS 上）→ 拷到本目录 keylist.json
# （缺省可用 mocklitellm.py 内置合成夹具，可跳过本步先跑起来）
ssh your-vps 'cd ~/LLM-Portal/vps && set -a; . ./.env; set +a; \
  curl -s "http://127.0.0.1:4000/key/list?return_full_object=true" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"' > keylist.json

# 起桩与被测服务（注意本机 4000 可能被占，桩用 4100）
python3 mocklitellm.py &                      # 127.0.0.1:4100
env CONSOLE_PORT=8399 CONSOLE_DATA=/tmp/cdata LITELLM_BASE=http://127.0.0.1:4100 \
    LITELLM_MASTER_KEY=sk-test-master ONBOARD_ADMIN_TOKEN=tok \
    ADMIN_EMAIL=admin@test.local ADMIN_PASSWORD=test-pass-1 \
    .venv/bin/python ../console.py &          # .venv 指 console 依赖

node keys-e2e.js                              # 断言输出 + 截图 r*.png（= npm run keys）
```

其余脚本同理：`npm run keys-memory` / `keys-vault` / `usage-redesign` / `usage-reqlog`。

## MCP 管理 e2e（`npm run mcp`）

使用同一套 mock LiteLLM 和 consoled 启动命令。脚本拦截 MCP 管理 API，因此无需真实
外部 MCP：它验证注册、分组保存和移除都使用页面 `pf-modal` 而不是浏览器确认框，并确认
注册预检失败后表单和值仍保留。

## 站点模型管理 e2e（`npm run sites-models`）

比其余脚本多两个前提：

```bash
MOCK_PORT=8004 python3 mocklitellm.py &    # 假「站点上游」：console 的 /sites/probe
                                           # 是服务端 httpx 真发起的,回环才可达
env ... ONBOARDD_URL=http://127.0.0.1:4100 .venv/bin/python ../console.py &
                                           # onboardd 也指到 mock(夹具: workstation 站点)
```

覆盖：模型弹窗打开（回归:smReload 曾定义在 pfReady.then 内,点击 ReferenceError）、
刷新上游（多 id 下拉→应用→列表更新）、添加（独立弹窗,选端口即自动探测→radio
点选回填对外名）、两段式删除；全程断言无 pageerror。mock 夹具 wg_ip=127.0.0.1、
DEP_SEQ 从 1 起（撞 id 会把多个 deployment 一起删,踩过）。

`sites-models-e2e.js` 支持 `PW_CHANNEL=chrome node sites-models-e2e.js` 复用本机
已装 Chrome，免去 `npx playwright install chromium` 的大体积下载。

## 已知桩缺口（断言时留意）

`/key/generate` 只回 `{"ok":true}`（无 key 字段）、`/key/block` 不改状态——
涉及明文生成/禁用态的断言以「请求发出 + UI 不报错」为准，真语义回归在 VPS 冒烟。
