/* 用量页（issue #106 原型落地）E2E：双 Tab / KPI / 2×2 分布仪表盘 / 模型行展开按节点拆分 */
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'test-pass-1';

const ROWS = [
  { key: '3f2a', alias: 'zhangsan-dev', group: '研发', model: 'qwen3-32b-instruct', node: 'gb10', endpoint: '/v1/chat/completions',
    requests: 40, failures: 2, prompt_tokens: 120000, completion_tokens: 40000, cached_tokens: 60000, avg_ms: 3200 },
  { key: '3f2a', alias: 'zhangsan-dev', group: '研发', model: 'qwen3-32b-instruct', node: 'gb10-worker', endpoint: '/v1/chat/completions',
    requests: 25, failures: 0, prompt_tokens: 80000, completion_tokens: 26000, cached_tokens: 41000, avg_ms: 3600 },
  { key: '9c1b', alias: 'lisi-dev', group: '研发', model: 'deepseek-v3.1', node: 'dell', endpoint: '/v1/chat/completions',
    requests: 18, failures: 1, prompt_tokens: 95000, completion_tokens: 30000, cached_tokens: 52000, avg_ms: 5100 },
  { key: '77e0', alias: 'wangwu-da', group: '数据分析', model: 'glm-4.5-air', node: 'm2s2', endpoint: '/v1/embeddings',
    requests: 45, failures: 0, prompt_tokens: 8000, completion_tokens: 0, cached_tokens: 0, avg_ms: 210 }
];
const TOTALS = {
  requests: 128, failures: 3, prompt_tokens: 303000, completion_tokens: 96000,
  cached_tokens: 153000, avg_tft: 812, avg_ms: 3400
};
const HOURLY = Array.from({ length: 24 }, (_, i) => ({
  label: String(i).padStart(2, '0') + ':00',
  reqs: i > 8 && i < 20 ? 6 : 0, in: i > 8 && i < 20 ? 24000 : 0,
  out: i > 8 && i < 20 ? 8000 : 0, cache: i > 8 && i < 20 ? 12000 : 0, avg_tft: 800
}));

async function installFixtures(page) {
  await page.route('**/console/api/usage?**', route => route.fulfill({ json: {
    rows: ROWS, errors: [], totals: TOTALS, hourly: HOURLY,
    per_key: [['zhangsan-dev', 65], ['lisi-dev', 18], ['wangwu-da', 45]] } }));
  await page.route('**/console/api/usage/logs?**', route => {
    const params = new URL(route.request().url()).searchParams;
    const per = Number(params.get('limit') || 2000);
    const logs = [];
    for (let i = 0; i < 45; i++) {
      const r = ROWS[i % ROWS.length];
      const fail = i % 15 === 0;
      logs.push({
        ts: '2026-09-14T10:' + String(59 - i % 60).padStart(2, '0') + ':00',
        alias: r.alias, group: r.group, key: r.key, model: r.model, node: r.node,
        endpoint: r.endpoint, effort: i % 3 === 0 ? 'high' : '',
        prompt_tokens: 900 + i, completion_tokens: 300 + i, cached_tokens: i % 2 ? 400 : 0,
        tft_ms: 300 + i * 10, duration_ms: 2000 + i * 30,
        status: fail ? 'failure' : 'ok',
        request_id: 'req-idx-' + String(i).padStart(4, '0'),
        session_id: '', ip: '10.77.0.2',
        error: fail ? 'upstream timeout: no response within 120s' : ''
      });
    }
    return route.fulfill({ json: { logs: logs.slice(0, per), next_cursor: '', has_more: false } });
  });
}

(async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => d.accept());
  await installFixtures(page);

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', ADMIN_PASSWORD);
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.goto(BASE + '/console/usage.html');
  await page.waitForTimeout(900);

  console.log('== 1. 汇总 Tab（默认）:');
  const kpis = await page.locator('#ug-kpis .pf-kpi').count();
  if (kpis !== 5) throw new Error('summary KPI row must have 5 cards, got ' + kpis);
  console.log('KPI 卡:', kpis,
    '| 分布表行数(模型):', await page.locator('#ug-dist-model tr').count(),
    '| 端点表行数:', await page.locator('#ug-dist-ep tr').count());
  await page.screenshot({ path: '/tmp/e2e/r106-summary.png', fullPage: false });

  console.log('== 2. 模型行展开 → 按节点拆分:');
  await page.locator('#ug-dist-model tr[data-dx]').first().click();
  await page.waitForTimeout(200);
  const expanded = (await page.locator('#ug-dist-model').innerText()).includes('└');
  if (!expanded) throw new Error('model row expansion must reveal per-node split');
  console.log('展开后含节点拆分行: true');
  await page.locator('#ug-dist-model tr[data-dx]').first().click();

  console.log('== 3. 分布 Tab 切换（按请求数）:');
  await page.locator('#ug-model-tabs button[data-m="req"]').click();
  await page.waitForTimeout(200);
  console.log('模型分布切换后行数:', await page.locator('#ug-dist-model tr').count());

  console.log('== 4. 时间范围切换（请求参数断言）:');
  let lastUsageUrl = '', lastLogsUrl = '';
  page.on('request', r => {
    if (r.url().includes('/console/api/usage?')) lastUsageUrl = r.url();
    if (r.url().includes('/console/api/usage/logs?')) lastLogsUrl = r.url();
  });
  await page.locator('#ug-range button[data-r="7"]').click();
  await page.waitForTimeout(500);
  if (!/days=7/.test(lastUsageUrl)) throw new Error('range switch must query days=7, got ' + lastUsageUrl);
  console.log('切换范围请求:', lastUsageUrl.split('?')[1]);

  console.log('== 5. 维度筛选联动（服务端过滤参数）:');
  await page.selectOption('#ug-dim-model', 'qwen3-32b-instruct');
  await page.waitForTimeout(500);
  if (!/model=qwen3-32b-instruct/.test(decodeURIComponent(lastUsageUrl))) throw new Error('model dim must be sent server-side');
  await page.click('#ug-dim-reset');
  await page.waitForTimeout(500);
  if (/model=/.test(lastUsageUrl)) throw new Error('reset must clear dim filters');
  console.log('维度筛选发送/重置: true');

  console.log('== 6. 明细 Tab:');
  await page.locator('.pf-tab[data-tab="rec"]').click();
  await page.waitForTimeout(500);
  const statCards = await page.locator('#ug-statbar .pf-kpi').count();
  const rows = await page.locator('#ug-tbody tr').count();
  if (statCards !== 6) throw new Error('record stats bar must have 6 cards, got ' + statCards);
  if (rows !== 20) throw new Error('default per-page is 20, got ' + rows);
  console.log('统计卡:', statCards, '| 首页行数:', rows);
  await page.screenshot({ path: '/tmp/e2e/r106-records.png', fullPage: false });

  console.log('== 7. 分页:');
  const firstPageFirst = await page.locator('#ug-tbody tr td').first().innerText();
  await page.locator('#ug-pager button[data-pg="2"]').click();
  await page.waitForTimeout(200);
  const secondPageFirst = await page.locator('#ug-tbody tr td').first().innerText();
  if (firstPageFirst === secondPageFirst) throw new Error('pagination must change rows');
  console.log('翻页后首行时间变化: true');

  console.log('== 8. 失败行展开错误详情:');
  await page.locator('#ug-f-status').selectOption('failure');
  await page.waitForTimeout(500);
  if (!/status=failure/.test(lastLogsUrl)) throw new Error('status filter must be sent server-side');
  await page.locator('#ug-tbody tr.pf-err-row').first().click();
  await page.waitForTimeout(200);
  const errDetail = await page.locator('.pf-err-detail').count();
  if (!errDetail) throw new Error('failed row must expand error detail');
  console.log('失败行展开:', errDetail, '| 详情含 request_id:', (await page.locator('.pf-err-detail').innerText()).includes('request_id:'));

  console.log('== 9. 导出 CSV:');
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('#ug-csv').click()
  ]);
  console.log('CSV 文件名:', download.suggestedFilename());
  if (!/llm-portal-requests-\d{4}-\d{2}-\d{2}\.csv/.test(download.suggestedFilename())) {
    throw new Error('unexpected CSV filename');
  }

  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  if (errors.length) process.exit(1);
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
