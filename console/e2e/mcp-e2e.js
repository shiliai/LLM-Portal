/* MCP management confirmations stay inside Portal modals and never open a browser dialog. */
const { chromium } = require('playwright');
const BASE = process.env.E2E_BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const fail = message => { console.error('FAIL:', message); process.exit(1); };

async function loginForLocalE2E(page) {
  // consoled sessions are intentionally Secure.  Local HTTP E2E must inject the
  // short-lived test session after obtaining it through the same login endpoint.
  const response = await page.request.post(BASE + '/console/api/admin-login', {
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    data: { email: ADMIN_EMAIL, password: 'test-pass-1' }
  });
  const match = (response.headers()['set-cookie'] || '').match(/pll_session=([^;]+)/);
  if (!response.ok() || !match) fail('本地 E2E 管理员会话创建失败');
  await page.context().addCookies([{
    name: 'pll_session', value: match[1], domain: '127.0.0.1', path: '/console',
    httpOnly: true, secure: false, sameSite: 'Lax'
  }]);
}

(async () => {
  const browser = await chromium.launch(process.env.PW_CHANNEL ? {
    channel: process.env.PW_CHANNEL,
    // consoled deliberately marks sessions Secure; keep the local HTTP E2E origin usable.
    args: ['--unsafely-treat-insecure-origin-as-secure=' + BASE]
  } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  const dialogs = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => { dialogs.push(dialog.type()); dialog.dismiss(); });

  await page.route('**/console/api/mcp/usage?days=*', route => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ keys: [
      { alias: 'team-research', tools: { zai_search_web_search: 8, analyze_image: 3 }, total: 11 },
      { alias: 'automation', tools: { upload_image: 2 }, total: 2 }
    ] })
  }));
  await page.route('**/console/api/mcp', route => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      builtin: { tool: 'analyze_image(question, image_url | image_base64[, mime_type])', model: 'unit' },
      groups: ['home'], external: [{ name: 'svc', url: 'https://mcp.invalid/mcp',
        api_key_last4: '...1234', prefix: 'svc_', groups: ['home'] }]
    })
  }));
  await page.route('**/console/api/mcp/register', route => route.fulfill({
    status: 422, contentType: 'application/json', body: JSON.stringify({
      error: 'MCP 预检未发现可用工具，请检查上游 tools/list 响应。', category: 'zero_tools'
    })
  }));

  await loginForLocalE2E(page);
  await page.goto(BASE + '/console/mcp.html');
  await page.waitForSelector('#mcp-usage-rows');
  await page.waitForFunction(() => document.querySelector('#mcp-usage-rows').textContent.includes('team-research'));
  if ((await page.locator('[data-mcp-main-tab="usage"]').getAttribute('aria-selected')) !== 'true') {
    fail('MCP 管理页默认未打开调用用量 Tab');
  }
  if (!(await page.locator('#mcp-usage-stats').textContent()).includes('13')) fail('用量摘要未从真实 keys[].total 派生');
  if (!(await page.locator('#mcp-usage-rows').textContent()).includes('zai_search_web_search')) fail('用量表未展示真实工具明细');
  for (const tab of ['tools', 'external', 'usage', 'config']) {
    await page.click('[data-mcp-main-tab="' + tab + '"]');
    if ((await page.locator('#mcp-pane-' + tab).isHidden())) fail('主 Tab 未切换到 ' + tab);
  }
  await page.click('[data-mcp-main-tab="external"]');
  await page.waitForSelector('.js-mcp-groups');

  await page.click('[data-modal="modal-newmcp"]');
  await page.fill('#mcp-name', 'newsvc');
  await page.fill('#mcp-url', 'https://new.invalid/mcp');
  await page.fill('#mcp-prefix', 'newsvc_');
  await page.click('#btn-create-mcp');
  await page.waitForSelector('#modal-mcp-confirm.open');
  await page.waitForTimeout(300);
  const initialFocus = await page.evaluate(() => document.activeElement.id || document.activeElement.tagName);
  if (initialFocus !== 'btn-mcp-confirm-submit') {
    fail('确认弹窗未将初始焦点置于确认按钮: ' + initialFocus);
  }
  await page.keyboard.press('Tab');
  if (await page.evaluate(() => document.activeElement.id) !== 'btn-mcp-confirm-close') {
    fail('确认弹窗焦点未在末尾回绕');
  }
  await page.keyboard.press('Shift+Tab');
  if (await page.evaluate(() => document.activeElement.id) !== 'btn-mcp-confirm-submit') {
    fail('确认弹窗 Shift+Tab 焦点未回绕');
  }
  await page.keyboard.press('Escape');
  await page.waitForSelector('#modal-newmcp.open');
  await page.waitForFunction(() => document.activeElement.id === 'btn-create-mcp');
  if (!(await page.locator('#modal-newmcp').isVisible())) fail('取消注册确认后表单未恢复');
  if ((await page.inputValue('#mcp-name')) !== 'newsvc') fail('取消注册确认后表单内容丢失');
  if (await page.evaluate(() => document.activeElement.id) !== 'btn-create-mcp') fail('Escape 后焦点未恢复');

  await page.click('#btn-create-mcp');
  await page.waitForSelector('#modal-mcp-confirm.open');
  await page.locator('.pf-mask').click({ position: { x: 2, y: 2 } });
  await page.waitForSelector('#modal-newmcp.open');

  await page.click('#btn-create-mcp');
  await page.click('#btn-mcp-confirm-submit');
  await page.waitForSelector('#modal-newmcp.open');
  if ((await page.inputValue('#mcp-url')) !== 'https://new.invalid/mcp') fail('预检失败后注册表单未保留');
  if (!(await page.locator('#mcp-register-error').textContent()).includes('预检未发现可用工具')) {
    fail('预检错误未保留在注册表单的实时提示区');
  }
  if (await page.locator('.pf-toast', { hasText: '已注册并验证' }).count()) fail('预检失败后出现注册成功提示');

  await page.click('#modal-newmcp [data-close]');
  await page.click('.js-mcp-groups');
  await page.click('#btn-save-mcp-groups');
  await page.waitForSelector('#modal-mcp-confirm.open');
  await page.click('#btn-mcp-confirm-cancel');
  await page.waitForSelector('#modal-mcpgroups.open');
  if (!(await page.locator('#modal-mcpgroups').isVisible())) fail('取消分组保存后表单未恢复');

  await page.click('#modal-mcpgroups [data-close]');
  await page.click('.js-mcp-remove');
  await page.waitForSelector('#modal-mcp-confirm.open');
  await page.click('#btn-mcp-confirm-cancel');
  await page.click('[data-mcp-main-tab="usage"]');
  await page.selectOption('#filter-mcp-usage-days', '7');
  await page.waitForFunction(() => document.querySelector('#mcp-usage-status').textContent.includes('最近 7 天数据已刷新'));
  await page.click('#btn-refresh-mcp-usage');
  if (!(await page.locator('#btn-refresh-mcp-usage').isDisabled())) fail('用量刷新期间按钮未禁用');
  await page.waitForFunction(() => !document.querySelector('#btn-refresh-mcp-usage').disabled);
  await page.setViewportSize({ width: 390, height: 844 });
  for (const tab of ['tools', 'external', 'usage', 'config']) {
    await page.click('[data-mcp-main-tab="' + tab + '"]');
    if (await page.evaluate(() => document.body.scrollWidth > window.innerWidth)) {
      fail('390px 视口在 ' + tab + ' Tab 发生 body 横向溢出');
    }
  }
  await page.click('[data-mcp-main-tab="usage"]');
  const mobile = await page.evaluate(() => ({
    bodyOverflow: document.body.scrollWidth > window.innerWidth,
    tabScrollable: document.querySelector('.mcp-main-tabs').scrollWidth > document.querySelector('.mcp-main-tabs').clientWidth,
    tableScrollable: document.querySelector('.mcp-usage-table-wrap').scrollWidth > document.querySelector('.mcp-usage-table-wrap').clientWidth,
    controlsOverlap: Array.from(document.querySelectorAll('.mcp-usage-toolbar button, .mcp-usage-toolbar select')).some(function (control, index, controls) {
      var a = control.getBoundingClientRect();
      return controls.slice(index + 1).some(function (other) {
        var b = other.getBoundingClientRect();
        return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
          Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
      });
    })
  }));
  if (mobile.bodyOverflow) fail('390px 视口发生 body 横向溢出');
  if (!mobile.tabScrollable) fail('390px 主 Tab 未提供横向滚动');
  if (!mobile.tableScrollable) fail('390px 用量表未保持 section 内横向滚动');
  if (mobile.controlsOverlap) fail('390px 用量工具栏控件发生重叠');
  if (dialogs.length) fail('触发浏览器原生对话框: ' + dialogs.join(', '));
  if (errors.length) fail('页面 JS 报错: ' + errors.join(' | '));
  await browser.close();
  console.log('PASS: MCP 注册/分组/移除均使用页面确认弹窗；预检失败保留注册表单；无 browser dialog');
})().catch(error => { console.error(error); process.exit(1); });
