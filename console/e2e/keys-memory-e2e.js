/* 会话记忆验证:粘贴一次 → 关弹窗 → 再点同行的「使用」→ 密钥直接代入 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8399';
let KEY = 'sk-e2eTestKeyAbCdEf12345678';   // 会按首行尾4位重建,保证同把 Key

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', 'admin@test.local');
  await page.fill('#lg-pwd', 'test-pass-1');
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.goto(BASE + '/console/keys.html');
  await page.waitForTimeout(600);

  // 取首行尾4位,构造同尾的密钥(模拟真实场景:粘的就是这把 key)
  const tail = (await page.locator('.pf-key').first().textContent()).replace('sk-…', '');
  KEY = 'sk-e2ePrefixMiddle' + tail;
  console.log('== 0. 首行尾4位:', tail, '| 测试密钥尾4:', KEY.slice(-4));
  console.log('== 1. 首次点「使用」(无记忆):');
  await page.locator('.js-use-key').first().click();
  await page.waitForTimeout(300);
  console.log('key input:', JSON.stringify(await page.locator('#use-key').inputValue()),
    '| snippet:', (await page.locator('#cc-nix-term').textContent()).split('\n')[1]);

  console.log('== 2. 粘贴密钥:');
  await page.fill('#use-key', KEY);
  await page.waitForTimeout(200);
  console.log('snippet now:', (await page.locator('#cc-nix-term').textContent()).split('\n')[1].includes(KEY) ? '已代入 ✓' : '未代入 ✗');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);

  console.log('== 3. 再点同一行「使用」(应有记忆):');
  await page.locator('.js-use-key').first().click();
  await page.waitForTimeout(300);
  const refilled = await page.locator('#use-key').inputValue();
  const snippet = await page.locator('#cc-nix-term').textContent();
  console.log('input:', refilled === KEY ? '直接代入 ✓' : '✗ ' + JSON.stringify(refilled),
    '| snippet 含 key:', snippet.includes(KEY));

  console.log('== 4. reload 后仍在(同标签页):');
  await page.keyboard.press('Escape');
  await page.reload();
  await page.waitForTimeout(600);
  await page.locator('.js-use-key').first().click();
  await page.waitForTimeout(300);
  console.log('after reload input:', (await page.locator('#use-key').inputValue()) === KEY ? '保持 ✓' : '✗');

  console.log('== 5. 其他行(尾4不同)不受污染:');
  const n = await page.locator('.js-use-key').count();
  if (n > 1) {
    await page.keyboard.press('Escape');
    await page.locator('.js-use-key').nth(1).click();
    await page.waitForTimeout(300);
    console.log('other row input:', JSON.stringify(await page.locator('#use-key').inputValue()));
  } else { console.log('(仅一行,跳过)'); }

  await page.screenshot({ path: '/tmp/e2e/r5-session-memory.png' });
  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
})();
