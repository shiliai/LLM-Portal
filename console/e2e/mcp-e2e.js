/* MCP management confirmations stay inside Portal modals and never open a browser dialog. */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const fail = message => { console.error('FAIL:', message); process.exit(1); };

(async () => {
  const browser = await chromium.launch(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  const dialogs = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => { dialogs.push(dialog.type()); dialog.dismiss(); });

  await page.route('**/console/api/mcp/usage?days=1', route => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ keys: [] })
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

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', 'test-pass-1');
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.goto(BASE + '/console/mcp.html');
  await page.waitForSelector('.js-mcp-groups');

  await page.click('[data-modal="modal-newmcp"]');
  await page.fill('#mcp-name', 'newsvc');
  await page.fill('#mcp-url', 'https://new.invalid/mcp');
  await page.fill('#mcp-prefix', 'newsvc_');
  await page.click('#btn-create-mcp');
  await page.waitForSelector('#modal-mcp-confirm.open');
  await page.waitForTimeout(250);
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
  if (dialogs.length) fail('触发浏览器原生对话框: ' + dialogs.join(', '));
  if (errors.length) fail('页面 JS 报错: ' + errors.join(' | '));
  await browser.close();
  console.log('PASS: MCP 注册/分组/移除均使用页面确认弹窗；预检失败保留注册表单；无 browser dialog');
})().catch(error => { console.error(error); process.exit(1); });
