/* 用量页请求明细 E2E */
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
  await page.goto(BASE + '/console/usage.html');
  await page.waitForTimeout(800);

  console.log('== 1. 明细表加载:');
  const rows = await page.locator('#rl-rows tr').count();
  const extra = await page.locator('#rl-extra').textContent();
  console.log('首屏行数:', rows, '|', extra);

  console.log('== 2. 状态筛选=失败:');
  await page.selectOption('#rl-status', 'failure');
  await page.waitForTimeout(300);
  console.log('失败行数:', await page.locator('#rl-rows tr').count(),
    '| 失败chip数:', await page.locator('#rl-rows .pf-chip.err').count());
  await page.selectOption('#rl-status', '');
  await page.waitForTimeout(200);

  console.log('== 3. 搜索 request_id:');
  await page.fill('#rl-search', 'req-0007');
  await page.waitForTimeout(300);
  console.log('搜索后行数:', await page.locator('#rl-rows tr').count());
  await page.fill('#rl-search', '');
  await page.waitForTimeout(200);

  console.log('== 4. 分页:');
  await page.click('#rl-next');
  await page.waitForTimeout(200);
  console.log('第2页页码:', await page.locator('#rl-page').textContent(),
    '| 行数:', await page.locator('#rl-rows tr').count());
  await page.click('#rl-prev');
  await page.waitForTimeout(200);

  console.log('== 5. 详情抽屉:');
  await page.locator('.js-rl-detail').first().click();
  await page.waitForTimeout(400);
  const drawerOpen = await page.locator('#drawer-reqlog.open').count();
  const detail = await page.locator('#rl-detail').innerText();
  console.log('抽屉打开:', drawerOpen, '| 含 Request ID:', detail.includes('req-'), '| 含延迟:', detail.includes('ms') || detail.includes('s'));
  await page.screenshot({ path: '/tmp/e2e/r7-reqlog.png' });

  console.log('== 6. 聚合表仍在:');
  console.log('聚合行数:', await page.locator('#usage-rows tr').count(),
    '| 合计请求:', await page.locator('#sum-req').textContent());

  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
})();
