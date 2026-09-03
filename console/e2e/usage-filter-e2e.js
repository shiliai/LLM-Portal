/* 用量页范围切换后，Key/模型筛选必须来自当前范围的完整聚合数据。 */
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'test-pass-1';

function totals(requests) {
  return { requests, failures: 0, prompt_tokens: 0, completion_tokens: 0,
    cached_tokens: 0, total_tokens: 0, avg_tft: 0, avg_ms: 0 };
}

(async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_EXECUTABLE_PATH ?
    { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', ADMIN_PASSWORD);
  await page.click('#lg-go');
  await page.waitForURL('**/console/');

  await page.route('**/console/api/usage?days=*', async route => {
    const days = new URL(route.request().url()).searchParams.get('days');
    const rows = [{ alias: 'today-key', key: '1111', model: 'deepseek', requests: 1 }];
    if (days === '7' || days === '30') rows.push({ alias: 'week-key', key: '7777', model: 'qwen', requests: 2 });
    if (days === '30') rows.push({ alias: 'james-ubuntu', key: 'a1b2', model: 'qwen', requests: 1 });
    await route.fulfill({ json: { rows, errors: [], totals: totals(rows.length), hourly: [],
      per_key: rows.map(row => [row.alias, row.requests]) } });
  });
  await page.route('**/console/api/usage/logs?days=*', route => {
    const params = new URL(route.request().url()).searchParams;
    const logs = params.get('key') === 'a1b2'
      ? [{ alias: 'james-ubuntu', key: 'a1b2', model: 'qwen', request_id: 'older-james', status: 'ok',
          prompt_tokens: 1, completion_tokens: 1, cached_tokens: 0, duration_ms: 1 }]
      : [];
    return route.fulfill({ json: { logs, next_cursor: '' } });
  });

  await page.goto(BASE + '/console/usage.html');
  await page.waitForFunction(() => document.querySelectorAll('#rl-key option').length === 2);
  let keys = await page.locator('#rl-key option').allTextContents();
  if (keys.join('|') !== '全部 Key|today-key') throw new Error('today Key options are incorrect: ' + keys.join('|'));

  await page.selectOption('#filter-days', '7');
  await page.waitForFunction(() => document.querySelectorAll('#rl-key option').length === 3);
  keys = await page.locator('#rl-key option').allTextContents();
  if (keys.join('|') !== '全部 Key|today-key|week-key') throw new Error('7-day Key options did not refresh: ' + keys.join('|'));

  await page.click('button[data-tab="logs"]');
  await page.selectOption('#rl-key', 'week-key');
  await page.selectOption('#filter-days', '1');
  await page.waitForFunction(() => document.querySelectorAll('#rl-key option').length === 2);
  if (await page.locator('#rl-key').inputValue() !== '') throw new Error('stale Key selection was not reset');

  await page.selectOption('#filter-days', '30');
  await page.waitForFunction(() => document.querySelectorAll('#rl-key option').length === 4);
  await page.selectOption('#rl-key', 'a1b2');
  await page.waitForFunction(() => document.querySelector('#rl-rows').textContent.includes('james-ubuntu'));
  if (!(await page.locator('#rl-rows').textContent()).includes('james-ubuntu')) {
    throw new Error('server-side Key filter did not return an older matching row');
  }

  console.log('usage range filters refreshed correctly');
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
