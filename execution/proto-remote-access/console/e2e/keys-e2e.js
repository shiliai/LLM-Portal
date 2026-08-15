/* 真实后端 E2E:本地 console.py + mock LiteLLM(生产数据形状) */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8399';

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('framenavigated', f => { if (f === page.mainFrame() && !f.url().includes('favicon'))
    console.log('[nav]', f.url().replace(BASE, '')); });

  console.log('== 1. 管理员登录');
  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', 'admin@test.local');
  await page.fill('#lg-pwd', 'test-pass-1');
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.waitForTimeout(400);

  console.log('== 2. 进 keys 页');
  await page.goto(BASE + '/console/keys.html');
  await page.waitForTimeout(600);
  console.log('rows:', await page.locator('#keys-tbody tr').count(),
    '|', await page.locator('#keys-extra').textContent());

  console.log('== 3. 点击行内分组下拉(只点开不选择,再点别处关闭)');
  await page.locator('.js-group').first().click();
  await page.waitForTimeout(300);
  await page.mouse.click(700, 500);
  await page.waitForTimeout(300);
  console.log('after open/close select, rows:', await page.locator('#keys-tbody tr').count());

  console.log('== 4. 切换行内分组 home → default(触发 POST update)');
  await page.locator('.js-group').first().selectOption('home');
  await page.waitForTimeout(700);
  console.log('after change, rows:', await page.locator('#keys-tbody tr').count(),
    '| first row group now:', await page.locator('.js-group').first().inputValue());

  console.log('== 5. 复现关键场景:搜索框粘完整密钥后 reload(浏览器恢复表单值?)');
  await page.fill('#f-search', 'sk-REDACTED-ROTATED-2026-08-16');
  await page.waitForTimeout(300);
  console.log('paste detected, search now:', JSON.stringify(await page.locator('#f-search').inputValue()),
    '| modal open:', await page.locator('#modal-usekey.open').count());
  await page.keyboard.press('Escape');
  await page.reload();
  await page.waitForTimeout(600);
  console.log('after reload: rows:', await page.locator('#keys-tbody tr').count(),
    '| search restored:', JSON.stringify(await page.locator('#f-search').inputValue()),
    '| extra:', await page.locator('#keys-extra').textContent());
  await page.screenshot({ path: '/tmp/e2e/r1-after-reload.png' });

  console.log('== 6. 手动输入不匹配词再 reload');
  await page.fill('#f-search', '不存在的词');
  await page.waitForTimeout(200);
  await page.reload();
  await page.waitForTimeout(600);
  console.log('after reload2: rows:', await page.locator('#keys-tbody tr').count(),
    '| search:', JSON.stringify(await page.locator('#f-search').inputValue()));
  await page.screenshot({ path: '/tmp/e2e/r2-filter-persist.png' });

  console.log('== 7. 清空搜索,点「使用」');
  await page.fill('#f-search', '');
  await page.waitForTimeout(200);
  await page.locator('.js-use-key').first().click();
  await page.waitForTimeout(400);
  console.log('modal open:', await page.locator('#modal-usekey.open').count(),
    '| focused is use-key:', await page.evaluate(() => document.activeElement && document.activeElement.id));
  await page.screenshot({ path: '/tmp/e2e/r3-usekey.png' });

  console.log('== 8. 编辑/禁用/新建全链路');
  console.log('== 编辑弹窗');
  await page.locator('.js-edit-key').first().click();
  await page.waitForTimeout(300);
  console.log('edit modal open:', await page.locator('#modal-editkey.open').count());
  await page.fill('#edit-alias', 'justink-改名');
  await page.click('#btn-editkey');
  await page.waitForTimeout(600);
  const firstAlias = await page.locator('#keys-tbody tr td').first().textContent();
  console.log('alias after edit:', firstAlias);

  console.log('== 禁用/启用');
  await page.locator('.js-toggle-key').first().click();
  await page.waitForTimeout(600);
  const chip = await page.locator('#keys-tbody .pf-chip.gray').count();
  console.log('disabled chips:', chip, '| select disabled:', await page.locator('.js-group[disabled]').count());
  await page.locator('.js-toggle-key').first().click();
  await page.waitForTimeout(600);
  console.log('re-enabled, disabled chips:', await page.locator('#keys-tbody .pf-chip.gray').count());

  console.log('== 新建密钥 → 查看客户端配置');
  await page.click('#btn-open-newkey');
  await page.fill('#newkey-alias', 'e2e-新建');
  await page.click('#btn-genkey');
  await page.waitForTimeout(600);
  const fullKey = await page.locator('#newkey-full').textContent();
  console.log('new key shown:', fullKey.slice(0, 12) + '…');
  await page.click('#btn-use-now');
  await page.waitForTimeout(500);
  console.log('use modal open:', await page.locator('#modal-usekey.open').count(),
    '| key prefilled:', (await page.locator('#use-key').inputValue()).slice(0, 10) + '…',
    '| alias:', await page.locator('#use-alias').textContent());
  await page.screenshot({ path: '/tmp/e2e/r4-newkey-use.png' });

  console.log('== 客户端 tabs 切换');
  for (const tab of ['Codex CLI', 'OpenAI 兼容', 'pi']) {
    await page.locator(`.pf-tabs > .pf-tab:has-text("${tab}")`).first().click();
    await page.waitForTimeout(150);
  }
  const piJson = await page.locator('#pi-json').textContent();
  console.log('pi json has key:', piJson.includes(fullKey.trim()), '| has baseUrl:', piJson.includes('/v1'));

  console.log('== 密钥粘贴框显示/隐藏');
  await page.click('#btn-use-show');
  console.log('type after show:', await page.locator('#use-key').getAttribute('type'));

  
  console.log('\n== ERRORS:', errors.length ? '\n' + errors.join('\n') : 'none');
  await browser.close();
})();
