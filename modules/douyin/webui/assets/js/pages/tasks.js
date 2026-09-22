/* ============================================================
 * pages/tasks.js — 任务中心
 * ------------------------------------------------------------
 * bridge 模式（接入工程真实数据）：
 *   ① 实时任务 —— 本次会话提交的真实下载（轮询 3s，可取消/看日志）
 *   ② 工程下载历史 —— dy_downloader.db download_history 表（只读）
 *   ③ 本地演示记录 —— 之前 mock 阶段留下的任务，完整保留可切换查看
 * mock 模式：行为与首版一致（本地任务 + 状态筛选）
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  let filter = { tab: 'all', q: '' };

  function render(view) {
    view.className = 'content';
    delete view.dataset.platform;

    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div class="page-head__row">' +
          '<div><div class="crumb"><span class="crumb__dot"></span>任务中心 · Tasks</div>' +
          '<h1 class="page-title">任务中心</h1></div>' +
          '<div class="flex">' +
            '<button class="btn btn--ghost btn--sm" id="tk-clear">' + I.icon('trash', 14) + '清空本地演示记录</button>' +
            '<button class="btn btn--primary btn--sm" data-go="douyin">' + I.icon('plus', 14) + '新建下载</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div id="tk-stats"></div>' +
      '<div class="flex flex-wrap mb-4" style="gap:12px">' +
        '<div id="tk-tabs"></div>' +
        '<div class="right" style="position:relative;min-width:220px">' +
          '<input class="input" id="tk-search" placeholder="搜索标题 / 链接" aria-label="搜索任务" ' +
          'style="padding:9px 13px 9px 36px;font-family:var(--font-sans)">' +
          '<span class="i" style="position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--faint)">' +
          I.icon('search', 15) + '</span>' +
        '</div>' +
      '</div>' +
      '<div id="tk-list" aria-live="polite"></div>' +
      '<div id="tk-history-section"></div>' +
      '<div id="tk-local-section"></div>';

    if (window.DYApi.isBridge()) {
      renderBridge();
    } else {
      renderStats();
      renderTabs();
      renderList();
      bindMockControls(view);
    }

    view.querySelectorAll('[data-go]').forEach((b) =>
      b.addEventListener('click', () => window.DYRouter.go(b.dataset.go)));
    view.querySelector('#tk-list').addEventListener('click', onTaskAction);
    U.bus.on('store:reset', () => { if (!window.DYApi.isBridge()) renderAll(); });
  }

  /* ================= BRIDGE 模式 ================= */
  function renderBridge() {
    const view = document.getElementById('page-view');
    // 实时任务轮询（页面切换时由 router 统一清理）
    window.__DY_PAGE_TIMER = setInterval(refreshBridge, 3000);
    refreshBridge();
    renderLocalSection();
    bindMockControls(view);

    async function refreshBridge() {
      const box = document.getElementById('tk-list');
      if (!box) return;
      try {
        const feed = await window.DYApi.taskFeed();
        // 实时任务
        const live = feed.live.filter((t) => matchFilter(t));
        box.innerHTML =
          '<div class="flex mb-3"><h2 class="card__title" style="margin:0">实时任务</h2>' +
          '<span class="small faint">' + I.icon('loader', 13) + ' 每 3 秒自动刷新</span></div>' +
          (live.length
            ? '<div class="grid" style="gap:12px">' + live.map((t) => C.taskRow(t)).join('') + '</div>'
            : '<div class="empty"><div class="empty__icon">' + I.icon('tasks', 32) + '</div>' +
              '<div class="empty__title">暂无进行中的下载</div>' +
              '<div class="empty__desc">去「链接下载」粘贴抖音链接，提交真实下载任务</div></div>');
        // 工程下载历史（真实数据库）
        const hBox = document.getElementById('tk-history-section');
        const hist = feed.history.filter((t) => matchFilter(t));
        hBox.innerHTML =
          '<div class="flex mb-3 mt-5"><h2 class="card__title" style="margin:0">工程下载历史</h2>' +
          '<span class="small faint">来自 dy_downloader.db · 只读</span></div>' +
          (hist.length
            ? '<div class="table-wrap"><table class="table"><thead><tr>' +
              '<th>时间</th><th>类型</th><th>链接</th><th class="num">总数</th><th class="num">成功</th>' +
              '</tr></thead><tbody>' +
              hist.map((h) =>
                '<tr><td class="small">' + esc(h.createdAt || '-') + '</td>' +
                '<td>' + C.mediaType(h.type) + '</td>' +
                '<td><code class="mono small ellipsis" style="max-width:340px;display:inline-block" title="' + esc(h.url) + '">' +
                esc(U.truncate(h.url, 56)) + '</code></td>' +
                '<td class="num">' + esc(String(h.total)) + '</td>' +
                '<td class="num" style="color:' + (h.success > 0 ? 'var(--green)' : 'var(--muted)') + '">' +
                esc(String(h.success)) + '</td></tr>'
              ).join('') + '</tbody></table></div>'
            : '<div class="empty"><div class="empty__title">暂无历史记录</div></div>');
      } catch (e) {
        box.innerHTML = '<div class="error-box"><h3>桥接服务不可达</h3><p class="muted">' +
          esc(String(e.message || e)) + '</p></div>';
      }
    }

    // 统计条：实时 + 历史 + 本地
    (async () => {
      try {
        const feed = await window.DYApi.taskFeed();
        const active = feed.live.filter((t) => t.status === 'running').length;
        const el = document.getElementById('tk-stats');
        if (!el) return;
        el.innerHTML =
          '<div class="row-item mb-4" style="padding:14px 20px">' +
          '<div class="row-item__main"><div class="row-item__meta" style="margin:0;font-size:13px">' +
          '<span>进行中 <b class="num" style="color:var(--' + (active ? 'cyan' : 'muted') + ')">' + active + '</b></span>' +
          '<span>历史记录 <b class="num">' + feed.history.length + '</b></span>' +
          '<span>本地演示记录 <b class="num">' + feed.local.length + '</b></span>' +
          '<span class="faint">·</span>' +
          '<span class="small" style="color:var(--green)">' + I.icon('database', 13) + ' 已连接工程数据库（只读）</span>' +
          '</div></div></div>';
      } catch (e) { /* 忽略，列表区已展示错误 */ }
    })();
  }

  function renderLocalSection() {
    const box = document.getElementById('tk-local-section');
    if (!box) return;
    const local = window.DYStore.state.tasks;
    box.innerHTML =
      '<details class="mt-5" style="border:1px solid var(--line);border-radius:var(--r-card);background:var(--panel)">' +
      '<summary class="small" style="padding:14px 18px;cursor:pointer;font-weight:600;color:var(--muted)">' +
      '本地演示记录（' + local.length + ' 条 · 之前保留的数据，点击展开）</summary>' +
      '<div style="padding:0 18px 18px" id="tk-local-list">' +
      (local.length
        ? '<div class="grid" style="gap:12px;margin-top:14px">' + local.map((t) => C.taskRow(t)).join('') + '</div>'
        : '<p class="small faint">暂无本地演示记录。</p>') +
      '</div></details>';
    box.querySelector('#tk-local-list').addEventListener('click', onTaskAction);
  }

  function matchFilter(t) {
    if (filter.tab === 'running') { if (!(t.status === 'running' || t.status === 'pending')) return false; }
    else if (filter.tab === 'failed' && t.status !== 'failed') return false;
    if (filter.q && !((t.title || '').toLowerCase().includes(filter.q) ||
      (t.url || '').toLowerCase().includes(filter.q))) return false;
    return true;
  }

  /* ================= MOCK 模式 ================= */
  function renderAll() {
    const view = document.getElementById('page-view');
    if (!view || !view.querySelector('#tk-list')) return;
    renderStats();
    renderTabs();
    renderList();
  }

  function renderStats() {
    const ts = window.DYStore.state.tasks;
    const today0 = new Date(); today0.setHours(0, 0, 0, 0);
    const isToday = (t) => new Date(t.createdAt).getTime() >= today0.getTime();
    const active = ts.filter((t) => t.status === 'running' || t.status === 'pending').length;
    const okToday = ts.filter((t) => t.status === 'success' && isToday(t)).reduce((a, t) => a + (t.success || 0), 0);
    const failToday = ts.filter((t) => t.status === 'failed' && isToday(t)).length;
    const el = document.getElementById('tk-stats');
    if (!el) return;
    el.innerHTML =
      '<div class="row-item mb-4" style="padding:14px 20px">' +
        '<div class="row-item__main">' +
          '<div class="row-item__meta" style="margin:0;font-size:13px">' +
            '<span>进行中 <b class="num" style="color:var(--' + (active ? 'cyan' : 'muted') + ')">' + active + '</b></span>' +
            '<span>今日成功 <b class="num" style="color:var(--green)">' + U.group(okToday) + '</b></span>' +
            '<span>今日失败 <b class="num" style="color:' + (failToday ? 'var(--red)' : 'var(--muted)') + '">' + failToday + '</b></span>' +
            '<span class="faint">·</span>' +
            '<span>共 <b class="num">' + ts.length + '</b> 条记录</span>' +
          '</div>' +
        '</div>' +
        '<div class="row-item__actions">' +
          '<span class="small faint">最后更新 ' + esc(U.fromNow(window.__DY_LAST_RENDER ? window.__DY_LAST_RENDER.at : new Date().toISOString())) + '</span>' +
          '<button class="btn btn--ghost btn--sm" id="tk-refresh">' + I.icon('refresh', 13) + '刷新</button>' +
          '<button class="btn btn--ghost btn--sm" id="tk-clear">清理</button>' +
        '</div>' +
      '</div>';
    const rb = document.getElementById('tk-refresh');
    if (rb) rb.addEventListener('click', () => { renderAll(); C.toast('已刷新', 'ok'); });
    const cb = document.getElementById('tk-clear');
    if (cb) cb.addEventListener('click', clearFinished);
  }

  function clearFinished() {
    const n = window.DYStore.state.tasks.filter((t) =>
      t.status === 'success' || t.status === 'failed' || t.status === 'canceled').length;
    if (!n) { C.toast('没有可清理的任务', 'err'); return; }
    C.confirm('清理已结束任务', '将移除 ' + n + ' 条已结束（完成/失败/取消）的任务记录，进行中的任务不受影响。', () => {
      window.DYStore.clearFinishedTasks();
      renderAll();
      C.toast('已清理 ' + n + ' 条记录', 'ok');
    }, '清理', true);
  }

  function renderTabs() {
    const ts = window.DYStore.state.tasks;
    const count = (id) => id === 'all' ? ts.length : ts.filter((t) => t.status === id).length;
    const tabs = [
      { id: 'all', label: '全部' },
      { id: 'running', label: '进行中' },
      { id: 'success', label: '已完成' },
      { id: 'failed', label: '失败' },
      { id: 'canceled', label: '已取消' },
    ];
    const el = document.getElementById('tk-tabs');
    if (el) el.innerHTML = C.tabs(tabs.map((t) => ({ ...t, count: count(t.id) })), filter.tab);
  }

  function filtered() {
    const ts = window.DYStore.state.tasks;
    return ts.filter((t) => {
      if (filter.tab === 'running') { if (!(t.status === 'running' || t.status === 'pending')) return false; }
      else if (filter.tab !== 'all' && t.status !== filter.tab) return false;
      if (filter.q && !(t.title.toLowerCase().includes(filter.q) || t.url.toLowerCase().includes(filter.q))) return false;
      return true;
    });
  }

  function renderList() {
    const box = document.getElementById('tk-list');
    if (!box) return;
    const list = filtered();
    box.innerHTML = list.length
      ? '<div class="grid" style="gap:12px">' + list.map((t) => C.taskRow(t)).join('') + '</div>'
      : (filter.q || filter.tab !== 'all'
        ? C.empty('search', 'search')
        : C.empty('tasks', 'tasks'));
  }

  function bindMockControls(view) {
    const search = view.querySelector('#tk-search');
    if (search) search.addEventListener('input', U.debounce((e) => {
      filter.q = e.target.value.trim().toLowerCase();
      if (window.DYApi.isBridge()) { renderBridge(); } else { renderTabs(); renderList(); }
    }, 150));

    const tabs = view.querySelector('#tk-tabs');
    if (tabs) tabs.addEventListener('click', (e) => {
      const tab = e.target.closest('[data-tab]');
      if (!tab) return;
      filter.tab = tab.dataset.tab;
      if (window.DYApi.isBridge()) { renderBridge(); } else { renderTabs(); renderList(); }
    });

    const clearBtn = view.querySelector('#tk-clear');
    if (clearBtn && !window.DYApi.isBridge()) clearBtn.addEventListener('click', clearFinished);
  }

  function onTaskAction(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    const store = window.DYStore;
    const isBridgeTask = String(id).indexOf('b') === 0 && String(id).indexOf('bh-') !== 0;
    if (window.DYApi.isBridge() && isBridgeTask && (act === 'cancel' || act === 'log')) {
      if (act === 'cancel') {
        window.DYApi.cancelTask(id)
          .then(() => C.toast('已请求取消', 'ok'))
          .catch((err) => C.toast(err.message, 'err'));
      }
      return;
    }
    const t = store.state.tasks.find((x) => x.id === id);
    if (!t) return;
    if (act === 'cancel') {
      window.DYApi.cancelTask(id);
      C.toast('任务已取消', 'ok');
    } else if (act === 'retry') {
      window.DYApi.retryTask(id);
      C.toast('已重新排队', 'ok');
    } else if (act === 'remove') {
      store.removeTask(id);
      C.toast('任务已移除', 'ok');
    } else if (act === 'copy') {
      U.copyText(t.url).then((ok) => C.toast(ok ? window.DYConfig.TEXT.copied : '复制失败', ok ? 'ok' : 'err'));
    }
    if (window.DYApi.isBridge()) { renderBridge(); renderLocalSection(); }
    else renderAll();
    window.DYApp.updateTaskBadge();
  }

  window.DYRouter.register('tasks', render);
})();
