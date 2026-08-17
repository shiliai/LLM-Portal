/* 高保真原型 mock：拦截 /console/api 请求喂假数据，免起后端即可在真实壳里演示。
   必须在 portal.js 之前加载。数据与 console e2e 夹具一致（workstation 站点）。 */
(function () {
  'use strict';
  var SITE = {
    name: 'workstation',
    pubkey: 'wg-pub-7f3a…9k2e',
    wg_ip: '10.77.0.14',
    handshake: 42,
    status: 'active', online: true,
    groups: ['lab'],
    models: [{ name: 'qwen3.6-35b-fp8', port: 8004 }, { name: 'deepseek-v4-flash', port: 8890 }],
    known_ports: [8004, 8890],
    deps: [
      { name: 'qwen3.6-35b-fp8', port: 8004, upstream: 'qwen3.6-35b-fp8' },
      { name: 'deepseek-v4-flash', port: 8890, upstream: 'deepseek-v4-flash-0731' }
    ]
  };
  var PROBE = {
    8004: [{ id: 'qwen3.8-27b-mtp2', owned_by: 'vllm' }, { id: 'qwen3.6-35b-fp8', owned_by: 'vllm' }],
    8890: [{ id: 'deepseek-v4-flash-0731', owned_by: 'llama.cpp' }]
  };
  window.MOCK = { SITE: SITE, PROBE: PROBE };

  function json(data) {
    return Promise.resolve(new Response(JSON.stringify(data), {
      status: 200, headers: { 'Content-Type': 'application/json' }
    }));
  }
  var realFetch = window.fetch;
  window.fetch = function (url) {
    var u = String(url);
    if (u === '/console/api/me') return json({ role: 'admin', alias: 'admin' });
    if (u === '/console/api/sites') return json({ sites: [SITE] });
    var m = u.match(/\/console\/api\/sites\/probe\?site=([^&]+)&port=(\d+)/);
    if (m) {
      var port = parseInt(m[2], 10);
      return new Promise(function (res) {
        setTimeout(function () {
          if (PROBE[port]) {
            res(new Response(JSON.stringify({ models: PROBE[port] }),
              { status: 200, headers: { 'Content-Type': 'application/json' } }));
          } else {
            // 手填端口 → 模拟探测失败，演示降级手填路径
            res(new Response(JSON.stringify({ error: '连接 10.77.0.14:' + port + ' 超时' }),
              { status: 502, headers: { 'Content-Type': 'application/json' } }));
          }
        }, 500);
      });
    }
    if (u.indexOf('/console/api/') === 0) return json({ ok: true });   // 增删改一律假成功
    return realFetch.apply(this, arguments);
  };
})();
