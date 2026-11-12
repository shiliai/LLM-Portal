/* Real browser -> console -> shared DB <- compat -> controlled HTTP backend.
   No production services, synthetic credentials only; starts/stops its own stack. */
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const assert = require('assert/strict');
const ROOT = path.resolve(__dirname, '../..');
const PYTHON = process.env.PYTHON || 'python3';
const delay = ms => new Promise(r => setTimeout(r, ms));
async function port() {
  const server = net.createServer();
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const value = server.address().port;
  await new Promise(r => server.close(r));
  return value;
}
async function until(check, label) {
  for (let i=0;i<150;i++) { if (await check()) return; await delay(100); }
  throw new Error('Timed out: ' + label);
}
(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'portal-live-e2e-'));
  const output = path.join(ROOT, 'output/playwright/queue');
  fs.mkdirSync(output, {recursive:true});
  const [backendPort, consolePort, compatPort] = await Promise.all([port(),port(),port()]);
  const upstream='http://127.0.0.1:'+backendPort, base='http://127.0.0.1:'+consolePort;
  const env = {...process.env, PYTHONPATH:ROOT, QUEUE_FIXTURE_PORT:String(backendPort),
    CONSOLE_PORT:String(consolePort), COMPAT_PORT:String(compatPort),
    CONSOLE_DATA:path.join(tmp,'console'), CONVERSATION_MONITOR_DB:path.join(tmp,'monitor.db'),
    LIVE_REQUESTS_DB:path.join(tmp,'live.db'), LITELLM_BASE:upstream, ONBOARDD_URL:upstream,
    LITELLM_MASTER_KEY:'sk-e2e-master', ONBOARD_ADMIN_TOKEN:'fixture',
    ADMIN_EMAIL:'admin@test.local', ADMIN_PASSWORD:'test-pass-1', ADMIN_TOTP_SECRET:'', SESSION_COOKIE_SECURE:'false'};
  const children=[], requests=[];
  let browser,page;
  function start(args, cwd) {
    const child=spawn(PYTHON,args,{cwd,env,stdio:['ignore','pipe','pipe']});
    let log='';child.stdout.on('data',v=>log+=v);child.stderr.on('data',v=>log+=v);
    children.push({child,log:()=>log});
  }
  try {
    start(['-m','uvicorn','queue_fixture:app','--host','127.0.0.1','--port',String(backendPort)],__dirname);
    start([path.join(ROOT,'console/console.py')],ROOT);
    start([path.join(ROOT,'compat/compat_proxy.py')],ROOT);
    await until(async()=>{try{return (await fetch(upstream+'/test/state')).ok&&(await fetch(base+'/console/admin-login.html')).ok;}catch{return false;}},'services');
    await until(async()=>{try{return (await fetch('http://127.0.0.1:'+compatPort+'/not-found')).status===404;}catch{return false;}},'compat');
    const launch={};
    if(process.env.PLAYWRIGHT_EXECUTABLE_PATH)launch.executablePath=process.env.PLAYWRIGHT_EXECUTABLE_PATH;
    browser=await chromium.launch(launch);
    page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/console/admin-login.html');
    await page.fill('#lg-email',env.ADMIN_EMAIL);await page.fill('#lg-pwd',env.ADMIN_PASSWORD);
    await page.click('#lg-go');await page.waitForURL('**/console/');
    // Exercise navigation, not just a direct page load.
    await page.getByRole('link',{name:'活动任务',exact:true}).click();
    await page.waitForSelector('#rq-rows');
    for(let i=0;i<12;i++)requests.push(fetch('http://127.0.0.1:'+compatPort+'/v1/chat/completions',{
      method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer sk-queue-browser','User-Agent':'queue-browser/1.0 <img src=x onerror=window.__uaXss=1>'},
      body:JSON.stringify({model:i===11?'test-alias':'test-deepseek',stream:true,max_tokens:64,messages:[{role:'user',content:'fixture'}]})
    }).then(async r=>{assert.equal(r.status,200);return r.text();}));
    await until(async()=>(await (await fetch(upstream+'/test/state')).json()).pending===12,'12 pending');
    await page.click('#rq-refresh');
    await page.waitForFunction(()=>document.querySelector('#rq-active').textContent==='12');
    assert.equal(await page.locator('#rq-rows tr').count(),12);
    assert.match(await page.locator('#rq-rows').innerText(),/等待响应 · 排队状态未知/);
    assert.match(await page.locator('#rq-rows').innerText(),/browser-user/);
    await page.waitForFunction(()=>document.querySelector('#rq-backends').textContent.includes('12'));
    assert.equal(await page.locator('#rq-backends tr').count(),1,'same backend aliases deduplicated');
    assert(await page.locator('#rq-backends tr td').nth(1).evaluate(el=>el.scrollWidth<=el.clientWidth),
      'long backend model aliases must wrap inside their cell without overlapping counters');
    await page.fill('#rq-search','test-alias');assert.equal(await page.locator('#rq-rows tr').count(),1);
    await page.fill('#rq-search','not-a-model');assert.match(await page.locator('#rq-rows').innerText(),/没有符合/);
    await page.fill('#rq-search','');
    await page.fill('#rq-search','queue-browser/1.0');assert.equal(await page.locator('#rq-rows tr').count(),12);
    await page.fill('#rq-search','');
    await page.locator('#rq-rows button[data-request-id]').first().click();
    const detailId=await page.locator('#rq-detail-fields dd').first().innerText();
    await page.waitForFunction(()=>document.querySelector('#rq-detail-fields')?.textContent.includes('queue-browser/1.0'));
    assert.match(await page.locator('#rq-detail-fields').innerText(),/待后端返回/);
    assert.match(await page.locator('#rq-detail-fields').innerText(),/64/);
    assert.equal(await page.evaluate(()=>window.__uaXss),undefined,'User-Agent must render as text');
    await page.click('#rq-detail-close');
    await page.screenshot({path:path.join(output,'pending-dark.png'),fullPage:true});
    // Node counters are direct, accessible links, including zero; unknown
    // metrics remain non-interactive. Only overview/history are substituted;
    // the destination uses the real console APIs and compat request metadata.
    await page.route('**/console/api/overview',r=>r.fulfill({json:{sites:{rows:[
      {name:'test-gb10',status:'online',metrics:{requests_running:0,requests_waiting:12}},
      {name:'empty-node',status:'online',metrics:{requests_running:0,requests_waiting:0}},
      {name:'unknown-node',status:'online',metrics:{}}
    ]}}}));
    await page.route('**/console/api/metrics/range?**',r=>r.fulfill({json:{points:[]}}));
    await page.getByRole('link',{name:'节点性能',exact:true}).click();
    await page.waitForSelector('#nm-test-gb10 a');
    assert.equal(await page.locator('#nm-unknown-node a').count(),0);
    assert.equal(await page.locator('#nm-empty-node a').count(),2);
    const active=page.getByRole('link',{name:'查看 test-gb10 的活动概览与关联请求',exact:true});
    assert.equal(await active.innerText(),'0');
    await active.focus();await page.keyboard.press('Enter');
    await page.waitForFunction(()=>document.querySelector('#rq-rows')?.textContent.includes('browser-user'));
    assert.equal(new URL(page.url()).searchParams.get('site'),'test-gb10');
    assert.equal(await page.locator('#rq-phase').inputValue(),'');
    assert.equal(await page.locator('#rq-rows tr').count(),12);
    assert.match(await page.locator('#rq-context').innerText(),/实际执行节点与后端排队状态未确认/);
    await page.goBack();await page.waitForSelector('#nm-test-gb10 a');
    await page.getByRole('link',{name:'查看 test-gb10 的排队概览与等待响应请求',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#rq-rows')?.textContent.includes('browser-user'));
    assert.equal(await page.locator('#rq-phase').inputValue(),'awaiting_response');
    assert.equal(await page.locator('#rq-backends tr').count(),1);
    assert.match(await page.locator('#rq-backends').innerText(),/test-gb10/);
    await page.selectOption('#rq-phase','receiving_response');
    assert.match(await page.locator('#rq-rows').innerText(),/没有符合/);
    await page.reload();await page.waitForFunction(()=>document.querySelector('#rq-phase')?.value==='receiving_response');
    await page.selectOption('#rq-phase','awaiting_response');
    await page.waitForFunction(()=>document.querySelector('#rq-rows')?.textContent.includes('browser-user'));
    await page.screenshot({path:path.join(output,'node-queue-entry.png'),fullPage:true});
    await page.getByRole('link',{name:'节点性能',exact:true}).click();
    await page.waitForSelector('#nm-empty-node a');
    await page.getByRole('link',{name:'查看 empty-node 的排队概览与等待响应请求',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#rq-backend-status')?.textContent.includes('更新于'));
    assert.match(await page.locator('#rq-rows').innerText(),/没有符合/);
    assert.match(await page.locator('#rq-backends').innerText(),/没有已注册/);
    await page.getByRole('link',{name:'查看全部节点',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#rq-active')?.textContent==='12');
    assert(await page.locator('#rq-context').isHidden());
    assert.equal(await page.locator('#rq-rows tr').count(),12);
    // Existing data becomes visibly unknown on a failed refresh.
    await page.route('**/console/api/requests/live',r=>r.fulfill({status:503,json:{error:'fixture unavailable'}}));
    await page.click('#rq-refresh');await page.waitForFunction(()=>document.querySelector('#rq-status').textContent.includes('刷新失败'));
    assert.equal(await page.locator('#rq-active').innerText(),'—');
    await page.unroute('**/console/api/requests/live');
    await page.click('#rq-refresh');await page.waitForFunction(()=>document.querySelector('#rq-active').textContent==='12');
    // Hold streamed responses open; entries must survive the response headers.
    await fetch(upstream+'/test/release',{method:'POST'});
    await page.click('#rq-refresh');
    await page.waitForFunction(()=>document.querySelector('#rq-responding').textContent==='12');
    await page.selectOption('#rq-phase','receiving_response');
    assert.equal(await page.locator('#rq-rows tr').count(),12);
    await page.locator('#rq-rows button[data-request-id]').first().click();
    await page.waitForFunction(()=>document.querySelector('#rq-detail-fields')?.textContent.includes('1,234'));
    assert.match(await page.locator('#rq-detail-fields').innerText(),/256/);
    await page.click('#rq-detail-close');
    await page.setViewportSize({width:780,height:1000});
    await page.evaluate(()=>document.documentElement.setAttribute('data-theme','light'));
    await page.screenshot({path:path.join(output,'receiving-light-narrow.png'),fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'page must not overflow viewport');
    await page.locator('#rq-rows button[data-request-id]').first().click();
    await fetch(upstream+'/test/end',{method:'POST'});
    const results=await Promise.all(requests);assert(results.every(r=>r.includes('[DONE]')));
    await page.waitForFunction(()=>document.querySelector('#rq-detail-status')?.textContent.includes('已保存到历史'));
    assert(await page.locator('#rq-detail').isVisible(),'details remain open when a request moves to history');
    assert.match(await page.locator('#rq-detail-fields').innerText(),/已完成/);
    assert.match(await page.locator('#rq-detail-fields').innerText(),/1,234/);
    assert.match(await page.locator('#rq-detail-fields').innerText(),/8/);
    await page.screenshot({path:path.join(output,'completed-detail-light-narrow.png'),fullPage:true});
    await page.click('#rq-detail-close');
    await page.selectOption('#rq-phase','');await page.click('#rq-refresh');
    await page.waitForFunction(()=>document.querySelector('#rq-active').textContent==='0');
    assert.match(await page.locator('#rq-rows').innerText(),/当前没有可追踪/);
    await page.waitForFunction(()=>document.querySelectorAll('#rq-history tr button[data-request-id]').length===12);
    assert.match(await page.locator('#rq-history').innerText(),/已完成/);
    assert.match(await page.locator('#rq-history').innerText(),/1,234/);
    assert.match(await page.locator('#rq-history').innerText(),new RegExp(detailId));
    await page.fill('#rq-search','queue-browser');
    await page.waitForFunction(()=>document.querySelectorAll('#rq-history tr button[data-request-id]').length===12);
    await page.selectOption('#rq-outcome','failed');
    await page.waitForFunction(()=>document.querySelector('#rq-history')?.textContent.includes('没有符合'));
    await page.selectOption('#rq-outcome','');await page.fill('#rq-search','');
    await page.waitForFunction(()=>document.querySelectorAll('#rq-history tr button[data-request-id]').length===12);
    await page.reload();
    await page.waitForFunction(()=>document.querySelectorAll('#rq-history tr button[data-request-id]').length===12);
    await page.locator('#rq-history button[data-request-id]').first().click();
    await page.waitForFunction(()=>document.querySelector('#rq-detail-status')?.textContent.includes('已保存到历史'));
    assert.match(await page.locator('#rq-detail-fields').innerText(),/queue-browser/);
    assert.equal(await page.evaluate(()=>window.__uaXss),undefined);
    await page.click('#rq-detail-close');
    await page.screenshot({path:path.join(output,'history-light-narrow.png'),fullPage:true});
    // Missing heartbeat must never look like a healthy empty queue.
    await page.route('**/console/api/requests/live',r=>r.fulfill({json:{available:false,requests:[],stale_requests:[{}],stale_workers:1}}));
    await page.click('#rq-refresh');await page.waitForFunction(()=>document.querySelector('#rq-status').textContent.includes('心跳已过期'));
    assert.equal(await page.locator('#rq-active').innerText(),'—');assert(await page.locator('#rq-stale').isVisible());
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({result:'PASS',pending:12,receiving:12,completed:12,backendAliasesDeduplicated:true,
      checked:['durable history after completion/reload, live/history details, backend token usage, User-Agent search and XSS, result filters','node counter links: keyboard, activity/queue, site and phase context, reload, zero/unknown, clear scope','admin navigation','search','phase filter','live HTTP lifecycle','refresh failure','missing heartbeat','dark/light','narrow layout'],production:false},null,2));
    console.log('PASS: 12 pending -> 12 receiving -> 0 active; filtering, aliases, error/stale states and narrow browser layout.');
  } catch(e) {
    if(page)await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});
    for(const child of children)console.error(child.log().slice(-2000));
    throw e;
  } finally {
    if(browser)await browser.close();
    for(const {child} of children)child.kill('SIGTERM');
    await delay(300);
    for(const {child} of children)if(child.exitCode===null)child.kill('SIGKILL');
    fs.rmSync(tmp,{recursive:true,force:true});
  }
})().catch(e=>{console.error(e);process.exit(1);});
