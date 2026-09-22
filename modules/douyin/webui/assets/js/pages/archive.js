/* ============================================================
 * pages/archive.js — 作品档案（本地作品库）
 * ------------------------------------------------------------
 * bridge 模式：读取工程 dy_downloader.db 的 aweme 表（只读），
 *   真实封面（CDN 过期自动降级程序化封面）、真实落盘路径；
 *   收藏操作保存在本地（不动工程数据）。
 * mock 模式：与首版一致（演示作品 + 本地缩略图）。
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  let f = { q: '', type: 'all', favOnly: false, sort: 'new', author: '' };
  let bridgeState = { offset: 0, total: 0, items: [] };
  const PAGE = 60;

  function render(view) {
    view.className = 'content';
    delete view.dataset.platform;
    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div class="page-head__row">' +
          '<div><div class="crumb"><span class="crumb__dot"></span>下载与管理 · 作品档案</div>' +
          '<h1 class="page-title">作品档案</h1></div>' +
          '<div class="flex">' +
            '<button class="btn btn--ghost btn--sm" id="ar-export">' + I.icon('file', 14) + '导出清单</button>' +
            '<button class="btn btn--primary btn--sm" data-go="douyin">' + I.icon('download', 14) + '去下载</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div id="ar-stats"></div>' +
      '<div class="flex flex-wrap mb-4" style="gap:12px">' +
        '<div style="position:relative;flex:2;min-width:220px">' +
          '<input class="input" id="ar-search" placeholder="搜索标题 / 作者 / 路径" aria-label="搜索作品" ' +
          'style="padding:9px 13px 9px 36px;font-family:var(--font-sans)">' +
          '<span class="i" style="position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--faint)">' +
          I.icon('search', 15) + '</span>' +
        '</div>' +
        '<select class="input" id="ar-author" style="flex:0 0 180px;padding:9px 36px 9px 13px;font-family:var(--font-sans)" aria-label="作者筛选">' +
          '<option value="">全部作者</option>' +
        '</select>' +
        '<select class="input" id="ar-type" style="flex:0 0 130px;padding:9px 36px 9px 13px;font-family:var(--font-sans)" aria-label="类型筛选">' +
          '<option value="all">全部类型</option><option value="video">视频</option>' +
          '<option value="note">图文</option>' +
        '</select>' +
        '<button class="chip' + (f.favOnly ? ' is-on' : '') + '" id="ar-fav" style="align-self:stretch;display:inline-flex;align-items:center">' +
        I.icon('heart', 13) + '仅收藏</button>' +
      '</div>' +
      '<div id="ar-list"></div>' +
      '<div id="ar-more" class="mt-4" style="text-align:center"></div>';

    if (window.DYApi.isBridge()) {
      initBridge(view);
    } else {
      f.author = '';
      document.getElementById('ar-author').parentElement.classList.add('hidden-author');
      renderStatsMock();
      renderListMock();
    }
    bindCommon(view);
  }

  function bindCommon(view) {
    view.querySelector('#ar-search').addEventListener('input', U.debounce((e) => {
      f.q = e.target.value.trim().toLowerCase();
      window.DYApi.isBridge() ? reloadBridge(true) : renderListMock();
    }, 200));
    view.querySelector('#ar-type').addEventListener('change', (e) => {
      f.type = e.target.value;
      window.DYApi.isBridge() ? reloadBridge(true) : renderListMock();
    });
    view.querySelector('#ar-fav').addEventListener('click', (e) => {
      f.favOnly = !f.favOnly;
      e.currentTarget.classList.toggle('is-on', f.favOnly);
      window.DYApi.isBridge() ? reloadBridge(true) : renderListMock();
    });
    const authorSel = document.getElementById('ar-author');
    if (authorSel) authorSel.addEventListener('change', (e) => {
      f.author = e.target.value;
      reloadBridge(true);
    });
    view.querySelector('#ar-list').addEventListener('click', onAction);
    view.querySelector('#ar-export').addEventListener('click', exportManifest);
    view.querySelectorAll('[data-go]').forEach((b) =>
      b.addEventListener('click', () => window.DYRouter.go(b.dataset.go)));
    U.bus.on('store:reset', () => {
      if (!window.DYApi.isBridge()) { renderStatsMock(); renderListMock(); }
    });
  }

  /* ================= BRIDGE：真实数据库 ================= */
  async function initBridge(view) {
    // 作者下拉
    try {
      const authors = await window.DYApi.bridgeAuthors();
      const sel = document.getElementById('ar-author');
      if (sel) {
        sel.innerHTML = '<option value="">全部作者</option>' +
          authors.map((a) => '<option value="' + esc(a.nickname) + '">' +
            esc(a.nickname) + '（' + a.works + '）</option>').join('');
      }
    } catch (e) { /* 下拉留空 */ }
    await renderStatsBridge();
    await reloadBridge(true);
  }

  async function renderStatsBridge() {
    const el = document.getElementById('ar-stats');
    try {
      const r = await fetch('/api/bridge/stats', { cache: 'no-store' });
      const st = await r.json();
      if (el) el.innerHTML = C.stats([
        { label: '作品总数', value: U.group(st.works || 0), hint: '工程数据库实际记录' },
        { label: '作者数', value: String(st.authors || 0), hint: '按目录去重' },
        { label: '最近下载', value: esc(st.lastDownload ? U.fmtDate(st.lastDownload) : '-'), hint: '按 download_time' },
        { label: '历史任务', value: String(st.history || 0), hint: 'download_history 表' },
      ]);
    } catch (e) {
      if (el) el.innerHTML = '';
    }
  }

  async function reloadBridge(reset) {
    const box = document.getElementById('ar-list');
    if (!box) return;
    if (reset) { bridgeState = { offset: 0, total: 0, items: [] }; }
    if (bridgeState.loading) return;
    bridgeState.loading = true;
    box.innerHTML = bridgeState.items.length
      ? box.innerHTML : C.skeletonRows(3);
    try {
      const d = await window.DYApi.bridgeWorks({
        author: f.author, type: f.type, q: f.q,
        limit: PAGE, offset: bridgeState.offset,
      });
      let items = d.items;
      if (f.favOnly) items = items.filter((w) => w.fav);
      bridgeState.items = reset ? items : bridgeState.items.concat(items);
      bridgeState.total = d.total;
      bridgeState.offset += PAGE;
      renderBridgeList();
    } catch (e) {
      box.innerHTML = '<div class="error-box"><h3>读取数据库失败</h3><p class="muted">' +
        esc(String(e.message || e)) + '</p></div>';
    } finally {
      bridgeState.loading = false;
    }
  }

  function renderBridgeList() {
    const box = document.getElementById('ar-list');
    const more = document.getElementById('ar-more');
    if (!box) return;
    const shown = bridgeState.items;
    box.innerHTML = shown.length
      ? '<div class="grid grid--4">' + shown.map((w) => C.workCard(w)).join('') + '</div>'
      : ((f.q || f.type !== 'all' || f.favOnly || f.author)
        ? C.empty('search', 'search')
        : '<div class="empty"><div class="empty__icon">' + I.icon('folder', 32) + '</div>' +
          '<div class="empty__title">数据库暂无作品记录</div>' +
          '<div class="empty__desc">完成第一次下载后会自动出现在这里</div></div>');
    if (more) {
      const hasMore = shown.length < bridgeState.total;
      more.innerHTML = hasMore
        ? '<button class="btn btn--ghost" id="ar-loadmore">加载更多（已显示 ' + shown.length +
          ' / ' + bridgeState.total + '）</button>'
        : (shown.length ? '<span class="small faint">已全部加载 ' + shown.length + ' 条</span>' : '');
      const btn = document.getElementById('ar-loadmore');
      if (btn) btn.addEventListener('click', () => reloadBridge(false));
    }
  }

  /* ================= MOCK：演示数据 ================= */
  function renderStatsMock() {
    const ws = window.DYStore.state.works;
    const videos = ws.filter((w) => w.type === 'video');
    const totalMB = ws.reduce((a, w) => a + (w.sizeMB || 0), 0);
    const authors = new Set(ws.map((w) => w.creatorName)).size;
    const el = document.getElementById('ar-stats');
    if (el) el.innerHTML = C.stats([
      { label: '作品总数', value: U.group(ws.length), hint: '本地已归档' },
      { label: '视频', value: U.group(videos.length), hint: '图文 ' + (ws.length - videos.length) + ' 条' },
      { label: '总体积', value: U.bytes(totalMB * 1024 * 1024), hint: '估算值（演示数据）' },
      { label: '作者数', value: String(authors), hint: '按目录去重' },
    ]);
  }

  function filteredMock() {
    let ws = window.DYStore.state.works.slice();
    if (f.q) ws = ws.filter((w) => w.title.toLowerCase().includes(f.q) ||
      (w.creatorName || '').toLowerCase().includes(f.q));
    if (f.type !== 'all') ws = ws.filter((w) => w.type === f.type);
    if (f.favOnly) ws = ws.filter((w) => w.fav);
    const by = {
      new: (a, b) => (b.downloadTime || '').localeCompare(a.downloadTime || ''),
      old: (a, b) => (a.downloadTime || '').localeCompare(b.downloadTime || ''),
      size: (a, b) => b.sizeMB - a.sizeMB,
    };
    return ws.sort(by[f.sort] || by.new);
  }

  function renderListMock() {
    const box = document.getElementById('ar-list');
    const more = document.getElementById('ar-more');
    if (!box) return;
    if (more) more.innerHTML = '';
    const ws = filteredMock();
    box.innerHTML = ws.length
      ? '<div class="grid grid--4">' + ws.map((w) => C.workCard(w)).join('') + '</div>'
      : (f.q || f.type !== 'all' || f.favOnly ? C.empty('search', 'search') : C.empty('archive', 'folder'));
  }

  /* ================= 公共操作 ================= */
  function exportManifest() {
    let rows;
    if (window.DYApi.isBridge()) {
      rows = bridgeState.items.map((w) =>
        [w.createTime, w.type, w.creatorName, w.title, w.awemeId, w.filePath].join('\t'));
    } else {
      rows = window.DYStore.state.works.map((w) =>
        [w.createTime, w.type, w.creatorName, w.title, w.awemeId, w.filePath].join('\t'));
    }
    const blob = new Blob(['\ufeff' + rows.join('\n')], { type: 'text/tab-separated-values;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'archive-manifest.tsv';
    a.click();
    URL.revokeObjectURL(a.href);
    C.toast('已导出 TSV 清单（对应工程的 download_manifest.jsonl）', 'ok');
  }

  function onAction(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    if (window.DYApi.isBridge()) {
      const w = bridgeState.items.find((x) => x.id === id);
      if (!w) return;
      if (act === 'fav') {
        window.DYApi.toggleBridgeFav(w.awemeId);
        w.fav = !w.fav;
        renderBridgeList();
      } else if (act === 'path') {
        U.copyText(w.filePath).then((ok) => C.toast(ok ? '路径已复制' : '复制失败', ok ? 'ok' : 'err'));
      } else if (act === 'folder') {
        openPathModal(w);
      }
      return;
    }
    const w = window.DYStore.state.works.find((x) => x.id === id);
    if (!w) return;
    if (act === 'fav') {
      window.DYStore.toggleWorkFav(id);
      renderListMock();
    } else if (act === 'path') {
      U.copyText(w.filePath).then((ok) => C.toast(ok ? '路径已复制' : '复制失败', ok ? 'ok' : 'err'));
    } else if (act === 'folder') {
      openPathModal(w);
    }
  }

  function openPathModal(w) {
    C.open(C.modalFrame('文件位置',
      '<p class="small muted mb-3">浏览器沙箱不能直接打开本地目录。以下为该作品的落盘相对路径' +
      '（与工程 <code>Downloaded/</code> 目录结构一致），在工程根目录即可找到对应文件。</p>' +
      '<code class="mono small" style="display:block;padding:12px;background:var(--panel-2);border-radius:8px;' +
      'word-break:break-all">' + esc(w.filePath) + '</code>',
      '<button class="btn btn--ghost" data-modal-close>关闭</button>' +
      '<button class="btn btn--primary" id="__copy_path">复制路径</button>'));
    document.getElementById('__copy_path').addEventListener('click', () => {
      U.copyText(w.filePath).then((ok) => C.toast(ok ? '路径已复制' : '复制失败', ok ? 'ok' : 'err'));
    });
  }

  window.DYRouter.register('archive', render);
})();
