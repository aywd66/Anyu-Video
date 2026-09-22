/* ============================================================
 * app.js - 应用骨架:侧边栏渲染、全局事件、入口启动
 * ------------------------------------------------------------
 * 职责:
 *  1. 按 config.js 的 NAV 渲染侧边栏(平台切换 / 菜单 / 账号卡 / 数据卡)
 *  2. 渲染顶层面板结构(面包屑 + 页面容器)
 *  3. 全局事件:任务徽标数量、平台切换后的账号卡文案
 *  4. 网络诊断 / 数据台账 两个全局弹窗
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  const App = {
    currentPlatform: 'douyin',

    /* ---------- 启动 ---------- */
    async boot() {
      window.DYStore.init();
      this.renderShell();
      this.renderSidebar();
      this.bindGlobal();
      // 探测运行环境：由 bridge_server.py 托管时自动接入工程真实数据
      try { await window.DYApi.detectMode(); } catch (e) { console.warn('模式探测失败', e); }
      this.updateModeCard();
      window.DYRouter.start();
      this.updateTaskBadge();
      this.updateAccountCard();
      // 任务数量变化 → 更新侧边栏徽标
      const refreshBadge = () => this.updateTaskBadge();
      ['task:created', 'task:updated', 'task:removed', 'task:bulk-changed', 'store:reset']
        .forEach((ev) => U.bus.on(ev, refreshBadge));
    },

    /** 侧边栏数据卡：显示当前数据源模式 */
    updateModeCard() {
      const title = document.getElementById('side-mode-title');
      const meta = document.getElementById('side-mode-meta');
      if (!meta) return;
      if (window.DYApi.isBridge()) {
        if (title) title.textContent = '工程数据';
        meta.innerHTML = C.dot('green') + '已连接 · 只读';
      } else {
        if (title) title.textContent = '本地数据';
        meta.innerHTML = C.dot('green') + '演示模式';
      }
    },

    /* ---------- 顶层结构 ---------- */
    renderShell() {
      const root = document.getElementById('app');
      root.innerHTML =
        '<aside class="sidebar" id="sidebar" aria-label="主导航"></aside>' +
        '<main class="main" id="main"><div class="content" id="page-view"></div></main>';
    },

    /* ---------- 侧边栏 ---------- */
    renderSidebar() {
      const cfg = window.DYConfig;
      const sb = document.getElementById('sidebar');
      if (!sb) return;
      const groups = {};
      cfg.NAV.menu.forEach((m) => {
        (groups[m.group] = groups[m.group] || []).push(m);
      });

      let menuHtml = '';
      Object.keys(groups).forEach((g) => {
        menuHtml += '<div class="side-label tag-caps">' + esc(g) + '</div><nav class="nav" aria-label="' + esc(g) + '">';
        groups[g].forEach((m) => {
          menuHtml +=
            '<button class="nav__item" data-nav-id="' + esc(m.id) + '">' +
            I.icon(m.icon, 17) + '<span>' + esc(m.label) + '</span>' +
            (m.badge ? '<span class="nav__badge" id="nav-task-badge" hidden>0</span>' : '') +
            '<span class="nav__dot"></span>' +
            '</button>';
        });
        menuHtml += '</nav>';
      });

      sb.innerHTML =
        C.brand() +
        '<div class="side-section">' +
          '<div class="side-label tag-caps">平台</div>' +
          '<div class="platform-switch" role="tablist" aria-label="平台切换">' +
          cfg.NAV.platforms.map((p) =>
            '<button class="platform-btn" role="tab" data-dot="' + esc(p.dot) + '" data-platform-id="' + esc(p.id) + '" ' +
            'aria-selected="false"><span class="platform-btn__dot"></span>' + I.icon(p.icon, 16) + '<span class="platform-btn__label">' + esc(p.label) + '</span></button>'
          ).join('') +
          '</div>' +
          '<div class="platform-sub" id="platform-sub"></div>' +
        '</div>' +
        '<div class="side-section side-section--account">' +
          '<div class="side-card" id="account-card"></div>' +
        '</div>' +
        '<div class="side-section side-section--menu">' + menuHtml + '</div>' +
        '<div class="side-section">' +
          '<div class="side-actions">' +
            '<button class="btn btn--ghost btn--sm" id="btn-settings-nav">' + I.icon('settings', 15) + '设置</button>' +
            '<button class="btn btn--ghost btn--sm" id="btn-netdiag">' + I.icon('activity', 15) + '网络诊断</button>' +
          '</div>' +
        '</div>' +
        '<div class="side-section side-section--mode">' +
          '<div class="side-card">' +
            '<div class="side-card__icon">' + I.icon('database', 17) + '</div>' +
            '<div class="side-card__body">' +
              '<div class="side-card__title" id="side-mode-title">本地数据</div>' +
              '<div class="side-card__meta" id="side-mode-meta">' + C.dot('green') + '演示模式</div>' +
            '</div>' +
            '<button class="btn btn--ghost" id="btn-ledger">台账</button>' +
          '</div>' +
        '</div>' +
        '<div class="side-footer tag-caps"><span>' + esc(cfg.APP.fullName.toUpperCase()) + '</span>' +
          '<span>' + esc(cfg.APP.version) + '</span></div>';

      // 平台切换
      sb.querySelectorAll('.platform-btn').forEach((btn) => {
        btn.addEventListener('click', () => window.DYRouter.go(btn.dataset.platformId));
      });
      // 菜单跳转
      sb.querySelectorAll('.nav__item').forEach((btn) => {
        btn.addEventListener('click', () => window.DYRouter.go(btn.dataset.navId));
      });
      document.getElementById('btn-settings-nav').addEventListener('click', () => window.DYRouter.go('settings'));
      document.getElementById('btn-netdiag').addEventListener('click', () => this.openNetDiag());
      document.getElementById('btn-ledger').addEventListener('click', () => this.openLedger());
    },

    /** 平台按钮/账号卡/副标题随当前平台联动 */
    updatePlatformUI(platformId) {
      this.currentPlatform = platformId;
      const cfg = window.DYConfig;
      document.querySelectorAll('.platform-btn').forEach((b) => {
        const on = b.dataset.platformId === platformId;
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-selected', String(on));
      });
      const p = cfg.NAV.platforms.find((x) => x.id === platformId);
      const sub = document.getElementById('platform-sub');
      if (sub && p) sub.textContent = p.sub || '';
      // 菜单里镜像平台页的选中态:douyin 平台页即"链接下载"
      document.querySelectorAll('.nav__item').forEach((b) => {
        b.classList.toggle('is-active', b.dataset.navId === platformId);
      });
      this.updateAccountCard();
    },

    updateAccountCard() {
      const el = document.getElementById('account-card');
      if (!el) return;
      const acc = window.DYConfig.NAV.account[this.currentPlatform] || window.DYConfig.NAV.account.douyin;
      el.innerHTML =
        '<div class="side-card__icon">' + I.icon(acc.icon, 17) + '</div>' +
        '<div class="side-card__body">' +
          '<div class="side-card__title">' + esc(acc.name) + '</div>' +
          '<div class="side-card__meta">' + C.dot(acc.metaTone) + esc(acc.meta) + '</div>' +
        '</div>' +
        '<button class="btn btn--ghost" id="btn-account-manage">管理</button>';
      document.getElementById('btn-account-manage').addEventListener('click', () => this.openAccountManage());
    },

    updateTaskBadge() {
      const badge = document.getElementById('nav-task-badge');
      if (!badge || !window.DYStore.state) return;
      const active = window.DYStore.state.tasks.filter((t) => t.status === 'pending' || t.status === 'running').length;
      badge.textContent = String(active);
      badge.hidden = active === 0;
    },

    /* ---------- 全局事件 ---------- */
    bindGlobal() {
      U.bus.on('platform:changed', (p) => {
        this.currentPlatform = p;
        this.updateAccountCard();
      });
    },

    /* ---------- 全局弹窗：账号管理 ---------- */
    openAccountManage() {
      const s = window.DYStore.state.settings;
      C.open(C.modalFrame(
        '账号管理',
        '<div class="flex mb-4"><div class="side-card__icon">' + I.icon('users', 18) + '</div>' +
        '<div class="grow"><div class="side-card__title">抖音账号</div>' +
        '<div class="side-card__meta">' + C.dot('green') + '已登录(演示态,网页版不读写真实 Cookie)</div></div></div>' +
        '<div class="form-item"><div class="form-item__label">Cookie 状态</div>' +
        '<p class="small muted">网页版演示环境不保存任何账号凭据。接入真实后端后,此处展示 ' +
        '<code>.cookies.json</code> 的有效期与 msToken 状态。</p></div>' +
        '<div class="form-item"><div class="form-item__label">代理</div>' +
        '<p class="small muted">' + esc(s.proxy || '未设置(可在设置页配置)') + '</p></div>',
        '<button class="btn btn--ghost" data-modal-close>关闭</button>' +
        '<button class="btn btn--primary" id="__acct_to_settings">去设置</button>'
      ));
      document.getElementById('__acct_to_settings').addEventListener('click', () => {
        C.close();
        window.DYRouter.go('settings');
      });
    },

    /* ---------- 全局弹窗:网络诊断 ---------- */
    async openNetDiag() {
      const bodyId = '__netdiag_body';
      C.open(C.modalFrame('网络诊断', '<div id="' + bodyId + '">' + C.skeletonRows(2) + '</div>',
        '<button class="btn btn--ghost" data-modal-close>关闭</button>' +
        '<button class="btn btn--primary" id="__netdiag_again">重新检测</button>'));

      const run = async () => {
        const box = document.getElementById(bodyId);
        if (!box) return;
        box.innerHTML = C.skeletonRows(2);
        const t0 = performance.now();
        let health = null;
        let err = null;
        try { health = await window.DYApi.health(); }
        catch (e) { err = e; }
        const ms = Math.round(performance.now() - t0);
        const s = window.DYStore.state.settings;
        const items = [
          { label: '数据层', ok: true, desc: '本地 mock 数据层正常,任务模拟可用' },
          { label: 'API 模式', ok: true, desc: '当前为 mock 模式(接真实后端见 README)' },
          err
            ? { label: '健康检查', ok: false, desc: '请求失败:' + (err.message || err) }
            : { label: '健康检查', ok: true, desc: (health && health.status === 'ok' ? '正常' : '异常') + ' · 响应 ' + ms + 'ms' },
          { label: '代理', ok: !s.proxy, desc: s.proxy ? '已配置 ' + s.proxy : '未配置(如需代理请在设置页填写)' },
        ];
        box.innerHTML = items.map((it) =>
          '<div class="row-item" style="padding:12px 16px;margin-bottom:10px">' +
          (it.ok ? '<span class="i" style="color:var(--green)">' + I.icon('circleCheck', 20) + '</span>'
                 : '<span class="i" style="color:var(--amber)">' + I.icon('alert', 20) + '</span>') +
          '<div class="row-item__main"><div class="row-item__title">' + esc(it.label) + '</div>' +
          '<div class="row-item__meta">' + esc(it.desc) + '</div></div></div>'
        ).join('');
      };
      await run();
      document.getElementById('__netdiag_again').addEventListener('click', run);
    },

    /* ---------- 全局弹窗:数据台账 ---------- */
    openLedger() {
      const rows = window.DYStore.ledger();
      const total = rows.reduce((a, b) => a + b.bytes, 0);
      const bridge = window.DYApi.isBridge() ? window.DYApi.bridgeInfo : null;
      const dbSt = bridge && bridge.db;
      const bridgeCard = bridge
        ? '<div class="row-item mb-4" style="padding:14px 18px">' +
          '<div class="side-card__icon">' + I.icon('database', 17) + '</div>' +
          '<div class="row-item__main"><div class="row-item__meta" style="margin:0">' +
          '<span>工程数据库（只读）</span>' +
          '<span>作品 <b class="num">' + esc(String((dbSt && dbSt.works) || 0)) + '</b></span>' +
          '<span>作者 <b class="num">' + esc(String((dbSt && dbSt.authors) || 0)) + '</b></span>' +
          '<span>历史任务 <b class="num">' + esc(String((dbSt && dbSt.history) || 0)) + '</b></span>' +
          '</div></div></div>'
        : '';
      const body = bridgeCard +
        '<p class="small muted mb-4">所有交互数据(任务、作者、收藏、设置)统一存在浏览器 localStorage 的 ' +
        '<code>dyui.*</code> 键下,导出即可迁移,清空即回演示初始态;工程数据库只读不受影响。</p>' +
        (rows.length
          ? '<div class="table-wrap"><table class="table"><thead><tr><th>台账项</th><th>存储键</th><th class="num">占用</th></tr></thead><tbody>' +
            rows.map((r) =>
              '<tr><td>' + esc(r.label) + '</td><td><code class="mono small">' + esc(r.key) + '</code></td>' +
              '<td class="num">' + esc(window.DYUtils.bytes(r.bytes)) + '</td></tr>'
            ).join('') +
            '<tr><td><b>合计</b></td><td></td><td class="num">' + esc(window.DYUtils.bytes(total)) + '</td></tr>' +
            '</tbody></table></div>'
          : '<p class="muted">暂无本地存储记录。</p>');
      C.open(C.modalFrame('数据台账', body,
        '<button class="btn btn--ghost" id="__ledger_reset">重置演示数据</button>' +
        '<button class="btn btn--primary" id="__ledger_export">导出数据 JSON</button>', true));
      document.getElementById('__ledger_export').addEventListener('click', () => {
        const data = window.DYStore.exportAll();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'dyui-data-' + new Date().toISOString().slice(0, 10) + '.json';
        a.click();
        URL.revokeObjectURL(a.href);
        C.toast('数据已导出', 'ok');
      });
      document.getElementById('__ledger_reset').addEventListener('click', () => {
        C.confirm('重置演示数据', '将清空全部本地记录并恢复为内置演示数据,此操作不可撤销。', () => {
          window.DYStore.resetDemo();
          window.DYRouter.render();
          C.toast('已重置为演示数据', 'ok');
        }, '重置', true);
      });
    },
  };

  window.DYApp = App;
  document.addEventListener('DOMContentLoaded', () => App.boot());
})();
