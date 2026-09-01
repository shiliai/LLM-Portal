/* 真实后端 E2E:本地 console.py + mock LiteLLM(生产数据形状) */
const { chromium } = require('playwright');
const BASE = process.env.CONSOLE_E2E_BASE || 'http://127.0.0.1:8399';
const ADMIN_EMAIL = ['admin', 'test.local'].join('@');
const AUTOFILL_EMAIL = ['autofill', 'example.com'].join('@');

(async () => {
  // PW_CHROME：显式指定本机 Chromium 可执行文件（playwright 缓存 revision 不匹配时用）
  const browser = await chromium.launch(process.env.PW_CHROME ? { executablePath: process.env.PW_CHROME } : {});
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('framenavigated', f => { if (f === page.mainFrame() && !f.url().includes('favicon'))
    console.log('[nav]', f.url().replace(BASE, '')); });

  console.log('== 1. 管理员登录');
  await page.goto(BASE + '/console/admin-login.html');
  await page.fill('#lg-email', ADMIN_EMAIL);
  await page.fill('#lg-pwd', 'test-pass-1');
  await page.click('#lg-go');
  await page.waitForURL('**/console/');
  await page.waitForTimeout(400);

  console.log('== 2. 进 keys 页');
  await page.goto(BASE + '/console/keys.html');
  await page.waitForTimeout(600);
  console.log('rows:', await page.locator('#keys-tbody tr').count(),
    '|', await page.locator('#keys-extra').textContent());

  console.log('== 2b. Chrome 静默 autofill 不得成为筛选状态');
  const initialKeyRows = await page.locator('.js-use-key').count();
  await page.locator('#f-search').evaluate((el, value) => { el.value = value; }, AUTOFILL_EMAIL);
  await page.click('#btn-refresh');
  await page.waitForTimeout(600);
  const rowsAfterSilentAutofill = await page.locator('.js-use-key').count();
  console.log('silent autofill + refresh rows:', rowsAfterSilentAutofill,
    '| search:', JSON.stringify(await page.locator('#f-search').inputValue()));
  if (!initialKeyRows || rowsAfterSilentAutofill !== initialKeyRows) {
    console.error('ASSERT FAIL: 静默 autofill 不应改变 Key 筛选结果'); process.exitCode = 1;
  }

  console.log('== 2c. 真实 input 仍应更新筛选状态');
  await page.fill('#f-search', '不存在的词');
  await page.waitForTimeout(200);
  if (await page.locator('.js-use-key').count()) {
    console.error('ASSERT FAIL: 真实搜索输入应过滤 Key 列表'); process.exitCode = 1;
  }
  await page.fill('#f-search', '');
  await page.waitForTimeout(200);

  console.log('== 3. 点击行内分组下拉(只点开不选择,再点别处关闭)');
  await page.locator('.js-group:not([disabled])').first().click();
  await page.waitForTimeout(300);
  await page.mouse.click(700, 500);
  await page.waitForTimeout(300);
  console.log('after open/close select, rows:', await page.locator('#keys-tbody tr').count());

  console.log('== 4. 切换行内分组 home → default(触发 POST update)');
  await page.locator('.js-group:not([disabled])').first().selectOption('home');
  await page.waitForTimeout(700);
  console.log('after change, rows:', await page.locator('#keys-tbody tr').count(),
    '| first row group now:', await page.locator('.js-group').first().inputValue());

  console.log('== 5. 复现关键场景:搜索框粘完整密钥后 reload(浏览器恢复表单值?)');
  await page.fill('#f-search', 'sk-e2eFixtureKey000000000000');
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
  const initialUseKeyState = await page.locator('#use-key').evaluate((el) => ({
    type: el.type,
    masked: el.classList.contains('use-key-masked'),
    textSecurity: getComputedStyle(el).webkitTextSecurity
  }));
  console.log('modal open:', await page.locator('#modal-usekey.open').count(),
    '| focused is use-key:', await page.evaluate(() => document.activeElement && document.activeElement.id),
    '| field:', JSON.stringify(initialUseKeyState));
  if (initialUseKeyState.type !== 'text' || !initialUseKeyState.masked || initialUseKeyState.textSecurity !== 'disc') {
    console.error('ASSERT FAIL: API Key 输入应保持 text 类型并默认视觉掩码'); process.exitCode = 1;
  }
  await page.screenshot({ path: '/tmp/e2e/r3-usekey.png' });
  await page.keyboard.press('Escape');   // 关闭使用弹窗，避免遮罩挡住后续行内操作

  console.log('== 8. 编辑/禁用/新建全链路');
  console.log('== 编辑弹窗');
  await page.locator('.js-edit-key').first().click();
  await page.waitForTimeout(300);
  console.log('edit modal open:', await page.locator('#modal-editkey.open').count());
  await page.fill('#edit-alias', 'justink-改名');
  await page.click('#btn-editkey');
  await page.waitForTimeout(600);
  const firstAlias = await page.locator('#keys-tbody tr:not(.pf-row-muted) td').first().textContent();
  console.log('alias after edit:', firstAlias);

  console.log('== 禁用/启用');
  await page.locator('.js-toggle-key').first().click();
  await page.waitForTimeout(600);
  const chip = await page.locator('#keys-tbody .pf-chip.gray').count();
  console.log('disabled chips:', chip, '| select disabled:', await page.locator('.js-group[disabled]').count());
  if (chip !== 1) { console.error('ASSERT FAIL: 禁用后应有 1 个「已禁用」chip'); process.exitCode = 1; }

  console.log('== 8b. 状态筛选「已禁用」→ 恰好显示被禁的那把（事件委托 + 状态ful 桩）');
  await page.selectOption('#f-status', 'off');
  await page.waitForTimeout(300);
  const offRows = await page.locator('#keys-tbody tr').count();
  const offText = (await page.locator('#keys-tbody').innerText()).trim();
  console.log('filtered rows:', offRows, '| text head:', JSON.stringify(offText.slice(0, 60)));
  if (offRows !== 1) { console.error('ASSERT FAIL: 「已禁用」筛选应只显示 1 把'); process.exitCode = 1; }

  console.log('== 8c. 无「已禁用」Key 时的空态指引（误点筛选不再像页面坏掉）');
  await page.locator('.js-toggle-key').first().click();   // 重新启用 → 筛选下无结果
  await page.waitForTimeout(600);
  const emptyHint = (await page.locator('#keys-tbody').innerText()).trim();
  console.log('empty hint:', JSON.stringify(emptyHint));
  if (!emptyHint.includes('当前没有「已禁用」的 Key') || !emptyHint.includes('禁用」按钮')) {
    console.error('ASSERT FAIL: 空态应给出「如何禁用」的指引'); process.exitCode = 1;
  }
  await page.screenshot({ path: '/tmp/e2e/r3b-empty-hint.png' });
  await page.selectOption('#f-status', '');

  console.log('== 8d. 通用无匹配空态');
  await page.fill('#f-search', '不存在的词');
  await page.waitForTimeout(300);
  const noMatch = (await page.locator('#keys-tbody').innerText()).trim();
  if (!noMatch.includes('当前筛选')) { console.error('ASSERT FAIL: 通用空态文案缺失'); process.exitCode = 1; }
  console.log('no-match hint:', JSON.stringify(noMatch.slice(0, 50)));
  await page.fill('#f-search', '');
  await page.waitForTimeout(200);

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
  for (const tab of ['Codex CLI', 'OpenAI 兼容', 'DeepSeek Harness', 'pi']) {
    await page.locator(`.pf-tabs > .pf-tab:has-text("${tab}")`).first().click();
    await page.waitForTimeout(150);
  }
  const adversarialKey = "sk-$(printf INJECTED)'tail";
  await page.fill('#use-key', adversarialKey);
  const posixExport = await page.locator('#codex-nix-term').textContent();
  const powershellExport = await page.locator('#codex-win-term').textContent();
  if (posixExport !== `export PLL_API_KEY='sk-$(printf INJECTED)'\"'\"'tail'`
      || powershellExport !== `$env:PLL_API_KEY = 'sk-$(printf INJECTED)''tail'`) {
    console.error('ASSERT FAIL: shell 配置必须把粘贴内容序列化为纯字符串'); process.exitCode = 1;
  }
  await page.fill('#use-key', fullKey.trim());
  const piJson = JSON.parse(await page.locator('#pi-json').textContent());
  const piSettings = JSON.parse(await page.locator('#pi-settings-json').textContent());
  const piProvider = piJson.providers['private-llm'];
  const piModel = piProvider.models.find((model) => model.id === 'deepseek-v4-flash-0731');
  const piQwen = piProvider.models.find((model) => model.id === 'qwen3.8-27b');
  const piLevels = Object.entries(piModel.thinkingLevelMap)
    .filter(([, mapped]) => mapped !== null)
    .map(([level]) => level);
  console.log('pi config:', {
    hasKey: piProvider.apiKey === fullKey.trim(),
    baseUrl: piProvider.baseUrl,
    api: piProvider.api,
    models: piProvider.models.map((model) => model.id),
    levels: piLevels,
    defaultThinking: piSettings.defaultThinkingLevel
  });
  if (piProvider.apiKey !== fullKey.trim()
      || !piProvider.baseUrl.endsWith('/v1')
      || piProvider.api !== 'openai-completions'
      || piProvider.compat.supportsReasoningEffort !== true
      || piProvider.compat.requiresReasoningContentOnAssistantMessages !== true
      || piModel.reasoning !== true
      || JSON.stringify(piLevels) !== JSON.stringify(['high', 'max'])
      || !piQwen
      || piQwen.reasoning !== true
      || JSON.stringify(piQwen.input) !== JSON.stringify(['text', 'image', 'video'])
      || piQwen.contextWindow !== 262144
      || piQwen.maxTokens !== 32768
      || piSettings.defaultProvider !== 'private-llm'
      || piSettings.defaultModel !== 'deepseek-v4-flash-0731'
      || piSettings.defaultThinkingLevel !== 'high') {
    console.error('ASSERT FAIL: Pi 配置必须包含能力准确的 Qwen 且保持 DeepSeek 默认与 high/max effort'); process.exitCode = 1;
  }
  const dshCredentials = await page.locator('#dsh-credentials-yaml').textContent();
  const dshSettings = await page.locator('#dsh-settings-yaml').textContent();
  const dshChecks = [
    dshCredentials === `LLM_PORTAL_API_KEY: "${fullKey.trim()}"`,
    dshSettings.includes(`baseURL: "${BASE}/v1"`),
    dshSettings.includes('api: openai-completions'),
    dshSettings.includes('supportsReasoningEffort: true'),
    dshSettings.includes('reasoning: high'),
    dshSettings.includes('contextWindow: 1048576'),
    dshSettings.includes('maxTokens: 32768'),
    dshSettings.includes('reasoningEfforts:\n            high: high\n            max: max'),
    dshSettings.includes('        - id: qwen3.8-27b\n          name: "Private Qwen3.8 27B"\n          contextWindow: 262144\n          maxTokens: 32768'),
    dshSettings.includes('agent-default-model:\n  provider: private-llm\n  model: deepseek-v4-flash-0731\n  reasoningEffort: high'),
    !dshSettings.includes('thinkingFormat:')
  ];
  console.log('dsh config checks:', dshChecks);
  if (dshChecks.some((ok) => !ok)) {
    console.error('ASSERT FAIL: dsh 配置必须包含能力准确的 Qwen 且保持 DeepSeek 默认'); process.exitCode = 1;
  }
  const dshTab = page.locator('.pf-tabs > .pf-tab:has-text("DeepSeek Harness")').first();
  await dshTab.click();
  await page.screenshot({ path: '/tmp/e2e/r5-dsh-config.png' });

  console.log('== 窄屏客户端 tabs 与 dsh 配置');
  await page.setViewportSize({ width: 390, height: 844 });
  await dshTab.scrollIntoViewIfNeeded();
  await dshTab.click();
  const dshPaneVisible = await page.locator('.pf-tabpane[data-tab="dsh"]:visible').count();
  const dshTabBox = await dshTab.boundingBox();
  const useModalBox = await page.locator('#modal-usekey').boundingBox();
  const dshTabFitsModal = dshTabBox && useModalBox
    && dshTabBox.x >= useModalBox.x
    && dshTabBox.x + dshTabBox.width <= useModalBox.x + useModalBox.width;
  console.log('mobile dsh:', { paneVisible: dshPaneVisible, tabFitsModal: dshTabFitsModal });
  if (!dshPaneVisible || !dshTabFitsModal) {
    console.error('ASSERT FAIL: 窄屏下必须能滚动选择并完整显示 DeepSeek Harness 标签'); process.exitCode = 1;
  }
  await page.screenshot({ path: '/tmp/e2e/r6-dsh-mobile.png' });

  console.log('== 密钥粘贴框显示/隐藏');
  await page.click('#btn-use-show');
  const useKeyType = await page.locator('#use-key').getAttribute('type');
  const useKeyMasked = await page.locator('#use-key').evaluate((el) => el.classList.contains('use-key-masked'));
  console.log('after show: type:', useKeyType, '| masked:', useKeyMasked);
  if (useKeyType !== 'text' || useKeyMasked) {
    console.error('ASSERT FAIL: 显示密钥应只移除视觉掩码，输入类型保持 text'); process.exitCode = 1;
  }

  
  console.log('\n== ERRORS:', errors.length ? '\n' + errors.join('\n') : 'none');
  await browser.close();
})();
