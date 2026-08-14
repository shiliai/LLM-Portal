/* ============================================================
   远程模型网关控制台原型 · 壳注入 + 交互助手
   基于 llm-portal-console/assets/portal.js 复制而来，仅替换导航表
   （NAV 数组）与侧栏文案以匹配本产品页面集；助手函数与事件委托逻辑
   保持一致，未删减任何既有能力。
   契约：
   - 控制台页面写 <body data-page="key"> + <template id="page">内容</template>，
     本脚本注入侧边栏/顶栏并把模板内容放入 .pf-content。
   - login 等无壳页面：不写 data-page 或 #page 即可，壳不注入，
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
    { key: 'dashboard', title: '仪表盘', file: 'index.html',
      icon: icon('<rect x="1.8" y="1.8" width="5.2" height="5.2" rx="1"/><rect x="9" y="1.8" width="5.2" height="5.2" rx="1"/><rect x="1.8" y="9" width="5.2" height="5.2" rx="1"/><rect x="9" y="9" width="5.2" height="5.2" rx="1"/>') },
    { key: 'sites', title: '站点与公钥', file: 'sites.html',
      icon: icon('<circle cx="4.6" cy="4.6" r="2.4"/><circle cx="11.4" cy="11.4" r="2.4"/><path d="M6.4 6.4l3.2 3.2"/><path d="M2.4 11.4h2.8M11.4 1.8v2.8"/>') },
    { key: 'groups', title: '分组', file: 'groups.html',
      icon: icon('<rect x="1.8" y="3.4" width="7.4" height="7.4" rx="1.2"/><rect x="6.8" y="6" width="7.4" height="7.4" rx="1.2"/>') },
    { key: 'models', title: '模型与别名', file: 'models.html',
      icon: icon('<path d="M1.8 4.5h3.4l5 7h4"/><path d="M1.8 11.5h3.4M10.2 4.5h4"/><path d="M12.4 2.7l1.8 1.8-1.8 1.8M12.4 9.7l1.8 1.8-1.8 1.8"/>') },
    { key: 'keys', title: '用户 Key', file: 'keys.html',
      icon: icon('<circle cx="5" cy="8" r="2.6"/><path d="M7.6 8h6.6M11.4 8v2.4M14.2 8v1.7"/>') },
    { key: 'usage', title: '用量总览', file: 'usage.html',
      icon: icon('<path d="M2.4 13.6V8.4M6.4 13.6V4.4M10.4 13.6V6.8M14 13.6V2.4"/>') },
    { key: 'my-usage', title: '我的用量', file: 'my-usage.html',
      icon: icon('<circle cx="8" cy="5.4" r="2.6"/><path d="M2.6 14c0-2.9 2.4-4.6 5.4-4.6s5.4 1.7 5.4 4.6"/>') },
    { key: 'mcp', title: 'MCP 管理', file: 'mcp.html',
      icon: icon('<rect x="1.8" y="3" width="12.4" height="10" rx="1.4"/><path d="M4.6 6.4l2 1.8-2 1.8M8.6 10h2.8"/>') }
  ];

  /* ---------- 壳注入 ---------- */
  function buildShell() {
    var pageKey = document.body.dataset.page;
    var tpl = document.getElementById('page');
    if (!pageKey || !tpl) return;           // login：不注入壳

    var current = null;
    var navHtml = '';
    NAV.forEach(function (n) {
      var active = n.key === pageKey;
      if (active) current = n;
      navHtml += '<a class="pf-nav-item' + (active ? ' active' : '') + '" href="' + n.file + '">' + n.icon + '<span>' + n.title + '</span></a>';
    });
    var title = current ? current.title : '远程模型网关';

    var layout = document.createElement('div');
    layout.className = 'pf-layout';
    layout.innerHTML =
      '<aside class="pf-sidebar">' +
        '<div class="pf-logo">' +
          '<span class="pf-logo-mark"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8.8 1.6 3.2 9h3.6l-.8 5.4L11.6 7H8z"/></svg></span>' +
          '<span class="pf-logo-text">远程模型网关</span>' +
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
