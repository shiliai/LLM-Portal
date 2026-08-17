/* 站点页「模型」管理弹窗 E2E（回归背景:smReload 曾定义在 pfReady.then 回调内,
   bindRowActions 点击时 ReferenceError → 弹窗打不开;页面无 JS 报错是硬断言） */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8399';
const fail = m => { console.error('FAIL:', m); process.exit(1); };

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
  await page.goto(BASE + '/console/sites.html');
  await page.waitForSelector('#sites-tbody tr');
  await page.waitForTimeout(300);

  console.log('== 1. 打开模型管理弹窗(曾在此挂死):');
  await page.click('.js-site-models');
  await page.waitForTimeout(400);
  const modalVisible = await page.locator('#modal-site-models').isVisible();
  console.log('弹窗可见:', modalVisible);
  if (!modalVisible) fail('模型弹窗未打开');
  const rows = await page.locator('#sm-list .pf-dep-item').count();
  console.log('deployment 行数:', rows);
  if (rows !== 1) fail('期望 1 行(夹具 workstation x qwen3.6)');
  console.log('首行上游:', await page.locator('#sm-list .pf-dep-item .pf-muted').nth(1).textContent());

  console.log('== 2. 刷新上游(两 id → 下拉):');
  await page.click('.js-sm-refresh');
  await page.waitForSelector('#sm-list .js-sm-edit select');
  await page.selectOption('#sm-list .js-sm-edit select', 'qwen3.8-27b-mtp2');
  await page.click('.js-sm-apply');
  await page.waitForFunction(() =>
    document.querySelector('#sm-list .pf-dep-item .pf-muted:nth-of-type(1)') &&
    document.querySelector('#sm-list').textContent.includes('qwen3.8-27b-mtp2'));
  await page.waitForTimeout(300);
  const refreshed = await page.locator('#sm-list').textContent();
  console.log('刷新后列表含新 id:', refreshed.includes('qwen3.8-27b-mtp2'),
    '| 对外名不变:', refreshed.includes('qwen3.6-35b-fp8'));
  if (!refreshed.includes('qwen3.8-27b-mtp2')) fail('刷新上游未生效');

  console.log('== 3. 手动添加(探测回填):');
  await page.fill('#sm-name', 'test-model');
  await page.fill('#sm-port', '8004');   // 与假上游 mock 同端口(探测是服务端发起的真请求)
  await page.click('#sm-probe');
  await page.waitForSelector('#sm-chips .js-sm-chip');
  await page.click('#sm-chips .js-sm-chip');
  console.log('点选后上游 id:', await page.inputValue('#sm-upstream'),
    '| 对外名保留手填:', await page.inputValue('#sm-name'));
  if ((await page.inputValue('#sm-upstream')) !== 'qwen3.8-27b-mtp2') fail('chip 未回填上游 id');
  await page.click('#btn-sm-add');
  await page.waitForFunction(() =>
    document.querySelectorAll('#sm-list .pf-dep-item').length >= 2);
  await page.waitForTimeout(300);
  console.log('添加后行数:', await page.locator('#sm-list .pf-dep-item').count());
  if (await page.locator('#sm-list .pf-dep-item').count() !== 2) fail('手动添加未入列');

  console.log('== 4. 两段式删除:');
  await page.locator('#sm-list .pf-dep-item', { hasText: 'test-model' })
    .locator('.js-sm-del').click();
  await page.waitForTimeout(100);
  const armed = await page.locator('#sm-list .js-sm-del', { hasText: '确认删除？' }).count();
  console.log('一段后按钮文案已变(防误点):', armed === 1);
  if (armed !== 1) fail('删除未进入确认态');
  await page.locator('#sm-list .pf-dep-item', { hasText: 'test-model' })
    .locator('.js-sm-del').click();
  await page.waitForFunction(() =>
    document.querySelectorAll('#sm-list .pf-dep-item').length === 1);
  await page.waitForTimeout(200);
  console.log('删除后行数:', await page.locator('#sm-list .pf-dep-item').count());
  if (await page.locator('#sm-list .pf-dep-item').count() !== 1) fail('删除未生效');

  await page.screenshot({ path: 'r-sites-models.png', fullPage: true });
  await browser.close();
  if (errors.length) fail('页面 JS 报错: ' + errors.join(' | '));   // 回归硬断言
  console.log('PASS: 模型管理弹窗全链路(打开/刷新上游/探测添加/两段删除),无 pageerror');
})();
