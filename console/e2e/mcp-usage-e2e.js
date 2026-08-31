/* MCP 调用计数的范围、刷新、状态与陈旧响应覆盖。 */
const { chromium } = require('playwright');
const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const fail = message => { throw new Error(message); };

(async () => {
  const browser = await chromium.launch(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.setDefaultTimeout(5000);
  const errors = [];
  const requests = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/console/api/mcp', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ builtin: {}, groups: [], external: [] }) }));
  await page.route('**/console/api/mcp/vision', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ candidates: [], selected: {}, catalog: {} }) }));
  await page.route('**/console/api/mcp/usage?days=*', async route => {
    const days = new URL(route.request().url()).searchParams.get('days');
    requests.push(days);
    if (days === '1' && requests.filter(x => x === '1').length === 1) {
      await new Promise(resolve => setTimeout(resolve, 300));
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ keys: [{ alias: 'old', tools: { old_tool: 1 }, total: 1 }] }) });
    }
    if (days === '30') return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'MCP 用量数据库不可用' }) });
    if (days === '7' && requests.filter(x => x === '7').length === 2) await new Promise(resolve => setTimeout(resolve, 150));
    const keys = days === '7' ? [{ alias: 'team-a', tools: { svc_ping: 2 }, total: 2 }] : [];
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ keys }) });
  });

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', 'test-pass-1');
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.goto(BASE + '/console/mcp.html');
  await page.waitForSelector('#mcp-usage-rows');
  if (!(await page.locator('#mcp-usage-status').textContent()).includes('正在加载')) fail('缺少中文加载状态');
  await page.click('[data-mcp-usage-days="7"]');
  await page.waitForFunction(() => document.querySelector('#mcp-usage-rows').textContent.includes('team-a'));
  if ((await page.locator('#mcp-usage-rows').textContent()).includes('old_tool')) fail('stale Today response replaced selected range');
  if (!requests.includes('1') || !requests.includes('7')) fail('range requests missing');
  if (!(await page.locator('#mcp-usage-status').textContent()).includes('最近 7 天数据已刷新')) fail('缺少中文数据刷新状态');
  await page.click('#btn-refresh-mcp-usage');
  if ((await page.locator('#btn-refresh-mcp-usage').textContent()) !== '↻ 刷新中…') fail('刷新按钮未显示中文加载状态');
  await page.waitForFunction(() => window.__mcpUsageRefreshSeen = (document.querySelector('#mcp-usage-status').textContent || '').includes('最近 7 天数据已刷新'));
  if (requests.filter(x => x === '7').length !== 2) fail('Refresh did not refetch selected range');
  await page.click('[data-mcp-usage-days="1"]');
  await page.waitForFunction(() => document.querySelector('#mcp-usage-rows').textContent.includes('暂无 MCP 工具调用'));
  await page.click('[data-mcp-usage-days="30"]');
  await page.waitForFunction(() => document.querySelector('#mcp-usage-status').textContent.includes('MCP 工具调用加载失败'));
  await page.waitForSelector('.pf-toast:has-text("MCP 工具调用加载失败")');
  if (errors.length) fail('page errors: ' + errors.join(' | '));
  await browser.close();
  console.log('PASS: MCP 调用计数的范围、刷新、中文状态与陈旧响应保护');
})().catch(async error => { console.error(error); process.exit(1); });
