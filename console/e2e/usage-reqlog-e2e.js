/* 用量页请求明细 E2E（issue #106）：搜索防抖/每页切换/自定义时间窗/失败展开/时区 */
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'test-pass-1';

function makeLogs() {
  const logs = [];
  for (let i = 0; i < 37; i++) {
    const fail = i % 9 === 0;
    logs.push({
      ts: '2026-09-14T' + String(23 - Math.floor(i / 4)).padStart(2, '0') + ':' + String(59 - (i % 4) * 7).padStart(2, '0') + ':00',
      alias: i % 2 ? 'zhangsan-dev' : 'lisi-dev', group: '研发',
      key: i % 2 ? '3f2a' : '9c1b', model: 'qwen3-32b-instruct', node: 'gb10',
      endpoint: '/v1/chat/completions', effort: i % 4 === 0 ? 'high' : '',
      prompt_tokens: 1000 + i, completion_tokens: 200 + i, cached_tokens: i % 3 ? 300 : 0,
      tft_ms: 250 + i * 5, duration_ms: 1800 + i * 20,
      status: fail ? 'failure' : 'ok',
      request_id: 'req-e2e-' + String(i).padStart(4, '0'),
      session_id: '', ip: '10.77.0.' + (2 + i % 5),
      error: fail ? 'context length exceeded: prompt 131072 tokens > model context' : ''
    });
  }
  return logs;
}

(async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1600, height: 960 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => d.accept());

  const ALL = makeLogs();
  let lastLogsUrl = '';
  await page.route('**/console/api/usage?**', route => route.fulfill({ json: {
    rows: [], errors: [], totals: { requests: 0, failures: 0, prompt_tokens: 0,
      completion_tokens: 0, cached_tokens: 0, avg_tft: 0, avg_ms: 0 }, hourly: [], per_key: [] } }));
  await page.route('**/console/api/usage/logs?**', route => {
    const url = route.request().url();
    lastLogsUrl = url;
    const params = new URL(url).searchParams;
    let rows = ALL;
    const q = params.get('q');
    if (q) rows = rows.filter(r => (r.request_id + r.alias + r.model + r.node + r.endpoint + r.error).toLowerCase().includes(q.toLowerCase()));
    return route.fulfill({ json: { logs: rows, next_cursor: '', has_more: false } });
  });

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', ADMIN_PASSWORD);
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.goto(BASE + '/console/usage.html');
  await page.waitForTimeout(600);
  await page.locator('.pf-tab[data-tab="rec"]').click();
  await page.waitForTimeout(500);

  console.log('== 1. 明细加载 + 时区(+08)格式:');
  if (await page.locator('#ug-tbody tr').count() !== 20) throw new Error('default per-page must be 20');
  const headers = await page.locator('.pf-table thead th').allInnerTexts();
  if (!headers.includes('IP')) throw new Error('request detail table must expose IP column');
  if (await page.locator('#ug-tbody .pf-token-read').count() !== 20 ||
      await page.locator('#ug-tbody .pf-token-write').count() !== 20) throw new Error('read/write token formatter missing');
  if (!(await page.locator('#ug-tbody .pf-token-cache.is-hit').count()) ||
      !(await page.locator('#ug-tbody .pf-token-cache.is-miss').count())) throw new Error('cache hit/miss state must be explicit');
  if (!(await page.locator('#ug-tbody tr').first().innerText()).includes('10.77.0.')) throw new Error('IP value missing from request detail row');
  console.log('IP 列 + 读/写 formatter + 缓存命中/未命中标记: true');
  const ts = (await page.locator('#ug-tbody tr td').first().innerText()).trim();
  console.log('首行时间:', ts, '| 格式 YYYY-MM-DD HH:MM:SS:', /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(ts));
  if (!/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$/.test(ts)) throw new Error('timestamp must be CST wall clock');

  console.log('== 2. 搜索防抖 → 服务端 q 参数:');
  await page.fill('#ug-search', 'req-e2e-0007');
  await page.waitForTimeout(700);
  if (!/[?&]q=req-e2e-0007/.test(lastLogsUrl)) throw new Error('search must send q param, got ' + lastLogsUrl);
  if (await page.locator('#ug-tbody tr').count() !== 1) throw new Error('q filter must narrow to 1 row');
  const statText = await page.locator('#ug-statbar').innerText();
  if (!statText.includes('1')) throw new Error('stats bar must react to filters');
  console.log('搜索 1 行命中 + 统计卡联动: true');
  await page.fill('#ug-search', '');
  await page.waitForTimeout(700);

  console.log('== 3. 每页切换 10/20/50:');
  await page.locator('#ug-per button[data-p="50"]').click();
  await page.waitForTimeout(300);
  const rows50 = await page.locator('#ug-tbody tr').count();
  if (rows50 !== 37) throw new Error('per-page=50 must show all 37 rows, got ' + rows50);
  await page.locator('#ug-per button[data-p="10"]').click();
  await page.waitForTimeout(300);
  if (await page.locator('#ug-tbody tr').count() !== 10) throw new Error('per-page=10 must show 10 rows');
  await page.locator('#ug-per button[data-p="20"]').click();
  console.log('每页切换: true');

  console.log('== 4. 自定义时间窗（from/to 参数）:');
  await page.locator('#ug-time button[data-t="custom"]').click();
  await page.waitForTimeout(200);
  if (await page.locator('#ug-custom-wrap').isVisible() === false) throw new Error('custom range must reveal datetime inputs');
  await page.fill('#ug-from', '2026-09-14T00:00');
  await page.waitForTimeout(500);
  if (!/from=2026-09-1/.test(lastLogsUrl)) throw new Error('custom from must be sent, got ' + lastLogsUrl);
  console.log('自定义窗请求:', lastLogsUrl.split('?')[1]);

  console.log('== 5. 失败行展开:');
  await page.locator('#ug-f-status').selectOption('failure');
  await page.waitForTimeout(500);
  await page.locator('#ug-tbody tr.pf-err-row').first().click();
  await page.waitForTimeout(200);
  const detail = await page.locator('.pf-err-detail').innerText();
  if (!detail.includes('request_id:') || !detail.includes('error:')) throw new Error('error detail must include request_id and error');
  console.log('失败详情:', detail.split('\n')[0], '/', detail.split('\n')[1].slice(0, 40) + '…');
  await page.screenshot({ path: '/tmp/e2e/r106-reqlog.png', fullPage: false });

  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  if (errors.length) process.exit(1);
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
