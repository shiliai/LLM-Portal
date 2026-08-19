/* 保险库验证:创建 key → 行内「使用」自动取回明文;旧 key(未入库)优雅回退 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8399';

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => d.accept());

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', 'admin@test.local');
  await page.fill('#lg-pwd', 'test-pass-1');
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.goto(BASE + '/console/keys.html');
  await page.waitForTimeout(600);
  const rowsBefore = await page.locator('.js-use-key').count();
  console.log('初始行数:', rowsBefore, '(旧 key,未入库)');

  console.log('== 1. 旧 key 点「使用」(保险库 404 → 空输入兜底):');
  await page.locator('.js-use-key').first().click();
  await page.waitForTimeout(500);
  console.log('input:', JSON.stringify(await page.locator('#use-key').inputValue()),
    '| snippet placeholder:', (await page.locator('#cc-nix-term').textContent()).includes('在此粘贴'));
  await page.keyboard.press('Escape');

  console.log('== 2. 新建 key(入保险库):');
  await page.click('#btn-open-newkey');
  await page.fill('#newkey-alias', 'vault-e2e');
  await page.click('#btn-genkey');
  await page.waitForTimeout(700);
  const fullKey = (await page.locator('#newkey-full').textContent()).trim();
  console.log('created:', fullKey.slice(0, 10) + '…' + fullKey.slice(-4));
  await page.click('#btn-key-saved');
  await page.waitForTimeout(700);

  console.log('== 3. 新 key 那行点「使用」(应从保险库取回明文):');
  const row = page.locator('tr', { hasText: 'vault-e2e' }).first();
  await row.locator('.js-use-key').click();
  await page.waitForTimeout(600);
  const got = await page.locator('#use-key').inputValue();
  console.log('取回明文匹配:', got === fullKey ? '✓' : '✗ ' + JSON.stringify(got.slice(0, 12)));
  const snippet = await page.locator('#cc-nix-term').textContent();
  console.log('配置片段含明文:', snippet.includes(fullKey));
  await page.screenshot({ path: '/tmp/e2e/r6-vault-reveal.png' });

  console.log('== 4. reload 后再点(保险库独立于会话):');
  await page.keyboard.press('Escape');
  await page.reload();
  await page.waitForTimeout(600);
  await page.locator('tr', { hasText: 'vault-e2e' }).first().locator('.js-use-key').click();
  await page.waitForTimeout(600);
  console.log('reload 后取回:', (await page.locator('#use-key').inputValue()) === fullKey ? '✓' : '✗');

  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
})();
