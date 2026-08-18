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
  await page.click('button[data-tab="logs"]');   // 页面默认激活「趋势」Tab，明细交互前需切换
  await page.waitForTimeout(200);

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

  console.log('== 5. 思考列:');
  const effortHead = await page.locator('#rl-head th', { hasText: '思考' }).count();
  const effortCells = await page.locator('#rl-rows tr td:nth-child(5)').allInnerTexts();
  const effortShown = effortCells.filter(t => t.trim() !== '—' && t.trim() !== '').length;
  console.log('表头思考列:', effortHead, '| 首屏显示 effort 的行:', effortShown,
    '| 样例:', effortCells.find(t => t.trim() !== '—' && t.trim() !== '') || '无');

  console.log('== 6. 详情抽屉:');
  await page.locator('.js-rl-detail').first().click();
  await page.waitForTimeout(400);
  const drawerOpen = await page.locator('#drawer-reqlog.open').count();
  const detail = await page.locator('#rl-detail').innerText();
  console.log('抽屉打开:', drawerOpen, '| 含 Request ID:', detail.includes('req-'),
    '| 含思考强度:', detail.includes('思考强度'),
    '| 含延迟:', detail.includes('ms') || detail.includes('s'));
  await page.screenshot({ path: '/tmp/e2e/r7-reqlog.png' });
  await page.evaluate(() => window.pfDrawerClose());   // 抽屉遮罩会拦截后续点击

  console.log('== 7. 趋势 Tab 仍在:');
  await page.click('button[data-tab="trend"]');
  await page.waitForTimeout(200);
  console.log('指标卡数:', await page.locator('#stat-grid > *').count(),
    '| 模型分布条数:', await page.locator('#model-bars > *').count());

  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
})();
