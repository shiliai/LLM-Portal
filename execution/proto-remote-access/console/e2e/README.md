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
npm init -y && npm install playwright && npx playwright install chromium
python3 -m venv venv && venv/bin/pip install starlette uvicorn httpx segno

# 导出生产形状数据（VPS 上）→ 拷到本目录 keylist.json
ssh vps-tencent-tokyo 'cd ~/private-llm-src/vps && set -a; . ./.env; set +a; \
  curl -s "http://127.0.0.1:4000/key/list?return_full_object=true" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"' > keylist.json

# 起桩与被测服务（注意本机 4000 可能被占，桩用 4100）
python3 mocklitellm.py &                      # 127.0.0.1:4100
env CONSOLE_PORT=8399 CONSOLE_DATA=/tmp/cdata LITELLM_BASE=http://127.0.0.1:4100 \
    LITELLM_MASTER_KEY=sk-test-master ONBOARD_ADMIN_TOKEN=tok \
    ADMIN_EMAIL=admin@test.local ADMIN_PASSWORD=test-pass-1 \
    ../venv/bin/python ../console.py &        # venv 指向 console 依赖

node keys-e2e.js                              # 断言输出 + 截图 r*.png
```

## 已知桩缺口（断言时留意）

`/key/generate` 只回 `{"ok":true}`（无 key 字段）、`/key/block` 不改状态——
涉及明文生成/禁用态的断言以「请求发出 + UI 不报错」为准，真语义回归在 VPS 冒烟。
