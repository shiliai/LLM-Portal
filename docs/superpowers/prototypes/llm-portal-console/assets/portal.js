/* ============================================================
   LLM-portal 控制台原型 · 壳注入 + 交互助手
   契约（BRIEF §4）：
   - 控制台页面写 <body data-page="key"> + <template id="page">内容</template>，
     本脚本注入侧边栏/顶栏并把模板内容放入 .pf-content。
   - login/wizard 等无壳页面：不写 data-page 或 #page 即可，壳不注入，
     但助手函数与 data-* 自动绑定仍然可用。
   - 全局助手：pfTabs(el) / pfDrawer(id) / pfDrawerClose()
              / pfModal(id) / pfModalClose() / pfToast(msg)
   - 声明式绑定：[data-drawer="id"] 打开抽屉、[data-modal="id"] 打开弹窗、
     [data-toast="文案"] 弹提示、[data-close] 关闭最近的抽屉/弹窗。
   - .pf-code 自动注入「复制」按钮。
   ============================================================ */
(function () {
  'use strict';

  /* ---------- 导航表（key → 标题 → 文件 → 图标） ---------- */
  function icon(paths) {
    return '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">' + paths + '</svg>';
  }
  var NAV = [
    { key: 'dashboard',   title: '仪表盘',      file: 'index.html',
      icon: icon('<rect x="1.8" y="1.8" width="5.2" height="5.2" rx="1"/><rect x="9" y="1.8" width="5.2" height="5.2" rx="1"/><rect x="1.8" y="9" width="5.2" height="5.2" rx="1"/><rect x="9" y="9" width="5.2" height="5.2" rx="1"/>') },
    { key: 'providers',   title: '上游 Provider', file: 'providers.html',
      icon: icon('<rect x="2" y="2.5" width="12" height="4.6" rx="1"/><rect x="2" y="8.9" width="12" height="4.6" rx="1"/><path d="M4.4 4.8h.01M4.4 11.2h.01" stroke-width="1.8"/>') },
    { key: 'mappings',    title: '模型映射',    file: 'mappings.html',
      icon: icon('<path d="M1.8 4.5h3.4l5 7h4"/><path d="M1.8 11.5h3.4M10.2 4.5h4"/><path d="M12.4 2.7l1.8 1.8-1.8 1.8M12.4 9.7l1.8 1.8-1.8 1.8"/>') },
    { key: 'keys',        title: '虚拟密钥',    file: 'keys.html',
      icon: icon('<circle cx="5" cy="8" r="2.6"/><path d="M7.6 8h6.6M11.4 8v2.4M14.2 8v1.7"/>') },
    { key: 'logs',        title: '调用日志',    file: 'logs.html',
      icon: icon('<rect x="3" y="1.8" width="10" height="12.4" rx="1"/><path d="M5.6 5.2h4.8M5.6 8h4.8M5.6 10.8h2.8"/>') },
    { key: 'prices',      title: '价格表',      file: 'prices.html',
      icon: icon('<path d="M2 2.4h4.8L14 9.6l-4.4 4.4L2 6.8z"/><circle cx="5" cy="5.2" r="1"/>') },
    { key: 'credentials', title: '数据凭据',    file: 'credentials.html',
      icon: icon('<ellipse cx="8" cy="3.6" rx="5.8" ry="2"/><path d="M2.2 3.6v8.6c0 1.1 2.6 2 5.8 2s5.8-.9 5.8-2V3.6"/><path d="M2.2 7.9c0 1.1 2.6 2 5.8 2s5.8-.9 5.8-2"/>') },
    { key: 'events',      title: '事件',        file: 'events.html',
      icon: icon('<path d="M8 1.9a4 4 0 0 0-4 4v3L2.6 11.4h10.8L12 8.9v-3a4 4 0 0 0-4-4z"/><path d="M6.8 13.5a1.3 1.3 0 0 0 2.4 0"/>') },
    { key: 'settings',    title: '设置',        file: 'settings.html',
      icon: icon('<circle cx="8" cy="8" r="2.2"/><path d="M8 1.6v1.9M8 12.5v1.9M1.6 8h1.9M12.5 8h1.9M3.5 3.5l1.3 1.3M11.2 11.2l1.3 1.3M12.5 3.5l-1.3 1.3M4.8 11.2l-1.3 1.3"/>') }
  ];

  /* ---------- 壳注入 ---------- */
  function buildShell() {
    var pageKey = document.body.dataset.page;
    var tpl = document.getElementById('page');
    if (!pageKey || !tpl) return;           // login / wizard：不注入壳

    var current = null;
    var navHtml = '';
    NAV.forEach(function (n) {
      var active = n.key === pageKey;
      if (active) current = n;
      navHtml += '<a class="pf-nav-item' + (active ? ' active' : '') + '" href="' + n.file + '">' + n.icon + '<span>' + n.title + '</span></a>';
    });
    var title = current ? current.title : 'LLM-portal';

    var layout = document.createElement('div');
    layout.className = 'pf-layout';
    layout.innerHTML =
      '<aside class="pf-sidebar">' +
        '<div class="pf-logo">' +
          '<span class="pf-logo-mark"><svg viewBox="0 0 64 64" width="100%" height="100%"><rect x="4.5" y="4.5" width="55" height="55" rx="9.5" fill="#0b60f5"/><path d="M13.3 13H31.5A5.5 5.5 0 0 1 37 18.5V29H35.3V35H37V45.5A5.5 5.5 0 0 1 31.5 51H13.3A5.5 5.5 0 0 1 7.8 45.5V18.5A5.5 5.5 0 0 1 13.3 13Z" fill="#041d66"/><path d="M14.3 19.5 30.4 32M14.3 32H30.4M14.3 44.5 30.4 32" stroke="#fff" stroke-width="3.1" stroke-linecap="round" fill="none"/><path d="M30.4 32H50" stroke="#fff" stroke-width="4.6" stroke-linecap="round" fill="none"/><circle cx="14.3" cy="19.5" r="3.6" fill="#fff"/><circle cx="14.3" cy="32" r="3.6" fill="#fff"/><circle cx="14.3" cy="44.5" r="3.6" fill="#fff"/><circle cx="50" cy="32" r="4.4" fill="#fff"/></svg></span>' +
          '<span class="pf-logo-text">LLM-portal</span>' +
        '</div>' +
        '<nav class="pf-nav">' + navHtml + '</nav>' +
        '<div class="pf-sidebar-foot">' +
          '<span class="pf-avatar sm">A</span>' +
          '<div><div class="pf-admin-name">管理员</div><div class="pf-admin-sub">admin</div></div>' +
        '</div>' +
      '</aside>' +
      '<div class="pf-main">' +
        '<header class="pf-topbar">' +
          '<div class="pf-topbar-title">' + title + '</div>' +
          '<div class="pf-topbar-right"><span class="pf-avatar">A</span></div>' +
        '</header>' +
        '<main class="pf-content"></main>' +
      '</div>';

    var content = layout.querySelector('.pf-content');
    content.appendChild(tpl.content);
    tpl.remove();
    document.body.insertBefore(layout, document.body.firstChild);
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
        } catch (err) { /* 原型环境忽略 */ }
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
  document.addEventListener('DOMContentLoaded', function () {
    buildShell();
    document.querySelectorAll('.pf-tabs').forEach(function (bar) { pfTabs(bar); });
    initCodeBlocks();
    initDelegation();
  });

  /* ---------- 全局导出 ---------- */
  window.pfTabs = pfTabs;
  window.pfDrawer = pfDrawer;
  window.pfDrawerClose = pfDrawerClose;
  window.pfModal = pfModal;
  window.pfModalClose = pfModalClose;
  window.pfToast = pfToast;
})();
