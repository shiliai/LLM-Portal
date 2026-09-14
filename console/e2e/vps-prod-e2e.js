/* 生产环境 e2e（vps-tencent-tokyo 公网入口，真实节点 + 真实 VictoriaMetrics/Postgres）
 * 凭据经环境变量传入，不落盘不打印。 */
const { chromium } = require('playwright');
const BASE = process.env.PROD_BASE || 'https://private-llm.onlyservice.io';
const EMAIL = process.env.PROD_EMAIL, PASSWORD = process.env.PROD_PASSWORD, TOTP = process.env.PROD_TOTP;

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || '/usr/bin/google-chrome' });
  const page = await browser.newPage({ viewport: { width: 1720, height: 1000 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', EMAIL);
  await page.fill('#lg-pwd', PASSWORD);
  if (TOTP) await page.fill('#lg-totp', TOTP).catch(() => {});
  await page.click('#lg-go');
  await page.waitForURL(/\/console\/$/, { timeout: 20000 });   // 登录成功落到控制台根
  // 登录页自身会跳转到 index；等壳渲染完成，避免导航打断在途请求
  await page.waitForSelector('.pf-layout', { timeout: 30000 });

  /* ═══ 总览（真实节点） ═══ */
  await page.goto(BASE + '/console/index.html', { waitUntil: 'domcontentloaded' });
  console.log('== 1. 总览:');
  try {
    await page.waitForSelector('#ov-kpis .pf-kpi', { timeout: 20000 });
  } catch (e) {
    const last = await page.locator('#ov-last').innerText().catch(() => '?');
    throw new Error('overview KPI did not render (state: ' + last + '; errors: ' + errors.join(' | ') + ')');
  }
  await page.waitForTimeout(800);
  const kpis = await page.locator('#ov-kpis .pf-kpi').count();
  if (kpis !== 7) throw new Error('overview KPI cards = ' + kpis);
  const health = await page.locator('#ov-health').innerText();
  for (const node of ['gb10', 'dell-shili-7960', 'm2s2NasUbuntuVM-shili-dev']) {
    if (!health.includes(node)) throw new Error('real node missing in health table: ' + node);
  }
  const runtimeOk = health.includes('vllm') && health.includes('llamacpp');
  console.log('  KPI=7 | 真实节点 3 行 | runtime 列(vllm+llamacpp):', runtimeOk);
  console.log('  健康表原文前 120 字:', health.replace(/\n/g, ' | ').slice(0, 200));
  await page.screenshot({ path: '/tmp/e2e/prod-overview.png' });

  /* ═══ 节点性能（真实 DCGM / 缺失 —） ═══ */
  await page.goto(BASE + '/console/nodes.html', { waitUntil: 'domcontentloaded' });
  console.log('== 2. 节点性能:');
  await page.waitForSelector('.pf-node-block', { timeout: 20000 });
  await page.waitForTimeout(800);
  const blocks = await page.locator('.pf-node-block').count();
  if (blocks < 2) throw new Error('node blocks = ' + blocks);
  const gb10Tile = await page.locator('.pf-node-block', { hasText: 'gb10' }).first().locator('.pf-metric-grid').innerText();
  console.log('  节点块:', blocks, '| gb10 含 DCGM 温度:', /\d+\s*°C/.test(gb10Tile), '| 含功耗:', /\d+\s*W/.test(gb10Tile));
  // dell/m2s2 已部署 node-agent（vmagent+DCGM），温度/功耗必须出数
  for (const n of ['dell-shili-7960', 'm2s2NasUbuntuVM-shili-dev']) {
    const tile = await page.locator('.pf-node-block', { hasText: n }).first().locator('.pf-metric-grid').innerText();
    if (!/\d+\s*°C/.test(tile) || !/\d+\s*W/.test(tile))
      throw new Error(n + ' tile missing DCGM temp/power — node-agent not delivering');
    console.log(' ', n, '| DCGM 温度+功耗:', /\d+\s*°C/.test(tile) && /\d+\s*W/.test(tile), '| 推测解码:', /\d+\.\d\s*%/.test(tile) && tile.includes('推测解码'), '| 缺失 — 计数:', (tile.match(/—/g) || []).length);
  }
  await page.screenshot({ path: '/tmp/e2e/prod-nodes.png' });

  /* ═══ 请求与用量（真实数据） ═══ */
  await page.goto(BASE + '/console/usage.html', { waitUntil: 'domcontentloaded' });
  console.log('== 3. 请求与用量:');
  await page.waitForSelector('#ug-kpis .pf-kpi', { timeout: 20000 });
  await page.waitForTimeout(800);
  const sumKpis = await page.locator('#ug-kpis .pf-kpi').count();
  if (sumKpis !== 5) throw new Error('summary KPI = ' + sumKpis);
  const kpiText = await page.locator('#ug-kpis').innerText();
  console.log('  汇总 KPI:', kpiText.replace(/\n/g, ' ').slice(0, 150));
  await page.screenshot({ path: '/tmp/e2e/prod-usage-sum.png' });

  await page.locator('#ug-range button[data-r="7"]').click();
  await page.waitForTimeout(3500);
  const kpi7d = await page.locator('#ug-kpis').innerText();
  console.log('  7d 汇总:', kpi7d.replace(/\n/g, ' ').slice(0, 120));

  await page.locator('.pf-tab[data-tab="rec"]').click();
  await page.waitForSelector('#ug-tbody tr', { timeout: 20000 });
  await page.waitForTimeout(800);
  const recRows = await page.locator('#ug-tbody tr').count();
  if (recRows !== 20) throw new Error('records first page rows = ' + recRows);
  const recHead = await page.locator('#ug-tbody tr').first().innerText();
  console.log('  明细首屏 20 行 | 首行含节点:', /gb10|dell|m2s2/i.test(recHead), '| 含端点:', recHead.includes('/v1/'));
  await page.screenshot({ path: '/tmp/e2e/prod-usage-rec.png' });

  /* ═══ 我的用量 / 其余页面冒烟（新主题不回归） ═══ */
  console.log('== 4. 其余页面冒烟:');
  for (const p of ['my-usage.html', 'keys.html', 'sites.html', 'models.html', 'mcp.html', '2fa.html']) {
    const resp = await page.goto(BASE + '/console/' + p);
    if (resp.status() !== 200) throw new Error(p + ' -> ' + resp.status());
    await page.waitForTimeout(500);
  }
  console.log('  6 页全部 200');

  console.log('\nPAGEERRORS:', errors.length ? errors.join('\n') : 'none');
  if (errors.length) process.exit(1);
  console.log('PROD E2E PASS');
  await browser.close();
})().catch(error => { console.error('FAIL:', error.message || error); process.exit(1); });
