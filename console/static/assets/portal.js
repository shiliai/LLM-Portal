/* ============================================================
   远程模型网关控制台（live）· 壳注入 + 会话守卫 + 交互助手
   由高保真原型 assets/portal.js 接线改造：导航表与助手函数保持，
   会话分为管理员（邮箱+密码+可选 TOTP）与用户虚拟 Key 两种角色。
   契约：
   - 页面写 <body data-page="key"> + <template id="page">；本脚本注入侧边栏/顶栏。
   - 页面业务脚本等待 window.pfReady（会话就绪，resolve {role, alias, ...}）。
   - 数据请求一律走 pfApi(method, path, body?)：自动带 X-Requested-With（CSRF）
     与 JSON 处理，非 2xx 抛 {status, error}。
   - login 等无壳页面：不写 data-page，助手函数仍可用。
   ============================================================ */
(function () {
  'use strict';
  var API = '/console/api';

  /* ---------- 数据请求助手 ---------- */
  async function pfApi(method, path, body) {
    var opt = { method: method, headers: { 'X-Requested-With': 'XMLHttpRequest' } };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    var r = await fetch(API + path, opt);
    var data = null;
    try { data = await r.json(); } catch (e) { /* 空响应 */ }
    if (!r.ok) throw { status: r.status, error: (data && data.error) || (r.status + ' ' + r.statusText) };
    return data;
  }
  window.pfApi = pfApi;

  /* ---------- 导航表（key → 标题 → 文件 → 图标） ---------- */
  function icon(paths) {
    return '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">' + paths + '</svg>';
  }
  var NAV = [
    { key: 'dashboard', title: '仪表盘', file: 'index.html', admin: true,
      icon: icon('<rect x="1.8" y="1.8" width="5.2" height="5.2" rx="1"/><rect x="9" y="1.8" width="5.2" height="5.2" rx="1"/><rect x="1.8" y="9" width="5.2" height="5.2" rx="1"/><rect x="9" y="9" width="5.2" height="5.2" rx="1"/>') },
    { key: 'sites', title: '站点与公钥', file: 'sites.html', admin: true,
      icon: icon('<circle cx="4.6" cy="4.6" r="2.4"/><circle cx="11.4" cy="11.4" r="2.4"/><path d="M6.4 6.4l3.2 3.2"/><path d="M2.4 11.4h2.8M11.4 1.8v2.8"/>') },
    { key: 'groups', title: '分组', file: 'groups.html', admin: true,
      icon: icon('<rect x="1.8" y="3.4" width="7.4" height="7.4" rx="1.2"/><rect x="6.8" y="6" width="7.4" height="7.4" rx="1.2"/>') },
    { key: 'models', title: '模型与别名', file: 'models.html', admin: true,
      icon: icon('<path d="M1.8 4.5h3.4l5 7h4"/><path d="M1.8 11.5h3.4M10.2 4.5h4"/><path d="M12.4 2.7l1.8 1.8-1.8 1.8M12.4 9.7l1.8 1.8-1.8 1.8"/>') },
    { key: 'keys', title: '用户 Key', file: 'keys.html', admin: true,
      icon: icon('<circle cx="5" cy="8" r="2.6"/><path d="M7.6 8h6.6M11.4 8v2.4M14.2 8v1.7"/>') },
    { key: 'usage', title: '用量总览', file: 'usage.html', admin: true,
      icon: icon('<path d="M2.4 13.6V8.4M6.4 13.6V4.4M10.4 13.6V6.8M14 13.6V2.4"/>') },
    { key: 'my-usage', title: '我的用量', file: 'my-usage.html', admin: false,
      icon: icon('<circle cx="8" cy="5.4" r="2.6"/><path d="M2.6 14c0-2.9 2.4-4.6 5.4-4.6s5.4 1.7 5.4 4.6"/>') },
    { key: 'mcp', title: 'MCP 管理', file: 'mcp.html', admin: true,
      icon: icon('<rect x="1.8" y="3" width="12.4" height="10" rx="1.4"/><path d="M4.6 6.4l2 1.8-2 1.8M8.6 10h2.8"/>') },
    { key: 'security', title: '安全设置', file: '2fa.html', admin: true,
      icon: icon('<path d="M8 1.8l4.6 1.8v3.6c0 3-1.9 5.6-4.6 6.8-2.7-1.2-4.6-3.8-4.6-6.8V3.6z"/><path d="M5.9 8l1.5 1.5 2.7-2.7"/>') }
  ];

  /* ---------- 会话守卫 + 壳注入 ---------- */
  var sessionResolve, sessionReject;
  window.pfReady = new Promise(function (res, rej) { sessionResolve = res; sessionReject = rej; });

  async function fetchSession() {
    try {
      return await pfApi('GET', '/me');
    } catch (e) {
      if (e.status === 401) { location.href = '/console/login.html'; throw e; }
      throw e;
    }
  }

  function buildShell(sess) {
    var pageKey = document.body.dataset.page;
    var tpl = document.getElementById('page');
    if (!pageKey || !tpl) return;

    var current = null;
    var navHtml = '';
    NAV.forEach(function (n) {
      if (sess.role !== 'admin' && n.admin) return;
      var active = n.key === pageKey;
      if (active) current = n;
      navHtml += '<a class="pf-nav-item' + (active ? ' active' : '') + '" href="' + n.file + '">' + n.icon + '<span>' + n.title + '</span></a>';
    });
    var title = current ? current.title : '远程模型网关';
    var who = sess.role === 'admin' ? '管理员' : (sess.alias || '用户');
    var sub = sess.role === 'admin' ? '邮箱登录' : ('Key …' + (sess.key_last4 || ''));

    var layout = document.createElement('div');
    layout.className = 'pf-layout';
    layout.innerHTML =
      '<aside class="pf-sidebar">' +
        '<div class="pf-logo">' +
          '<span class="pf-logo-mark"><svg viewBox="0 0 64 64" width="100%" height="100%"><rect x="4.5" y="4.5" width="55" height="55" rx="9.5" fill="#0b60f5"/><path d="M13.3 13H31.5A5.5 5.5 0 0 1 37 18.5V29H35.3V35H37V45.5A5.5 5.5 0 0 1 31.5 51H13.3A5.5 5.5 0 0 1 7.8 45.5V18.5A5.5 5.5 0 0 1 13.3 13Z" fill="#041d66"/><path d="M14.3 19.5 30.4 32M14.3 32H30.4M14.3 44.5 30.4 32" stroke="#fff" stroke-width="3.1" stroke-linecap="round" fill="none"/><path d="M30.4 32H50" stroke="#fff" stroke-width="4.6" stroke-linecap="round" fill="none"/><circle cx="14.3" cy="19.5" r="3.6" fill="#fff"/><circle cx="14.3" cy="32" r="3.6" fill="#fff"/><circle cx="14.3" cy="44.5" r="3.6" fill="#fff"/><circle cx="50" cy="32" r="4.4" fill="#fff"/></svg></span>' +
          '<span class="pf-logo-text">远程模型网关</span>' +
        '</div>' +
        '<nav class="pf-nav">' + navHtml + '</nav>' +
        '<div class="pf-sidebar-foot">' +
          '<span class="pf-avatar sm">' + (sess.role === 'admin' ? 'A' : 'U') + '</span>' +
          '<div><div class="pf-admin-name">' + who + '</div><div class="pf-admin-sub">' + sub + '</div></div>' +
          '<button class="pf-btn sm ghost" id="pf-logout" style="margin-left:auto" type="button">注销</button>' +
        '</div>' +
      '</aside>' +
      '<div class="pf-main">' +
        '<header class="pf-topbar">' +
          '<div class="pf-topbar-title">' + title + '</div>' +
          '<div class="pf-topbar-right"><span class="pf-avatar">' + (sess.role === 'admin' ? 'A' : 'U') + '</span></div>' +
        '</header>' +
        '<main class="pf-content"></main>' +
      '</div>';

    var content = layout.querySelector('.pf-content');
    content.appendChild(tpl.content);
    tpl.remove();
    document.body.insertBefore(layout, document.body.firstChild);
    layout.querySelector('#pf-logout').addEventListener('click', async function () {
      try { await pfApi('POST', '/logout'); } catch (e) { /* 忽略 */ }
      location.href = '/console/login.html';
    });
  }

  /* ---------- 遮罩 + 抽屉/弹窗 ---------- */
  var mask = null;
  var openEl = null;

  function ensureMask() {
    if (!mask) {
      mask = document.createElement('div');
      mask.className = 'pf-mask';
      mask.addEventListener('click', closeOverlay);
      document.body.appendChild(mask);
    }
    return mask;
  }
  function openOverlay(el) {
    if (!el) return;
    if (openEl && openEl !== el) openEl.classList.remove('open');
    openEl = el;
    ensureMask().classList.add('show');
    el.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeOverlay() {
    if (openEl) openEl.classList.remove('open');
    openEl = null;
    if (mask) mask.classList.remove('show');
    document.body.style.overflow = '';
  }

  function pfDrawer(id) { openOverlay(document.getElementById(id)); }
  function pfDrawerClose() { closeOverlay(); }
  function pfModal(id) { openOverlay(document.getElementById(id)); }
  function pfModalClose() { closeOverlay(); }

  /* ---------- Toast ---------- */
  var toastBox = null;
  function pfToast(msg) {
    if (!toastBox) {
      toastBox = document.createElement('div');
      toastBox.className = 'pf-toasts';
      document.body.appendChild(toastBox);
    }
    var t = document.createElement('div');
    t.className = 'pf-toast';
    t.innerHTML = '<span class="pf-toast-icon">✓</span><span></span>';
    t.lastElementChild.textContent = msg || '操作成功';
    toastBox.appendChild(t);
    requestAnimationFrame(function () { t.classList.add('show'); });
    setTimeout(function () {
      t.classList.remove('show');
      setTimeout(function () { t.remove(); }, 300);
    }, 2000);
  }
  function pfErr(msg) {
    if (!toastBox) pfToast('');
    var t = document.createElement('div');
    t.className = 'pf-toast';
    t.innerHTML = '<span class="pf-toast-icon" style="color:#c0392b">✕</span><span></span>';
    t.lastElementChild.textContent = msg || '操作失败';
    toastBox.appendChild(t);
    requestAnimationFrame(function () { t.classList.add('show'); });
    setTimeout(function () {
      t.classList.remove('show');
      setTimeout(function () { t.remove(); }, 300);
    }, 2600);
  }

  /* ---------- Tabs ---------- */
  function pfTabs(container) {
    if (typeof container === 'string') container = document.querySelector(container);
    if (!container) return;
    var bar = container.classList.contains('pf-tabs') ? container : container.querySelector('.pf-tabs');
    if (!bar || bar.dataset.pfBound) return;
    bar.dataset.pfBound = '1';
    var scope = bar.parentElement;

    function activate(key) {
      bar.querySelectorAll('.pf-tab').forEach(function (t) {
        t.classList.toggle('active', t.dataset.tab === key);
      });
      Array.prototype.forEach.call(scope.children, function (ch) {
        if (ch.classList && ch.classList.contains('pf-tabpane')) {
          ch.classList.toggle('active', ch.dataset.tab === key);
        }
      });
    }
    bar.addEventListener('click', function (e) {
      var tab = e.target.closest('.pf-tab');
      if (tab && bar.contains(tab)) activate(tab.dataset.tab);
    });
    var init = bar.querySelector('.pf-tab.active') || bar.querySelector('.pf-tab');
    if (init) activate(init.dataset.tab);
  }

  /* ---------- .pf-code 复制按钮 ---------- */
  function initCodeBlocks() {
    document.querySelectorAll('.pf-code').forEach(function (block) {
      if (block.querySelector('.pf-code-copy')) return;
      var btn = document.createElement('button');
      btn.className = 'pf-code-copy';
      btn.type = 'button';
      btn.textContent = '复制';
      block.appendChild(btn);
    });
  }

  /* ---------- 声明式绑定（事件委托） ---------- */
  function initDelegation() {
    document.addEventListener('click', function (e) {
      var el;
      if ((el = e.target.closest('.pf-code-copy'))) {
        var pre = el.closest('.pf-code').querySelector('pre');
        var text = pre ? pre.textContent : '';
        try {
          if (navigator.clipboard) navigator.clipboard.writeText(text);
        } catch (err) { /* 忽略 */ }
        pfToast('已复制');
        return;
      }
      if ((el = e.target.closest('[data-drawer]'))) { pfDrawer(el.dataset.drawer); return; }
      if ((el = e.target.closest('[data-modal]')))  { pfModal(el.dataset.modal); return; }
      if ((el = e.target.closest('[data-close]')))  { closeOverlay(); return; }
      if ((el = e.target.closest('[data-toast]')))  { pfToast(el.dataset.toast); return; }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeOverlay();
    });
  }

  /* ---------- 启动 ---------- */
  document.addEventListener('DOMContentLoaded', async function () {
    var pageKey = document.body.dataset.page;
    if (pageKey) {                                    // 有壳页面先过会话守卫
      try {
        var sess = await fetchSession();
        if (sess.role !== 'admin' && pageKey !== 'my-usage') {
          location.href = 'my-usage.html';
          return;
        }
        buildShell(sess);
        document.querySelectorAll('.pf-tabs').forEach(function (bar) { pfTabs(bar); });
        initCodeBlocks();
        initDelegation();
        sessionResolve(sess);
      } catch (e) { /* 已跳转 login 或失败静默 */ }
    } else {
      initCodeBlocks();
      initDelegation();
      sessionResolve(null);
    }
  });

  /* ---------- 全局导出 ---------- */
  window.pfTabs = pfTabs;
  window.pfDrawer = pfDrawer;
  window.pfDrawerClose = pfDrawerClose;
  window.pfModal = pfModal;
  window.pfModalClose = pfModalClose;
  window.pfToast = pfToast;
  window.pfErr = pfErr;
})();
