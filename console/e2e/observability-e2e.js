/* 总览 + 节点性能 E2E（issue #106）：综合仪表盘/筛选/自动刷新/节点详情块/缺失指标 — */
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'test-pass-1';

const SITES = [
  { name: 'gb10', transport: 'wireguard', wg_ip: '10.77.0.11', address: null, handshake: 5,
    deployments: 2, status: 'online',
    metrics: { runtime: 'vllm', output_tok_s: 86.4, input_tok_s: 520, requests_running: 5,
      requests_waiting: 1, kv_cache_pct: 71, cache_hit_pct: 63.2, gpu_util_pct: 88,
      gpu_temp_c: 72, power_w: 410 } },
  { name: 'dell-shili-7960', transport: 'wireguard', wg_ip: '10.77.0.14', address: null, handshake: 8,
    deployments: 1, status: 'online',
    metrics: { runtime: 'llamacpp', output_tok_s: 45.1, input_tok_s: 210, requests_running: 3,
      requests_waiting: 0, cache_hit_pct: 58.4, spec_accept_pct: 62.1, gpu_util_pct: 82,
      gpu_temp_c: 64, power_w: 285 } },
  { name: 'm2s2NasUbuntuVM-shili-dev', transport: 'wireguard', wg_ip: '10.77.0.13', address: null,
    handshake: 12, deployments: 1, status: 'online',
    metrics: { runtime: 'llamacpp', output_tok_s: 22.0, input_tok_s: 95, requests_running: 1,
      requests_waiting: 0, gpu_util_pct: 64 } }
];
const OVERVIEW = {
  version: 'test', build: 'e2ebuild',
  totals: { requests: 8420, prompt_tokens: 38214600, completion_tokens: 9642100,
    cached_tokens: 21483700, errors: 96 },
  sites: { online: 3, total: 3, rows: SITES },
  deployments: { healthy: 4, total: 4, rows: [
    { model: 'deepseek-v3.1', api_base: 'http://10.77.0.11:8890/v1', rpm: null, tpm: null, state: '健康', requests: 100, failures: 0 },
    { model: 'qwen3-32b-instruct', api_base: 'http://10.77.0.14:8005/v1', rpm: null, tpm: null, state: '健康', requests: 60, failures: 0 },
    { model: 'glm-4.5-air', api_base: 'http://10.77.0.13:8006/v1', rpm: null, tpm: null, state: '健康', requests: 40, failures: 0 }
  ]},
  recent_errors: []
};
const USAGE = { totals: { requests: 120, prompt_tokens: 250000, completion_tokens: 80000,
  cached_tokens: 40000, failures: 3, avg_tft: 780, avg_ms: 3300 },
  rows: [{ key: '3f2a', alias: 'zhangsan-dev', group: '研发', model: 'deepseek-v3.1',
    node: 'gb10', endpoint: '/v1/chat/completions', requests: 120, failures: 3,
    prompt_tokens: 250000, completion_tokens: 80000, cached_tokens: 40000, avg_ms: 3300 }],
  hourly: Array.from({ length: 12 }, (_, i) => ({ label: String(10 + i).padStart(2, '0') + ':00',
    reqs: 10, in: 20000, out: 6000, cache: 3000, avg_tft: 800 })), per_key: [], errors: [] };

function rangePoints(base) {
  const now = Math.floor(Date.now() / 1000);
  return Array.from({ length: 60 }, (_, i) => [now - (59 - i) * 60, Math.round((base + Math.sin(i / 6) * base * 0.2) * 10) / 10]);
}

async function installFixtures(page, seen) {
  await page.route('**/console/api/overview', route => route.fulfill({ json: OVERVIEW }));
  await page.route('**/console/api/usage?**', route => route.fulfill({ json: USAGE }));
  await page.route('**/console/api/metrics/range?**', route => {
    const params = new URL(route.request().url()).searchParams;
    seen.range = seen.range || [];
    seen.range.push(params.get('metric') + '@' + params.get('site') + 'h' + params.get('hours'));
    const base = { output_tok_s: 40, input_tok_s: 200, requests_active: 4,
      kv_cache_pct: 60, gpu_util_pct: 80 }[params.get('metric')] || 0;
    return route.fulfill({ json: { metric: params.get('metric'), site: params.get('site'),
      step: 60, points: rangePoints(base) } });
  });
}

(async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1720, height: 1000 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const seen = {};
  await installFixtures(page, seen);

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', ADMIN_PASSWORD);
  await page.click('#lg-go');
  await page.waitForURL('**/console/');

  /* ═════════ 总览 ═════════ */
  await page.goto(BASE + '/console/index.html');
  await page.waitForTimeout(1200);

  console.log('== 1. 总览 KPI 行（7 卡）:');
  const kpis = await page.locator('#ov-kpis .pf-kpi').count();
  if (kpis !== 7) throw new Error('overview must render 7 KPI cards, got ' + kpis);
  const speedVal = await page.locator('#ov-kpis .pf-kpi').first().innerText();
  if (!speedVal.includes('tok/s')) throw new Error('first KPI must be output speed');
  console.log('KPI 卡:', kpis, '| 首卡:', speedVal.split('\n')[1]);

  console.log('== 2. 图表渲染（canvas）:');
  const canvases = await page.locator('.pf-chart canvas').count();
  if (canvases < 3) throw new Error('expected >=3 chart canvases, got ' + canvases);
  console.log('canvas 数:', canvases);

  console.log('== 3. 节点健康表 + exporter 覆盖:');
  const healthRows = await page.locator('#ov-health tr').count();
  if (healthRows !== 3) throw new Error('health table must show 3 nodes, got ' + healthRows);
  const healthText = await page.locator('#ov-health').innerText();
  if (!healthText.includes('vllm') || !healthText.includes('llamacpp')) throw new Error('runtime column missing');
  if (!healthText.includes('部分')) throw new Error('partial exporter chip missing (m2s2)');
  console.log('节点行:', healthRows, '| exporter 部分标记: true');

  console.log('== 4. 时间窗口切换 → range 请求 hours:');
  seen.range = [];
  await page.locator('#ov-win button[data-win="0.25"]').click();
  await page.waitForTimeout(600);
  if (!seen.range.some(x => x.endsWith('h6'))) throw new Error('6h window must query hours=6: ' + seen.range.join(','));
  console.log('range 请求样例:', seen.range[0]);

  console.log('== 5. 节点筛选 → 健康表只剩 1 行:');
  await page.selectOption('#ov-node', 'gb10');
  await page.waitForTimeout(600);
  if (await page.locator('#ov-health tr').count() !== 1) throw new Error('node filter must narrow health table');
  console.log('节点筛选后行数:', await page.locator('#ov-health tr').count());
  await page.selectOption('#ov-node', '');
  await page.waitForTimeout(500);
  await page.screenshot({ path: '/tmp/e2e/r106-overview.png', fullPage: false });

  console.log('== 6. 主题切换（图表重渲染无错误）:');
  await page.locator('#pf-theme').click();
  await page.waitForTimeout(600);
  await page.locator('#pf-theme').click();
  await page.waitForTimeout(400);
  console.log('深浅切换完成');

  /* ═════════ 节点性能 ═════════ */
  await page.goto(BASE + '/console/nodes.html');
  await page.waitForTimeout(1200);

  console.log('== 7. 节点详情块 + 九宫格:');
  const blocks = await page.locator('.pf-node-block').count();
  if (blocks !== 3) throw new Error('expected 3 node blocks, got ' + blocks);
  const tiles = await page.locator('.pf-node-block').first().locator('.pf-metric').count();
  if (tiles !== 9) throw new Error('each node block must have 9 metric tiles, got ' + tiles);
  console.log('节点块:', blocks, '| 首块指标卡:', tiles);

  console.log('== 8. 缺失指标显示 —（m2s2 无 spec/温度/功耗/KV）:');
  const m2s2 = page.locator('.pf-node-block', { hasText: 'm2s2NasUbuntuVM-shili-dev' });
  const m2s2Text = await m2s2.locator('.pf-metric-grid').innerText();
  const dashCount = (m2s2Text.match(/—/g) || []).length;
  if (dashCount < 3) throw new Error('m2s2 must show ≥3 em-dashes for missing metrics, got ' + dashCount);
  if (/MTP\/TAR 接受率\n0/.test(m2s2Text)) throw new Error('missing spec metric must not render 0');
  console.log('m2s2 缺失指标 — 计数:', dashCount);

  console.log('== 9. dell 显示推测解码与 KV 命中、无 KV 占用:');
  const dell = await page.locator('.pf-node-block', { hasText: 'dell-shili-7960' }).locator('.pf-metric-grid').innerText();
  if (!/62\.1%/.test(dell)) throw new Error('dell spec accept rate missing');
  if (!/58\.4%/.test(dell)) throw new Error('dell cache hit rate missing');
  console.log('dell MTP/TAR=62.1% 命中=58.4%: true');

  console.log('== 10. 时间范围切换（hours 参数）+ 截图:');
  seen.range = [];
  await page.locator('#nd-win button[data-h="6"]').click();
  await page.waitForTimeout(600);
  if (!seen.range.every(x => x.endsWith('h6'))) throw new Error('nodes page must request hours=6: ' + seen.range.join(','));
  console.log('nodes range:', seen.range[0]);
  await page.screenshot({ path: '/tmp/e2e/r106-nodes.png', fullPage: false });

  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  if (errors.length) process.exit(1);
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
