/* 用量页维度筛选 E2E（issue #106）：范围切换重填下拉/失效选择复位/服务端过滤参数 */
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'test-pass-1';

function totals(requests) {
  return { requests, failures: 0, prompt_tokens: 0, completion_tokens: 0,
    cached_tokens: 0, avg_tft: 0, avg_ms: 0 };
}

(async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_EXECUTABLE_PATH ?
    { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1600, height: 960 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  /* 服务端语义夹具：范围决定返回的 rows；同时断言 KPI/分布来自同一批记录 */
  let lastUsageUrl = '', lastLogsUrl = '';
  await page.route('**/console/api/usage?**', async route => {
    const url = route.request().url();
    lastUsageUrl = url;
    const params = new URL(url).searchParams;
    const rows = [{ alias: 'today-key', key: '1111', group: 'default', model: 'deepseek',
      node: 'gb10', endpoint: '/v1/chat/completions', requests: 1, failures: 0,
      prompt_tokens: 100, completion_tokens: 50, cached_tokens: 0, avg_ms: 10 }];
    if (params.get('days') === '7' || params.get('days') === '30') {
      rows.push({ alias: 'week-key', key: '7777', group: '研发', model: 'qwen',
        node: 'dell', endpoint: '/v1/completions', requests: 2, failures: 0,
        prompt_tokens: 200, completion_tokens: 80, cached_tokens: 0, avg_ms: 20 });
    }
    if (params.get('days') === '30') {
      rows.push({ alias: 'james-ubuntu', key: 'a1b2', group: '研发', model: 'qwen',
        node: 'dell', endpoint: '/v1/completions', requests: 1, failures: 0,
        prompt_tokens: 30, completion_tokens: 10, cached_tokens: 0, avg_ms: 5 });
    }
    return route.fulfill({ json: { rows, errors: [], totals: totals(rows.length), hourly: [],
      per_key: rows.map(row => [row.alias, row.requests]) } });
  });
  await page.route('**/console/api/usage/logs?**', route => {
    const params = new URL(route.request().url()).searchParams;
    lastLogsUrl = route.request().url();
    const logs = [{ ts: '2026-09-01T10:00:00', alias: 'james-ubuntu', group: '研发', key: 'a1b2', model: 'qwen',
        node: 'dell', endpoint: '/v1/completions', effort: '',
        prompt_tokens: 1, completion_tokens: 1, cached_tokens: 0, tft_ms: 5, duration_ms: 9,
        status: 'ok', request_id: 'older-james', session_id: '', ip: '10.77.0.9', error: '' }];
    if (params.get('key') && params.get('key') !== 'a1b2') {
      return route.fulfill({ json: { logs: [], next_cursor: '', has_more: false } });
    }
    return route.fulfill({ json: { logs, next_cursor: '', has_more: false } });
  });

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', ADMIN_PASSWORD);
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.goto(BASE + '/console/usage.html');
  await page.waitForFunction(() => document.querySelectorAll('#ug-dim-key option').length === 2);
  let keys = await page.locator('#ug-dim-key option').allTextContents();
  if (keys.join('|') !== '全部 Key|…1111（today-key）') throw new Error('today Key options are incorrect: ' + keys.join('|'));

  console.log('== 1. 范围切换 → 下拉重填:');
  await page.locator('#ug-range button[data-r="7"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#ug-dim-key option').length === 3);
  keys = await page.locator('#ug-dim-key option').allTextContents();
  if (keys.join('|') !== '全部 Key|…1111（today-key）|…7777（week-key）') throw new Error('7-day Key options did not refresh: ' + keys.join('|'));
  console.log('7 天 Key 选项:', keys.join(' | '));

  console.log('== 2. 失效选择复位（切回后旧选择不在列表 → 全部）:');
  await page.locator('#ug-range button[data-r="0.9999"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#ug-dim-key option').length === 2);
  if (await page.locator('#ug-dim-key').inputValue() !== 'all') throw new Error('stale dim selection was not reset');
  console.log('失效选择复位: true');

  console.log('== 3. 今天语义（today=1 参数）:');
  await page.locator('#ug-range button[data-r="today"]').click();
  await page.waitForTimeout(500);
  if (!/[?&]today=1/.test(lastUsageUrl)) throw new Error('today range must send today=1, got ' + lastUsageUrl);
  console.log('汇总请求:', lastUsageUrl.split('?')[1]);

  console.log('== 4. 明细 Key 过滤（服务端）:');
  await page.locator('.pf-tab[data-tab="rec"]').click();
  await page.waitForTimeout(400);
  await page.locator('#ug-range button[data-r="30"], #ug-time button[data-t="7"]').first().click();
  await page.waitForTimeout(400);
  await page.selectOption('#ug-f-key', 'a1b2');
  await page.waitForFunction(() => document.querySelector('#ug-tbody').textContent.includes('james-ubuntu'));
  if (!/[?&]key=a1b2/.test(lastLogsUrl)) throw new Error('detail Key filter must be sent server-side');
  if (!(await page.locator('#ug-tbody').textContent()).includes('james-ubuntu')) {
    throw new Error('server-side Key filter did not return an older matching row');
  }
  console.log('明细服务端 Key 过滤: true |', lastLogsUrl.split('?')[1]);

  console.log('== 5. 明细重置（回到默认近24小时 + 全部 Key）:');
  await page.locator('#ug-reset').click();
  await page.waitForTimeout(500);
  if (await page.locator('#ug-f-key').inputValue() !== 'all') throw new Error('reset must clear key filter');
  if (!/days=0.9999/.test(lastLogsUrl)) throw new Error('reset must fall back to rolling 24h');
  console.log('重置后请求:', lastLogsUrl.split('?')[1]);

  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  if (errors.length) process.exit(1);
  console.log('usage range filters refreshed correctly');
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
