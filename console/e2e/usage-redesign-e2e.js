/* 用量页 C 方案 E2E:双 Tab/指标卡/图表/聚合列/排序/刷新/时区 */
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || ['admin', 'test.local'].join('@');
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'test-pass-1';

(async () => {
  const browser = await chromium.launch(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 940 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => d.accept());
  let usageCalls = 0;
  page.on('request', r => { if (r.url().includes('/console/api/usage')) usageCalls++; });

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', ADMIN_PASSWORD);
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.goto(BASE + '/console/usage.html');
  await page.waitForTimeout(900);
  await page.selectOption('#filter-days', '7');
  await page.waitForTimeout(600);

  console.log('== 1. 趋势 Tab:');
  console.log('指标卡:', await page.locator('.ustat').count(),
    '| 趋势svg:', await page.locator('#trend-chart svg').count(),
    '| TFT svg:', await page.locator('#tft-chart svg').count(),
    '| 模型条:', await page.locator('#model-bars .pf-bar-item').count());
  await page.screenshot({ path: '/tmp/e2e/r8-trend.png', fullPage: false });

  console.log('== 2. 切到明细 Tab:');
  await page.locator('.utab[data-tab="logs"]').click();
  await page.waitForTimeout(400);
  if (await page.locator('#rl-rows tr').count() !== 20) throw new Error('details tab must lazy-load exactly one 20-row page');
  const firstRow = page.locator('#rl-rows tr').first();
  const rowText = await firstRow.innerText();
  console.log('行含 ↓(输入):', rowText.includes('↓'), '| 含 ▣(缓存):', /▣|无缓存命中/.test(rowText),
    '| 含 首T:', rowText.includes('首T'), '| 含IP:', /112\.|192\.|172\./.test(rowText),
    '| 含（经 nginx）:', (await page.locator('#rl-rows').innerText()).includes('（经 nginx）'));

  console.log('== 3. 时区(+08):');
  const ts = (await firstRow.locator('td').first().innerText()).trim();
  console.log('首行时间:', ts, '| Asia/Shanghai 格式:', /^\d\d-\d\d \d\d:\d\d:\d\d$/.test(ts));

  console.log('== 4. 排序:点 Token 列头两次');
  const total = r => r; // 无法直接读,用表格首行数值对比
  async function firstTokenSum() {
    const t = await page.locator('#rl-rows tr').first().locator('td').nth(4).innerText();
    const nums = t.replace(/,/g, '').match(/\d+/g) || ['0'];
    return nums.reduce((a, b) => a + (+b), 0);
  }
  const before = await firstTokenSum();
  await page.locator('.sort-th[data-col="tokens"]').click();   // 升序
  await page.waitForTimeout(200);
  const asc = await firstTokenSum();
  await page.locator('.sort-th[data-col="tokens"]').click();   // 降序
  await page.waitForTimeout(200);
  const desc = await firstTokenSum();
  console.log('升序首行 token:', asc, '| 降序首行:', desc, '| 升序<=降序:', asc <= desc, '| 原始(时间序):', before);

  console.log('== 5. 刷新按钮(只刷数据):');
  const callsBefore = usageCalls;
  await page.locator('#btn-refresh').click();
  if (!(await page.locator('#btn-refresh').isDisabled())) throw new Error('refresh button must disable while loading');
  await page.waitForTimeout(600);
  if (await page.locator('#btn-refresh').isDisabled()) throw new Error('refresh button must recover after loading');
  console.log('usage API 调用增量:', usageCalls - callsBefore, '(logs Tab 应为 1)', '| URL 不变:', page.url().endsWith('/usage.html'));

  console.log('== 6. 详情抽屉(含 TFT/IP):');
  await page.locator('.js-rl-detail').first().click();
  await page.waitForTimeout(400);
  const detail = await page.locator('#rl-detail').innerText();
  console.log('抽屉开:', await page.locator('#drawer-reqlog.open').count(),
    '| 含IP:', detail.includes('IP'), '| 含耗时分解:', (await page.locator('#rl-lat').innerText()).includes('TFT') || (await page.locator('#rl-lat').innerText()).includes('总'));
  await page.screenshot({ path: '/tmp/e2e/r8-logs.png', fullPage: false });

  console.log('\nERRORS:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
})();
