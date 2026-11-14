/* ============================================================
 * pages/archive.js — 作品档案（本地作品库）
 * ------------------------------------------------------------
 * bridge 模式：两个平台的本地库都能看，顶部切换来源：
 *   · 抖音 → 工程 dy_downloader.db 的 aweme 表（只读），
 *            真实封面（CDN 过期自动降级程序化封面）、真实落盘路径
 *   · 快手 → JSON 台账 download_log.json（详见后端 ks_works），
 *            台账不含封面/发布时间，卡片走程序化封面、时间取文件时间
 *   收藏操作只有抖音侧有，存在本地（不动工程数据）。
 * mock 模式：与首版一致（演示作品 + 本地缩略图），不显示来源切换。
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
  // 来源：'douyin' | 'kuaishou'。两个平台的字段、能力、空态都不同，
  // 所以是切换而不是混排 —— 混排会让筛选与统计都没法自洽。
  let src = 'douyin';

  function render(view) {
    view.className = 'content';
    delete view.dataset.platform;
    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div class="page-head__row">' +
          '<div><div class="crumb"><span class="crumb__dot"></span>下载与管理 · 作品档案</div>' +
          '<h1 class="page-title">作品档案</h1></div>' +
          '<div class="flex">' +
            (window.DYApi.isBridge()
              ? '<button class="btn btn--ghost btn--sm" id="ar-dedup">' + I.icon('copy', 14) + '查找重复视频</button>'
              : '') +
            '<button class="btn btn--ghost btn--sm" id="ar-export">' + I.icon('file', 14) + '导出清单</button>' +
            '<button class="btn btn--primary btn--sm" data-go="douyin">' + I.icon('download', 14) + '去下载</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div id="ar-src"></div>' +
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
      '<div id="ar-cover-tip" class="small" style="min-height:18px"></div>' +
      '<div id="ar-list"></div>' +
      '<div id="ar-more" class="mt-4" style="text-align:center"></div>';

    if (window.DYApi.isBridge()) {
      initBridge(view);
    } else {
      f.author = '';
      // ⚠️ 这里原先加的是一个**没有定义过的类名**（hidden-author），等于没隐藏 ——
      //    演示模式下「全部作者」下拉一直挂在那儿。改用全局兜底的 [hidden]。
      const asel = document.getElementById('ar-author');
      if (asel) asel.hidden = true;
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
    const srcBox = document.getElementById('ar-src');
    if (srcBox) srcBox.addEventListener('click', (e) => {
      const b = e.target.closest('[data-src]');
      if (b) switchSrc(b.dataset.src);
    });
    view.querySelector('#ar-list').addEventListener('click', onAction);
    view.querySelector('#ar-export').addEventListener('click', exportManifest);
    const dedupBtn = view.querySelector('#ar-dedup');
    if (dedupBtn) dedupBtn.addEventListener('click', openDedup);
    view.querySelectorAll('[data-go]').forEach((b) =>
      b.addEventListener('click', () => window.DYRouter.go(b.dataset.go)));
    U.bus.on('store:reset', () => {
      if (!window.DYApi.isBridge()) { renderStatsMock(); renderListMock(); }
    });
  }

  /* ================= BRIDGE：真实本地库（抖音 + 快手） ================= */
  async function initBridge(view) {
    await renderSrcTabs();
    await fillAuthorSelect();
    await renderStatsBridge();
    await reloadBridge(true);
    ensureKsCovers(view);
  }

  /* —— 快手封面：ffmpeg 从本地视频抽帧（台账没有封面数据）。
   * 首次进快手 tab 触发后台批量生成；生成完的卡片自动显示真实封面，
   * 未生成的保持程序化占位（img onerror 降级）。全部完成后自动刷新一次。 */
  let ksCoverTimer = null;
  function ensureKsCovers(view) {
    if (src !== 'kuaishou' || !window.DYApi.isBridge()) return;
    const pids = bridgeState.items.map((w) => w.awemeId).filter(Boolean);
    if (!pids.length) return;
    window.DYApi.ksCoversEnsure(pids)
      .then((r) => {
        /* 全部无文件（no_file>0 且没启动任务）→ 如实说明条，不轮询 */
        if (r && r.no_file > 0 && !r.running && (r.total || 0) === 0) {
          const bar = document.getElementById('ar-cover-tip');
          if (bar) bar.innerHTML = '<span class="dot dot--amber"></span> ' +
            '这些是快手台账的历史记录：对应的本地视频文件已随旧下载目录（E:\\录屏\\kssp）清理，' +
            '因此没有封面、无法播放。想找回某些作品：在「设置」开启「重新下载缺失文件」后，' +
            '把对应主播的主页链接粘到启动台重新下载。';
          return;
        }
        pollKsCovers();
      })
      .catch(() => { /* 触发失败就保持占位图，不打扰 */ });
  }

  function pollKsCovers() {
    if (ksCoverTimer) clearInterval(ksCoverTimer);
    const tick = async () => {
      if (src !== 'kuaishou') { clearInterval(ksCoverTimer); ksCoverTimer = null; return; }
      let s;
      try { s = await window.DYApi.ksCoversStatus(); } catch (e) { return; }
      const bar = document.getElementById('ar-cover-tip');
      if (s.running) {
        if (bar) bar.innerHTML = '<span class="dot dot--amber dot--pulse"></span> 快手封面后台生成中：' +
          (s.done || 0) + ' / ' + (s.total || 0) +
          '（完成后本页自动刷新；生成前显示为彩色占位图，翻页不受影响）';
        return;   // 继续轮询
      }
      /* 刚完成一轮（done>=total>0）→ 刷列表让真实封面上屏，然后收摊 */
      if (s.done > 0 && s.total > 0 && s.done >= s.total) {
        if (bar) bar.innerHTML = '';
        clearInterval(ksCoverTimer);
        ksCoverTimer = null;
        if (src === 'kuaishou') reloadBridge(true);
        return;
      }
      /* 无任务在跑：以列表数据的 pending 为准（pending>0 且 ensure 后仍
       * pending → 说明条已在 ensureKsCovers 的 no_file 分支给出） */
      if ((bridgeState.coversPending || 0) > 0) {
        ensureKsCovers(view);   // 幂等补齐（可能新落盘了文件）
        return;
      }
      if (bar) bar.innerHTML = '';
      clearInterval(ksCoverTimer);
      ksCoverTimer = null;
    };
    ksCoverTimer = setInterval(tick, 8000);
    tick();
  }

  /** 顶部来源标签。两个平台的库是两套独立数据，切换而不是混排。 */
  async function renderSrcTabs() {
    const el = document.getElementById('ar-src');
    if (!el) return;
    const mk = (k, name) => '<button class="srctab' + (src === k ? ' is-on' : '') +
      '" data-src="' + k + '" role="tab" aria-selected="' + (src === k) + '">' + name +
      '<span class="srctab__n" data-cnt="' + k + '"></span></button>';
    el.innerHTML = '<div class="srctabs" role="tablist">' + mk('douyin', '抖音') +
      mk('kuaishou', '快手') + '</div>';
    // 数量各自取，取不到就不显示数字 —— 标签本身照样能点，
    // 不该让一次请求失败把切换入口一起拖掉。
    fetch('/api/bridge/stats', { cache: 'no-store' }).then((r) => r.json()).then((s) => {
      const n = el.querySelector('[data-cnt="douyin"]');
      if (n) n.textContent = U.group(s.works || 0);
    }).catch(() => {});
    window.DYApi.ksStats().then((s) => {
      const n = el.querySelector('[data-cnt="kuaishou"]');
      // ⚠️ 用「台账记录数」而不是「有文件的作品数」。标签上的数字必须和这个
      //    标签页里**列出来的条数**一致 —— 列表列的就是台账记录。取 works 时，
      //    产物目录一被清理，标签就写着「快手 0」，点进去却有一千多条
      //    （2026-11-01 用户报的「快手数据显示为 0」就是这一处）。
      if (n) n.textContent = U.group(s.records != null ? s.records : (s.works || 0));
    }).catch(() => {});
  }

  /** 作者下拉：跟着当前来源换数据源。 */
  async function fillAuthorSelect() {
    const sel = document.getElementById('ar-author');
    if (!sel) return;
    sel.innerHTML = '<option value="">全部作者</option>';
    try {
      const authors = src === 'kuaishou'
        ? await window.DYApi.ksAuthors()
        : await window.DYApi.bridgeAuthors();
      sel.innerHTML = '<option value="">全部作者</option>' +
        authors.map((a) => '<option value="' + esc(a.nickname) + '"' +
          (f.author === a.nickname ? ' selected' : '') + '>' +
          esc(a.nickname) + '（' + a.works + '）</option>').join('');
    } catch (e) { /* 读不到就留空，不挡别的功能 */ }
  }

  /** 2026-11-08：快手与抖音**同模板** —— 筛选器不再按平台隐藏，
   * 两个 tab 的页面结构完全一致（收藏筛选对快手无效果，行为层已兜住）。 */
  function toggleSrcFilters() {
    const ty = document.getElementById('ar-type');
    if (ty) ty.hidden = false;
    const fv = document.getElementById('ar-fav');
    if (fv) fv.hidden = false;
  }

  function switchSrc(next) {
    if (!next || next === src) return;
    src = next;
    // 两个库的字段与能力都不同，筛选条件不能带过去 ——
    // 否则「图文」这个只在抖音存在的类型会悄悄把快手列表筛成空。
    f = { q: '', type: 'all', favOnly: false, sort: 'new', author: '' };
    const si = document.getElementById('ar-search');
    if (si) si.value = '';
    const ty = document.getElementById('ar-type');
    if (ty) ty.value = 'all';
    const fv = document.getElementById('ar-fav');
    if (fv) fv.classList.remove('is-on');
    toggleSrcFilters();
    renderSrcTabs();
    fillAuthorSelect();
    renderStatsBridge();
    reloadBridge(true);
  }

  async function renderStatsBridge() {
    const el = document.getElementById('ar-stats');
    if (src === 'kuaishou') {
      try {
        const s = await window.DYApi.ksStats();
        if (el) el.innerHTML = C.stats([
          { label: '作品总数', value: U.group(s.records || 0),
            // ⚠️ 头条取「台账记录数」，不取「文件还在的数量」。
            //    作品档案的列表列的就是台账记录，两处口径必须一致；曾经头条取
            //    works（有文件的），产物目录一被清理，1,018 条记录就显示成 0，
            //    看着像"数据没写进去"（2026-11-01 用户报的就是这个）。
            hint: (s.records > s.works)
              ? '台账记录数；其中 ' + U.group(s.works || 0) + ' 个文件还在磁盘上'
              : '台账记录，文件都在磁盘上' },
          /* 2026-11-08：旧目录（E://录屏//kssp）清理后文件全部丢失 ——
           * 如实告诉用户封面/播放为什么不可用，以及怎么找回。 */
          { label: '作者数', value: String(s.authors || 0), hint: '按落盘目录去重' },
          { label: '总体积', value: U.bytes(s.sizeBytes || 0), hint: '本地文件实际占用' },
          { label: '最近下载', value: esc(s.lastTime ? U.fmtDate(s.lastTime) : '-'), hint: '按文件修改时间' },
        ]) + (s.ledgers && s.ledgers.length
          ? '<div class="small faint mt-2">数据来自 ' + s.ledgers.length + ' 处台账：' +
            s.ledgers.map((p) => esc(p)).join(' ｜ ') + '</div>'
          : '');
      } catch (e) {
        if (el) el.innerHTML = '';
      }
      return;
    }
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
      const d = src === 'kuaishou'
        ? await window.DYApi.ksWorks({
            author: f.author, q: f.q, limit: PAGE, offset: bridgeState.offset })
        : await window.DYApi.bridgeWorks({
            author: f.author, type: f.type, q: f.q, limit: PAGE, offset: bridgeState.offset });
      let items = d.items;
      // 收藏只有抖音侧有；快手台账没有收藏概念，不在这一层做无意义的过滤
      if (f.favOnly && src !== 'kuaishou') items = items.filter((w) => w.fav);
      bridgeState.items = reset ? items : bridgeState.items.concat(items);
      bridgeState.total = d.total;
      bridgeState.coversPending = (d.covers && d.covers.pending) || 0;   // 快手封面待生成数
      bridgeState.offset += PAGE;
      renderBridgeList();
    } catch (e) {
      box.innerHTML = '<div class="error-box"><h3>' +
        (src === 'kuaishou' ? '读取快手台账失败' : '读取数据库失败') +
        '</h3><p class="muted">' + esc(String(e.message || e)) + '</p></div>';
    } finally {
      bridgeState.loading = false;
    }
  }

  function renderBridgeList() {
    const box = document.getElementById('ar-list');
    const more = document.getElementById('ar-more');
    if (!box) return;
    const shown = bridgeState.items;
    const filtering = !!(f.q || f.type !== 'all' || f.favOnly || f.author);
    box.innerHTML = shown.length
      ? '<div class="grid grid--4">' + shown.map((w) => C.workCard(w)).join('') + '</div>'
      : (filtering
        ? C.empty('search', 'search')
        : '<div class="empty"><div class="empty__icon">' + I.icon('folder', 32) + '</div>' +
          '<div class="empty__title">' +
          (src === 'kuaishou' ? '没有读到快手作品' : '数据库暂无作品记录') + '</div>' +
          '<div class="empty__desc">' +
          (src === 'kuaishou'
            ? '快手台账（download_log.json）里还没有能对上文件的作品记录'
            : '完成第一次下载后会自动出现在这里') + '</div></div>');
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
    let rows, fname, tip;
    if (window.DYApi.isBridge()) {
      const ks = src === 'kuaishou';
      rows = bridgeState.items.map((w) =>
        // 快手台账不记发布时间，用文件时间顶上（也是列表排序用的那个值）
        [ks ? w.downloadTime : w.createTime, w.type, w.creatorName, w.title,
         w.awemeId, w.filePath].join('\t'));
      fname = ks ? 'kuaishou-manifest.tsv' : 'archive-manifest.tsv';
      tip = ks ? '已导出快手作品清单（TSV）' : '已导出 TSV 清单（对应工程的 download_manifest.jsonl）';
    } else {
      rows = window.DYStore.state.works.map((w) =>
        [w.createTime, w.type, w.creatorName, w.title, w.awemeId, w.filePath].join('\t'));
      fname = 'archive-manifest.tsv';
      tip = '已导出 TSV 清单（对应工程的 download_manifest.jsonl）';
    }
    const blob = new Blob(['\ufeff' + rows.join('\n')], { type: 'text/tab-separated-values;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = fname;
    a.click();
    URL.revokeObjectURL(a.href);
    C.toast(tip, 'ok');
  }

  function onAction(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    if (window.DYApi.isBridge()) {
      const w = bridgeState.items.find((x) => x.id === id);
      if (!w) return;
      const ks = src === 'kuaishou';
      if (act === 'play') {
        C.mediaPlayer(w);
      } else if (act === 'fav') {
        if (ks) return;                       // 快手卡片上不会有这个按钮，防御性返回
        window.DYApi.toggleBridgeFav(w.awemeId);
        w.fav = !w.fav;
        renderBridgeList();
      } else if (act === 'path') {
        U.copyText(w.filePath).then((ok) => C.toast(ok ? '路径已复制' : '复制失败', ok ? 'ok' : 'err'));
      } else if (act === 'folder') {
        // 本地能找到文件就直接在资源管理器里选中它 —— 比弹一串路径有用得多。
        // 两个平台的文件定位走各自接口（后端 explorer /select）。
        if (w.localKind) {
          (ks ? window.DYApi.ksRevealMedia(w.photoId || w.awemeId)
              : window.DYApi.revealMedia(w.awemeId))
            .then(() => C.toast('已在资源管理器中定位该文件', 'ok'))
            .catch((err) => C.toast(err.message || '打开失败', 'err'));
        } else {
          openPathModal(w, ks);
        }
      }
      return;
    }
    const w = window.DYStore.state.works.find((x) => x.id === id);
    if (!w) return;
    if (act === 'play') {
      C.mediaPlayer(w);
    } else if (act === 'fav') {
      window.DYStore.toggleWorkFav(id);
      renderListMock();
    } else if (act === 'path') {
      U.copyText(w.filePath).then((ok) => C.toast(ok ? '路径已复制' : '复制失败', ok ? 'ok' : 'err'));
    } else if (act === 'folder') {
      openPathModal(w);
    }
  }

  function openPathModal(w, ks) {
    C.open(C.modalFrame('文件位置',
      '<p class="small muted mb-3">浏览器沙箱不能直接打开本地目录。以下为该作品的落盘路径' +
      (ks ? '（快手台账里记录的真实路径）' : '（与工程 <code>Downloaded/</code> 目录结构一致），在工程根目录即可找到对应文件') +
      '。</p>' +
      '<code class="mono small" style="display:block;padding:12px;background:var(--panel-2);border-radius:8px;' +
      'word-break:break-all">' + esc(w.filePath || '（台账未记录路径）') + '</code>',
      '<button class="btn btn--ghost" data-modal-close>关闭</button>' +
      '<button class="btn btn--primary" id="__copy_path">复制路径</button>'));
    document.getElementById('__copy_path').addEventListener('click', () => {
      U.copyText(w.filePath).then((ok) => C.toast(ok ? '路径已复制' : '复制失败', ok ? 'ok' : 'err'));
    });
  }

  window.DYRouter.register('archive', render);

  /* ================= 查找重复视频（抖音 + 快手） ================= */
  // 判定 = 字节完全相同（SHA-256），不做标题/时长模糊匹配，避免把
  // 「同名不同内容」或「直播分段」误判成重复。删除只进回收站、可恢复。

  function platLabel(p) { return p === 'douyin' ? '抖音' : '快手'; }

  function openDedup() {
    C.open(
      '<div class="modal__head"><div class="modal__title">查找重复视频</div>' +
      '<button class="btn btn--ghost btn--icon" data-modal-close aria-label="关闭">' + I.icon('close', 18) + '</button></div>' +
      '<div class="modal__body" id="dd-body">' +
        '<div class="muted">正在准备扫描抖音 + 快手…</div>' +
      '</div>' +
      '<div class="modal__foot" id="dd-foot"></div>',
      { wide: true });
    runDedupScan();
  }

  async function runDedupScan() {
    const body = document.getElementById('dd-body');
    if (!body) return;
    try {
      await window.DYApi.dedupScan();
    } catch (e) {
      body.innerHTML = '<div class="error-box"><h3>扫描启动失败</h3><p class="muted">' +
        esc(String((e && e.message) || e)) + '</p></div>';
      return;
    }
    // 轮询进度直到扫描结束（哈希在后台线程里跑）
    for (;;) {
      let s;
      try { s = await window.DYApi.dedupStatus(); }
      catch (e) {
        body.innerHTML = '<div class="error-box"><h3>读取扫描状态失败</h3><p class="muted">' +
          esc(String((e && e.message) || e)) + '</p></div>';
        return;
      }
      if (s.error) {
        body.innerHTML = '<div class="error-box"><h3>扫描出错</h3><p class="muted">' +
          esc(s.error) + '</p></div>';
        return;
      }
      if (s.running) {
        renderDedupProgress(s);
        await new Promise((res) => setTimeout(res, 700));
      } else {
        renderDedupResult(s);
        return;
      }
    }
  }

  function renderDedupProgress(s) {
    const body = document.getElementById('dd-body');
    if (!body) return;
    const p = s.progress || {};
    const done = p.done || 0, total = p.total || 0;
    body.innerHTML =
      '<div class="muted mb-3">正在扫描抖音 + 快手（已扫描 ' + U.group(done) +
      (total ? ' / ' + U.group(total) : '') + ' 个视频）…</div>' +
      C.progressIndeterminate();
  }

  function renderDedupResult(s) {
    const body = document.getElementById('dd-body');
    const foot = document.getElementById('dd-foot');
    if (!body) return;
    const r = s.result || {};
    if (!r.group_count) {
      body.innerHTML =
        '<div class="empty"><div class="empty__icon">' + I.icon('check', 32) + '</div>' +
        '<div class="empty__title">没有发现重复视频</div>' +
        '<div class="empty__desc">已扫描抖音 + 快手共 ' + U.group(r.scanned || 0) +
        ' 个视频，没有字节完全相同的内容（跨平台转码后的同名视频内容不同，不会被误判）。</div></div>';
      if (foot) foot.innerHTML = '<button class="btn btn--primary" data-modal-close>关闭</button>';
      return;
    }
    body.innerHTML =
      '<div style="margin-bottom:14px">找到 <b class="num">' + r.group_count + '</b> 组重复，共 ' +
      '<b class="num">' + r.dup_count + '</b> 个冗余文件，可释放 <b class="num">' +
      U.bytes(r.reclaim_bytes || 0) + '</b>。</div>' +
      '<div class="small muted" style="margin-bottom:12px">每组保留一个（优先保留非辅助目录、体积大、时间新的），其余移入回收站。</div>' +
      '<div style="max-height:52vh;overflow:auto">' +
        r.groups.map((g, i) => dedupGroupHtml(g, i)).join('') +
      '</div>';
    if (foot) {
      foot.innerHTML =
        '<button class="btn btn--ghost" data-modal-close>取消</button>' +
        '<button class="btn btn--danger" id="dd-do">保留一个并删除其余（' + r.dup_count + ' 个 → 回收站）</button>';
      document.getElementById('dd-do').addEventListener('click', () => confirmDedupDelete(r));
    }
  }

  function dedupGroupHtml(g, i) {
    const keep = g.keep, dupes = g.dupes;
    const row = (tag, cls, d) =>
      '<div style="display:flex;align-items:center;gap:10px;padding:8px 10px;border-bottom:1px solid var(--line)">' +
      '<span style="flex:0 0 auto;font-size:11px;padding:2px 7px;border-radius:6px;' +
      (cls === 'keep'
        ? 'color:var(--bb-ok,var(--green));background:var(--bb-ok-soft,var(--green-soft));'
        : 'color:var(--bb-danger);background:var(--bb-danger-soft);') + '">' +
      (tag === 'keep' ? '保留' : '删除') + '</span>' +
      '<span class="dd-name" title="' + esc(d.path) + '" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
      esc(d.name) + '</span>' +
      '<span class="small muted" style="flex:0 0 auto">' + platLabel(d.platform) + ' · ' + U.bytes(d.size || 0) + '</span>' +
      '</div>';
    return '<div style="border:1px solid var(--line);border-radius:8px;margin-bottom:10px;overflow:hidden">' +
      '<div style="padding:7px 10px;font-weight:600;background:var(--panel-2)">重复组 ' + (i + 1) +
      ' · 保留 1 个，删除 ' + dupes.length + ' 个</div>' +
      row('keep', 'keep', keep) +
      dupes.map((d) => row('del', 'del', d)).join('') +
      '</div>';
  }

  function confirmDedupDelete(r) {
    const dupPaths = [];
    let totalBytes = 0;
    r.groups.forEach((g) => g.dupes.forEach((d) => { dupPaths.push(d.path); totalBytes += (d.size || 0); }));
    C.open(
      '<div class="modal__head"><div class="modal__title">确认删除重复视频</div>' +
      '<button class="btn btn--ghost btn--icon" data-modal-close aria-label="关闭">' + I.icon('close', 18) + '</button></div>' +
      '<div class="modal__body">' +
        '<div class="danger-note">' +
          '<div class="danger-note__title">' + I.icon('alert', 14) + '删除后会发生什么</div>' +
          '<ul class="small">' +
            '<li>把 <b>' + dupPaths.length + '</b> 个冗余文件（共 <b>' + U.bytes(totalBytes) + '</b>）移入 <b>Windows 回收站</b></li>' +
            '<li><b>不是永久删除</b>，可随时从回收站还原</li>' +
            '<li>每组重复视频<b>保留 1 个</b>，其余才删</li>' +
            '<li><b>只删磁盘文件</b>，不改数据库、不改快手台账</li>' +
            '<li>删除后「作品档案」里每个作品仍显示（保留的那份还在）</li>' +
          '</ul>' +
        '</div>' +
      '</div>' +
      '<div class="modal__foot">' +
        '<button class="btn btn--ghost" data-modal-close>取消</button>' +
        '<button class="btn btn--danger" id="dd-confirm-ok">确认移入回收站</button>' +
      '</div>');
    document.getElementById('dd-confirm-ok').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = '正在处理…';
      try {
        const res = await window.DYApi.dedupDelete(dupPaths);
        C.close();
        C.toast('已移入回收站 ' + res.deleted + ' 个文件' +
          (res.failed ? '，失败 ' + res.failed + ' 个' : ''),
          res.failed ? 'err' : 'ok');
        renderStatsBridge();
      } catch (err) {
        btn.disabled = false;
        btn.textContent = '确认移入回收站';
        C.toast(String((err && err.message) || err), 'err');
      }
    });
  }
})();
