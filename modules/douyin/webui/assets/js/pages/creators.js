/* ============================================================
 * pages/creators.js — 关注管理
 * ------------------------------------------------------------
 * 对照参考截图 004：同步状态条 → 搜索/排序/视图/多选工具行
 * → 筛选 chips → 统计行 → 作者卡片列表（网格/列表双视图）
 * 卡片：圆形头像 + 昵称(认证徽章) + 抖音号 + 简介 + 数据行 + 标签行
 *       + 右侧「下载」主按钮 + 「⋯」更多
 *
 * bridge 模式下两个平台的作者都能管，顶部切换来源：
 *   · 抖音 → aweme 表按 author_name 聚合，作者带 author_sec_uid（能拼主页链接）
 *   · 快手 → 台账按落盘目录聚合，作者主页 ID 靠作品清单推断（见后端 ks_authors）
 * 两边的「更新」都是「只看最新 N 个 + 增量去重」，只是走的下载器不同。
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  let ui = { q: '', sort: 'recent', view: 'list', chips: {}, grid: {} };
  // 来源：'douyin' | 'kuaishou'。两边的字段、能力、下载器都不同，所以是切换不是混排。
  let src = 'douyin';
  // 两个来源的作者清单各一份缓存 + 各自的 in-flight Promise。
  // 打开「关注管理」时来源标签 / 同步状态条 / 列表会各拉一次 authors，三次是同一份
  // 全表聚合查询（加索引前单次 300ms+）；这里保证同一时刻每个来源只真正发一次请求。
  const authorCache = { douyin: null, kuaishou: null };
  const authorInflight = { douyin: null, kuaishou: null };
  /* 「未加入关注」清单（只在单作品链接里出现过的作者）。
   * 与 authorCache 分开缓存：主列表/统计/一键更新都只该看关注中的作者，
   * 这份清单只服务于列表下面那条提示与「加入关注」弹窗。 */
  const othersCache = { douyin: null, kuaishou: null };
  const NOTES_KEY = 'dyui.creatorNotes';

  function localNotes() { return window.DYUtils.ls.get(NOTES_KEY, {}); }
  function setLocalNote(nickname, note) {
    const m = localNotes();
    if (note) m[nickname] = note; else delete m[nickname];
    window.DYUtils.ls.set(NOTES_KEY, m);
  }

  /** 取某个来源的作者清单（带缓存 + in-flight 去重）。删/增后置 null 重新拉。 */
  async function loadAuthors(which) {
    const s = which || src;
    if (authorCache[s]) return authorCache[s];
    if (authorInflight[s]) return authorInflight[s];
    authorInflight[s] = (s === 'kuaishou'
      ? window.DYApi.ksAuthors()
      : window.DYApi.bridgeAuthors())
      .then((a) => { authorCache[s] = a; return a; })
      .finally(() => { authorInflight[s] = null; });
    return authorInflight[s];
  }

  function render(view) {
    view.className = 'content';
    delete view.dataset.platform;
    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div class="page-head__row">' +
          '<div><div class="crumb"><span class="crumb__dot"></span>账号内容 · 关注</div>' +
          '<h1 class="page-title">关注</h1></div>' +
          '<button class="btn btn--primary btn--sm" id="cr-add">' + I.icon('plus', 14) + '添加作者</button>' +
        '</div>' +
      '</div>' +

      /* 来源切换 */
      '<div id="cr-src"></div>' +

      /* 同步状态条 */
      '<div class="row-item mb-4" id="cr-syncbar"></div>' +

      /* 工具行 */
      '<div class="flex flex-wrap mb-3" style="gap:12px">' +
        '<div style="position:relative;flex:2;min-width:220px">' +
          '<input class="input" id="cr-search" placeholder="搜索昵称 / 账号 / 备注" aria-label="搜索作者" ' +
          'style="padding:9px 13px 9px 36px;font-family:var(--font-sans)">' +
          '<span class="i" style="position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--faint)">' +
          I.icon('search', 15) + '</span>' +
        '</div>' +
        '<select class="input" id="cr-sort" style="flex:0 0 170px;padding:9px 36px 9px 13px;font-family:var(--font-sans)" aria-label="排序">' +
          '<option value="recent">最近同步</option><option value="new">有新作品优先</option>' +
          '<option value="works">作品数最多</option><option value="name">昵称 A-Z</option>' +
        '</select>' +
        '<div class="tabs" role="tablist" aria-label="视图切换">' +
          '<button class="tab" data-cview="grid" aria-label="网格视图">' + I.icon('image', 14) + '</button>' +
          '<button class="tab" data-cview="list" aria-label="列表视图">' + I.icon('tasks', 14) + '</button>' +
        '</div>' +
      '</div>' +

      /* 筛选 chips */
      '<div class="chips mb-3" id="cr-chips">' +
        chip('new', '有新作品') + chip('mutual', '互关') + chip('note', '有备注') +
        chip('never', '未下载过') + chip('auto', '自动同步') +
      '</div>' +

      '<div id="cr-statline" class="small muted mb-3"></div>' +
      '<div id="cr-list"></div>' +
      /* 未加入关注的作者（只下过单作品链接的那几位）—— 主列表下面一条提示，
       * 平时不占版面：没有这样的人时整块是空的。 */
      '<div id="cr-others" class="mt-4"></div>';

    if (window.DYApi.isBridge()) {
      const addBtn = view.querySelector('#cr-add');
      if (addBtn) addBtn.style.display = 'none';
      const chips = view.querySelector('#cr-chips');
      if (chips) chips.style.display = 'none';
    }

    renderSrcTabs();
    renderSyncBar();
    renderList();
    renderOthers();

    view.querySelector('#cr-search').addEventListener('input', U.debounce((e) => {
      ui.q = e.target.value.trim().toLowerCase();
      renderList();
    }, 150));
    view.querySelector('#cr-sort').addEventListener('change', (e) => { ui.sort = e.target.value; renderList(); });
    view.querySelector('#cr-add').addEventListener('click', openAdd);
    view.querySelector('#cr-src').addEventListener('click', (e) => {
      const b = e.target.closest('[data-src]');
      if (b) switchSrc(b.dataset.src);
    });
    view.querySelector('#cr-chips').addEventListener('click', (e) => {
      const c = e.target.closest('[data-chip]');
      if (!c) return;
      ui.chips[c.dataset.chip] = !ui.chips[c.dataset.chip];
      c.classList.toggle('is-on', ui.chips[c.dataset.chip]);
      renderList();
    });
    view.querySelectorAll('[data-cview]').forEach((b) => {
      b.classList.toggle('is-on', b.dataset.cview === ui.view);
      b.addEventListener('click', () => {
        ui.view = b.dataset.cview;
        view.querySelectorAll('[data-cview]').forEach((x) => x.classList.toggle('is-on', x === b));
        renderList();
      });
    });
    view.querySelector('#cr-list').addEventListener('click', onCardAction);
    view.querySelector('#cr-syncbar').addEventListener('click', (e) => {
      if (e.target.closest('#cr-sync-all')) syncAll(view);
      else if (e.target.closest('#cr-update-all')) updateAll(view);
    });

    U.bus.on('creator:synced', () => renderSyncBar());
    U.bus.on('store:reset', () => { renderSyncBar(); renderList(); });

    /* 预取后端存的网页端设置（`creators_update_*`）。
     * ⚠️ 必须预取：`updateLimit()` 是**同步**函数（渲染 title 时就在调），
     *    而设置页回填是异步的。这里在进入页面时先拿一次并缓存到
     *    `window.__ksWebuiSettings`，之后 `updateLimit()` 直接读缓存。
     *    拿不到就让它回落默认值 —— 绝不因为读设置失败而拦住下载。 */
    if (window.DYApi && window.DYApi.isBridge() && window.DYApi.bridgeWebuiSettings) {
      window.DYApi.bridgeWebuiSettings()
        .then((r) => { window.__ksWebuiSettings = (r && r.settings) || {}; })
        .catch(() => { window.__ksWebuiSettings = {}; });
    }
  }

  /* —— 来源标签 ——
   * 只有 bridge 模式才有两个平台的作者库可切（演示模式只有一份假数据）。 */
  async function renderSrcTabs() {
    const el = document.getElementById('cr-src');
    if (!el) return;
    if (!window.DYApi.isBridge()) { el.innerHTML = ''; return; }
    const mk = (k, name) => '<button class="srctab' + (src === k ? ' is-on' : '') +
      '" data-src="' + k + '" role="tab" aria-selected="' + (src === k) + '">' + name +
      '<span class="srctab__n" data-cnt="' + k + '"></span></button>';
    el.innerHTML = '<div class="srctabs" role="tablist">' + mk('douyin', '抖音') +
      mk('kuaishou', '快手') + '</div>';
    try {
      const a = await loadAuthors('douyin');
      const n = el.querySelector('[data-cnt="douyin"]');
      if (n) n.textContent = a.length + ' 位';
    } catch (e) { /* 数量取不到就不显示，标签照样可点 */ }
    try {
      const a = await loadAuthors('kuaishou');
      const n = el.querySelector('[data-cnt="kuaishou"]');
      if (n) n.textContent = a.length + ' 位';
    } catch (e) { /* 同上 */ }
  }

  function switchSrc(next) {
    if (!next || next === src) return;
    src = next;
    // 缓存按来源各存一份，换平台不用作废缓存（loadAuthors 会按 src 命中对应那份）。
    ui.q = '';
    ui.sort = 'recent';
    const si = document.getElementById('cr-search');
    if (si) si.value = '';
    const so = document.getElementById('cr-sort');
    if (so) so.value = 'recent';
    renderSrcTabs();
    renderSyncBar();
    renderList();
  }

  function chip(key, label) {
    return '<button class="chip' + (ui.chips[key] ? ' is-on' : '') + '" data-chip="' + key + '">' + esc(label) + '</button>';
  }

  /* —— 同步状态条（bridge：数据库概览；mock：同步控制） —— */
  async function renderSyncBar() {
    const el = document.getElementById('cr-syncbar');
    if (!el) return;
    if (window.DYApi.isBridge()) {
      try {
        const authors = await loadAuthors();
        const totalWorks = authors.reduce((a, c) => a + c.works, 0);
        /* 能不能"一键更新"取决于作者主页链接是否齐全：
         *   抖音 —— author_sec_uid 拼出来（aweme 表里有）
         *   快手 —— 靠下载时留下的作品清单推断；推不出来的作者要用户手动填一次
         * 没有链接就把按钮禁掉并说明原因，不给一个点了必然报错的按钮。 */
        const updatable = authors.filter((a) => a.url).length;
        const srcLabel = src === 'kuaishou' ? '来自快手台账（download_log.json）' : '来自工程数据库';
        const tip = src === 'kuaishou'
          ? '没有主页链接的作者，先在卡片上点「设置主页」填一次'
          : '作者缺少 sec_uid，无法构造主页链接';
        el.innerHTML =
          '<div class="side-card__icon">' + I.icon('database', 17) + '</div>' +
          '<div class="row-item__main">' +
            '<div class="row-item__meta" style="margin:0">' +
              '<span>' + esc(srcLabel) + '</span>' +
              '<span>作者 <b class="num">' + authors.length + '</b> 位</span>' +
              '<span>作品 <b class="num">' + U.group(totalWorks) + '</b> 条</span>' +
              '<span style="color:var(--green)">' + I.icon('circleCheck', 13) + ' 已连接</span>' +
            '</div>' +
          '</div>' +
          '<div class="row-item__actions">' +
            '<button class="btn btn--primary btn--sm" id="cr-update-all"' +
              (updatable ? '' : ' disabled title="' + esc(tip) + '"') + '>' +
              I.icon('refresh', 14) + '一键更新最新作品</button>' +
          '</div>';
        return;
      } catch (e) {
        el.innerHTML = '<div class="row-item__main"><div class="row-item__meta" style="margin:0;color:var(--red)">' +
          (src === 'kuaishou' ? '读取快手台账失败：' : '读取数据库失败：') +
          esc(String(e.message || e)) + '</div></div>';
        return;
      }
    }
    const cs = window.DYStore.state.creators;
    const last = cs.reduce((acc, c) => {
      if (!c.lastSync) return acc;
      return !acc || c.lastSync > acc ? c.lastSync : acc;
    }, null);
    const autoN = cs.filter((c) => c.autoSync).length;
    el.innerHTML =
      '<div class="side-card__icon">' + I.icon('refresh', 17) + '</div>' +
      '<div class="row-item__main">' +
        '<div class="row-item__meta" style="margin:0">' +
          '<span>上次同步：<b style="color:var(--text)">' + esc(last ? U.fromNow(last) : '从未') + '</b></span>' +
          '<span>共 <b class="num">' + cs.length + '</b> 位作者</span>' +
          '<span>自动同步 <b class="num">' + autoN + '</b> 位</span>' +
        '</div>' +
      '</div>' +
      '<div class="row-item__actions">' +
        '<button class="btn btn--ghost btn--sm" id="cr-sync-all">' + I.icon('refresh', 14) + '全部同步</button>' +
      '</div>';
  }

  /* —— 过滤与排序（bridge：缓存的真实作者；mock：本地作者） —— */
  function filtered() {
    if (window.DYApi.isBridge()) {
      let cs = (authorCache[src] || []).slice();
      if (ui.q) cs = cs.filter((c) =>
        c.nickname.toLowerCase().includes(ui.q) ||
        (c.douyinId || '').toLowerCase().includes(ui.q) ||
        // 工具行的 placeholder 写着「备注」，搜索就该真的能搜到备注
        (localNotes()[c.nickname] || '').toLowerCase().includes(ui.q));
      const by = {
        recent: (a, b) => (b.lastWorkTime || '').localeCompare(a.lastWorkTime || ''),
        new: (a, b) => b.works - a.works,
        works: (a, b) => b.works - a.works,
        name: (a, b) => a.nickname.localeCompare(b.nickname, 'zh-Hans-CN'),
      };
      return cs.sort(by[ui.sort] || by.recent);
    }
    let cs = window.DYStore.state.creators.slice();
    if (ui.q) {
      cs = cs.filter((c) =>
        c.nickname.toLowerCase().includes(ui.q) ||
        (c.douyinId || '').toLowerCase().includes(ui.q) ||
        (c.note || '').toLowerCase().includes(ui.q));
    }
    const f = ui.chips;
    if (f.new) cs = cs.filter((c) => c.newWorks > 0);
    if (f.mutual) cs = cs.filter((c) => c.mutual);
    if (f.note) cs = cs.filter((c) => c.note);
    if (f.never) cs = cs.filter((c) => c.lastDownload === '未下载过');
    if (f.auto) cs = cs.filter((c) => c.autoSync);
    const by = {
      recent: (a, b) => (b.lastSync || '').localeCompare(a.lastSync || ''),
      new: (a, b) => b.newWorks - a.newWorks,
      works: (a, b) => b.works - a.works,
      name: (a, b) => a.nickname.localeCompare(b.nickname, 'zh-Hans-CN'),
    };
    cs.sort(by[ui.sort] || by.recent);
    return cs;
  }

  /* —— 作者卡片（bridge 与 mock 共用骨架；空字段如实隐藏） —— */
  function creatorCard(c) {
    const note = window.DYApi.isBridge() ? (localNotes()[c.nickname] || '') : (c.note || '');
    const isBridge = window.DYApi.isBridge();
    const ks = isBridge && src === 'kuaishou';
    const tags = [];
    if (isBridge) {
      tags.push('<span class="badge badge--cyan">最近作品 ' + esc(c.lastDownload) + '</span>');
      if (ks) {
        // 快手台账没有发布时间，「最近作品」其实就是文件时间 —— 标签上不写这个细节，
        // 但把"多少条真的有文件 / 占多大"补上，那才是快手侧真正关心的。
        tags.push('<span class="badge">' + U.group(c.withFile || 0) + ' 个有文件</span>');
        if (c.sizeMB) tags.push('<span class="badge">' + esc(String(c.sizeMB)) + ' MB</span>');
        if (c.profileSource === 'manual') tags.push('<span class="badge badge--amber">主页手动填写</span>');
      }
      if (note) tags.push('<span class="badge badge--pink" title="' + esc(note) + '">备注: ' + esc(U.truncate(note, 12)) + '</span>');
    } else {
      if (c.newWorks > 0) tags.push('<span class="badge badge--amber">新作品 +' + c.newWorks + '</span>');
      if (c.mutual) tags.push('<span class="badge badge--cyan">互关</span>');
      if (note) tags.push('<span class="badge badge--pink" title="' + esc(note) + '">备注: ' + esc(U.truncate(note, 12)) + '</span>');
      tags.push('<span class="badge">' + esc(c.lastDownload || '未下载') + '</span>');
    }
    /* 主操作按钮：
     *   抖音 —— 有 sec_uid 才给「更新」（没有就构造不出主页链接）
     *   快手 —— 主页 ID 靠作品清单推断，推不出来的作者给「设置主页」让用户填一次；
     *           不放「下载」主按钮：快手的"下载"是全量，几百个作品不该占主按钮位，
     *           它挪到「⋯」里并带确认。 */
    let mainBtn = '';
    if (isBridge && c.url) {
      mainBtn = '<button class="btn btn--ghost btn--sm" data-act="update" data-id="' + esc(c.id) + '" ' +
        'title="只检查该作者' + updateLimitText() + '作品，已下载的跳过' +
        (ks ? '' : '；画质 ' + esc(qualityLabel(updateQuality()))) + '">' +
        I.icon('refresh', 14) + '更新</button>';
    } else if (ks) {
      mainBtn = '<button class="btn btn--ghost btn--sm" data-act="set-profile" data-id="' + esc(c.id) + '" ' +
        'title="台账推断不出作者 ID，填一次主页链接就能更新了">' +
        I.icon('link', 14) + '设置主页</button>';
    }
    return (
      '<div class="row-item" data-creator="' + esc(c.id) + '">' +
        C.avatar(c.nickname, c.colorIndex, 'lg') +
        '<div class="row-item__main">' +
          '<div class="flex" style="gap:8px">' +
            '<span class="row-item__title" style="flex:none;max-width:60%">' + C.ellipsisTitle(c.nickname, 22) + '</span>' +
            (c.verified ? '<span class="badge badge--cyan" style="gap:3px">' + I.icon('check', 11) + '已认证</span>' : '') +
            (c.douyinId ? '<span class="mono small faint">' + esc(c.douyinId) + '</span>' : '') +
          '</div>' +
          (c.bio ? '<div class="row-item__meta"><span class="ellipsis" style="max-width:52ch">' + esc(c.bio) + '</span></div>' : '') +
          '<div class="row-item__meta"><span>作品 <b class="num">' + U.group(c.works) + '</b></span>' +
            (c.fans != null ? '<span>粉丝 <b class="num">' + U.compact(c.fans) + '</b></span>' : '') +
            (c.modes && !isBridge ? '<span>模式 <b>' + esc(c.modes.join('+')) + '</b></span>' : '') +
          '</div>' +
          '<div class="row-item__meta">' + tags.join('') + '</div>' +
        '</div>' +
        '<div class="row-item__actions">' +
          mainBtn +
          (ks ? '' :
            '<button class="btn btn--primary btn--sm" data-act="download" data-id="' + esc(c.id) + '">' +
            I.icon('download', 14) + '下载</button>') +
          '<button class="btn btn--ghost btn--sm btn--icon" data-act="more" data-id="' + esc(c.id) + '" aria-label="更多操作">' +
          I.icon('dots', 15) + '</button>' +
        '</div>' +
      '</div>'
    );
  }

  /* —— 网格模式小卡 —— */
  function creatorTile(c) {
    const isBridge = window.DYApi.isBridge();
    const ks = isBridge && src === 'kuaishou';
    let mainBtn = '';
    if (isBridge && c.url) {
      mainBtn = '<button class="btn btn--ghost btn--sm btn--icon" data-act="update" data-id="' + esc(c.id) + '" ' +
        'aria-label="更新该作者作品" title="只检查最新作品，已下载的跳过">' + I.icon('refresh', 13) + '</button>';
    } else if (ks) {
      mainBtn = '<button class="btn btn--ghost btn--sm btn--icon" data-act="set-profile" data-id="' + esc(c.id) + '" ' +
        'aria-label="设置主页链接" title="填一次主页链接就能更新">' + I.icon('link', 13) + '</button>';
    }
    return (
      '<div class="card" data-creator="' + esc(c.id) + '" style="text-align:center;padding:var(--sp-4)">' +
        '<div class="flex" style="justify-content:center">' + C.avatar(c.nickname, c.colorIndex, 'lg') + '</div>' +
        '<div class="small mt-3" style="font-weight:700">' + esc(U.truncate(c.nickname, 12)) + '</div>' +
        '<div class="small faint mono">' + esc(c.douyinId) + '</div>' +
        '<div class="small muted mt-2">作品 <b class="num">' + U.group(c.works) + '</b></div>' +
        (c.newWorks ? '<div class="mt-2"><span class="badge badge--amber">新 +' + c.newWorks + '</span></div>' : '') +
        '<div class="mt-3 flex" style="gap:6px;justify-content:center">' +
          mainBtn +
          (ks ? '' :
            '<button class="btn btn--primary btn--sm" data-act="download" data-id="' + esc(c.id) + '">' +
            I.icon('download', 13) + '下载</button>') +
          '<button class="btn btn--ghost btn--sm btn--icon" data-act="more" data-id="' + esc(c.id) + '" aria-label="更多操作">' +
          I.icon('dots', 14) + '</button>' +
        '</div>' +
      '</div>'
    );
  }

  async function renderList() {
    const box = document.getElementById('cr-list');
    if (!box) return;
    if (window.DYApi.isBridge()) {
      if (!authorCache[src]) {
        box.innerHTML = C.skeletonRows(3);
        try { await loadAuthors(); }
        catch (e) {
          box.innerHTML = '<div class="error-box"><h3>' +
            (src === 'kuaishou' ? '读取快手作者失败' : '读取作者库失败') +
            '</h3><p class="muted">' + esc(String(e.message || e)) + '</p></div>';
          return;
        }
      }
      const cs = filtered();
      const line = document.getElementById('cr-statline');
      if (line) line.innerHTML = '显示 ' + cs.length + ' / ' + authorCache[src].length + ' 位作者（' +
        (src === 'kuaishou' ? '快手台账' : '工程数据库') + '）';
      box.innerHTML = cs.length
        ? (ui.view === 'list'
          ? '<div class="grid" style="gap:12px">' + cs.map(creatorCard).join('') + '</div>'
          : '<div class="grid grid--4">' + cs.map(creatorTile).join('') + '</div>')
        : (ui.q ? C.empty('search', 'search')
          : (src === 'kuaishou'
            ? '<div class="empty"><div class="empty__icon">' + I.icon('users', 32) + '</div>' +
              '<div class="empty__title">快手台账里还没有作者</div>' +
              '<div class="empty__desc">用「快手下载」下过作品后，作者会自动出现在这里</div></div>'
            : C.empty('creators', 'users')));
      await renderSyncBar();
      renderOthers();
      return;
    }
    const cs = filtered();
    const newN = window.DYStore.state.creators.filter((c) => c.newWorks > 0).length;
    const line = document.getElementById('cr-statline');
    if (line) {
      line.innerHTML = newN
        ? '<span style="color:var(--amber)">' + I.icon('bolt', 13) + ' 有新作品 ' + newN + ' 人</span> · 显示 ' + cs.length + ' / ' +
          window.DYStore.state.creators.length + ' 位'
        : '显示 ' + cs.length + ' / ' + window.DYStore.state.creators.length + ' 位作者';
    }
    box.innerHTML = cs.length
      ? (ui.view === 'list'
        ? '<div class="grid" style="gap:12px">' + cs.map(creatorCard).join('') + '</div>'
        : '<div class="grid grid--4">' + cs.map(creatorTile).join('') + '</div>')
      : (Object.values(ui.chips).some(Boolean) || ui.q ? C.empty('search', 'search') : C.empty('creators', 'users'));
  }

  /* —— 未加入关注的作者 ——
   *
   * 关注列表只收「主页来源」的作者（后端 q_authors / ks_authors 的派生规则）：
   * 单作品链接下到的作品照样进「作品档案」，但不会把作者算成关注。于是这里必须
   * 有一个**明确的操作入口**把作者加进来 —— 否则那群人就成了"看得见作品、
   * 却永远进不了关注"的孤儿。这就是需求里那句「仅在用户明确操作时才加入」。
   *
   * 平时零存在感：没有这样的人，整块不渲染（列表下面不会多出一个空条）。
   */
  const WHY_TEXT = {
    single: '只下过单作品链接',
    'legacy-single': '历史单链接作品',
    manual: '已取消关注',
  };

  async function renderOthers() {
    const box = document.getElementById('cr-others');
    if (!box) return;
    if (!window.DYApi.isBridge()) { box.innerHTML = ''; return; }
    if (!othersCache[src]) {
      try { othersCache[src] = await window.DYApi.authorsUnfollowed(src); }
      catch (e) { box.innerHTML = ''; return; }   // 取不到就不显示，别在列表下面报错
    }
    const list = othersCache[src] || [];
    if (!list.length) { box.innerHTML = ''; return; }
    const works = list.reduce((a, c) => a + c.works, 0);
    box.innerHTML =
      '<div class="row-item">' +
        '<div class="side-card__icon">' + I.icon('users', 17) + '</div>' +
        '<div class="row-item__main">' +
          '<div class="row-item__title" style="font-size:14px">还有 ' + list.length +
            ' 位作者没有加入关注</div>' +
          '<div class="row-item__meta">' +
            '<span>作品 <b class="num">' + U.group(works) + '</b> 条已在「作品档案」里</span>' +
            '<span>未计入上方统计，也不会被「一键更新」带上</span>' +
          '</div>' +
        '</div>' +
        '<div class="row-item__actions">' +
          '<button class="btn btn--ghost btn--sm" id="cr-others-open">查看 / 加入关注</button>' +
        '</div>' +
      '</div>';
    const b = box.querySelector('#cr-others-open');
    if (b) b.addEventListener('click', openOthers);
  }

  /** 加入关注后统一收拾缓存：两个列表都会变（一个少一位、一个多一位）。 */
  function afterFollowChange() {
    authorCache[src] = null;
    othersCache[src] = null;
    renderSrcTabs();
    renderSyncBar();
    renderList();
  }

  function openOthers() {
    const list = (othersCache[src] || []).slice();
    if (!list.length) { C.toast('没有未加入关注的作者', 'ok'); return; }
    const rowHtml = (x, i) =>
      '<div class="row-item" data-other-row="' + i + '">' +
        C.avatar(x.nickname, i % 6, 'sm') +
        '<div class="row-item__main">' +
          '<div class="row-item__title" style="font-size:14px">' + C.ellipsisTitle(x.nickname, 22) + '</div>' +
          '<div class="row-item__meta"><span>作品 <b class="num">' + U.group(x.works) + '</b> 条</span>' +
          '<span>' + esc(WHY_TEXT[x.why] || '未加入关注') + '</span></div>' +
        '</div>' +
        '<div class="row-item__actions">' +
          '<button class="btn btn--primary btn--sm" data-add-other="' + i + '">' +
          I.icon('plus', 13) + '加入关注</button>' +
        '</div>' +
      '</div>';
    C.open(C.modalFrame('未加入关注的作者',
      '<p class="small muted" style="margin-top:0">这些作者的作品在库里，但没有被算成关注 —— ' +
      '他们只出现在「单作品链接下载」里，或者你手动取消过关注。加入关注后，他们才会出现在关注列表、' +
      '计入统计，并参与「一键更新」。</p>' +
      '<div class="grid" id="__oth_list" style="gap:10px;max-height:52vh;overflow:auto">' +
      list.map(rowHtml).join('') + '</div>',
      '<button class="btn btn--ghost" data-modal-close>关闭</button>' +
      '<button class="btn btn--primary" id="__oth_all">全部加入关注</button>'));

    const box = document.getElementById('__oth_list');
    if (!box) return;

    /* 单点加入：成功就把这一行从弹窗里摘掉（用户能立刻看到剩下几个），
     * 全加完了自动关窗并刷新页面上的两块列表。
     * `quiet` 给「全部加入」用：逐个弹 toast 会把提示刷屏，那边最后汇总一条即可。 */
    async function addOne(name, btn, rowEl, quiet) {
      if (btn) { btn.disabled = true; btn.textContent = '加入中…'; }
      try {
        await window.DYApi.authorFollow(src, name, true);
        if (rowEl) rowEl.remove();
        if (!quiet) {
          C.toast('已加入关注：' + name, 'ok');
          if (!box.querySelector('[data-add-other]')) {
            C.close();
            afterFollowChange();
          }
        }
        return true;
      } catch (e) {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = I.icon('plus', 13) + '加入关注';
        }
        if (!quiet) C.toast('加入失败：' + String((e && e.message) || e), 'err');
        return false;
      }
    }

    box.querySelectorAll('[data-add-other]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const i = parseInt(btn.dataset.addOther, 10);
        addOne(list[i].nickname, btn, btn.closest('[data-other-row]'), false);
      });
    });

    const allBtn = document.getElementById('__oth_all');
    if (allBtn) allBtn.addEventListener('click', async () => {
      allBtn.disabled = true;
      allBtn.textContent = '加入中…';
      let ok = 0, fail = 0;
      for (let i = 0; i < list.length; i++) {
        const row = box.querySelector('[data-other-row="' + i + '"]');
        if (!row) continue;                       // 已经被单独加过了
        const btn = row.querySelector('[data-add-other]');
        if (await addOne(list[i].nickname, btn, row, true)) ok++; else fail++;
      }
      C.close();
      afterFollowChange();
      C.toast(fail ? ('已加入 ' + ok + ' 位，失败 ' + fail + ' 位')
                   : ('已加入 ' + ok + ' 位作者'), fail ? 'err' : 'ok');
    });
  }

  /* —— 全部同步（演示同步，含失败态） —— */
  async function syncAll(view) {
    const cs = window.DYStore.state.creators;
    C.toast('开始同步 ' + cs.length + ' 位作者…');
    let ok = 0, fail = 0;
    for (const c of cs) {
      try { await window.DYStore.syncCreator(c.id); ok++; }
      catch (e) { fail++; }
      renderSyncBar();
      renderList();
    }
    C.toast(fail ? '同步完成：' + ok + ' 成功 / ' + fail + ' 失败（可在列表查看）' : '全部同步完成', fail ? 'err' : 'ok');
  }

  /* —— 一键更新全部作者的最新作品（真实下载） ——
   *
   * 数据从哪来：aweme 表每条记录都存了 author_sec_uid，bridgeAuthors 已经把它
   * 拼成主页链接（c.url = douyin.com/user/<sec_uid>）。有链接就能按「作品」模式
   * 增量抓取——后端 increase.post=true，会逐条比对本地库，已有的一律跳过。
   *
   * ⚠️⚠️ **每位作者取最新 N 个里的 N 曾经写死成 20 —— 那是个会静默丢片的坑**
   * （2026-10-03 修）。抖音/快手的作品列表都按时间倒序返回，所以「取最新 N 个」
   * 天然只覆盖**最新那一段**。作者若一次新增超过 N 个作品，排在第 N+1 位以后的：
   *   · 这一轮扫不到；
   *   · 下一轮**还是**只取最新 N 个 —— 新作品还在最前面，那几个永远在窗口外；
   *   · 于是它们**永久扫不到**，日志里一句提示都没有。
   * 换句话说：N=20 时，作者一次发 30 条 → 10 条永久丢失。
   * 增量去重只能省掉「已下过的」，**省不掉「没被扫到的」** —— 两者是不同的漏。
   *
   * 现在 N 来自「设置 → 增量与去重 → 一键更新每人取最新」，0 = 不限。
   * 为什么默认给 30 而不是更大：作品列表按时间倒序，**一次新增超过 30 条**
   * 本身相当罕见，而 N 越大每位作者要翻的分页越多（慢、且更容易撞风控）。
   * 真要确认「一个都不漏」，把它设成 0。
   */
  function updateLimit() {
    /* 读用户设置；读不到/非法一律回落到默认 30（写死 20 的教训：
     * 宁可多扫，也不要有个小到会静默丢片的默认值）。
     * ⚠️ 设置页在 bridge 模式下会把值回填进 DYStore.state.settings，
     *    但那是**异步**的（要先 GET config.yml）。所以这里额外同步读一次
     *    后端存的网页端设置，保证「刚改完设置就来点更新」也能立刻生效，
     *    不受页面加载顺序影响。读失败一律忽略（回落默认，不阻塞下载）。 */
    const DEF = (window.DYConfig && window.DYConfig.DEFAULT_SETTINGS
                 && window.DYConfig.DEFAULT_SETTINGS.creators_update_limit) || 30;
    try {
      if (window.DYApi && window.DYApi.isBridge() && window.DYApi.bridgeWebuiSettings) {
        const w = window.__ksWebuiSettings || {};
        if (w.creators_update_limit !== undefined && w.creators_update_limit !== null
            && w.creators_update_limit !== '') {
          const n = parseInt(w.creators_update_limit, 10);
          if (!isNaN(n) && n >= 0) return n;
        }
      }
    } catch (e) { /* 忽略：回落到下面读本地 */ }
    try {
      const s = window.DYStore && window.DYStore.state && window.DYStore.state.settings;
      const raw = s ? s.creators_update_limit : undefined;
      if (raw === undefined || raw === null || raw === '') return DEF;
      const n = parseInt(raw, 10);
      if (isNaN(n) || n < 0) return DEF;
      return n;               // 0 = 不限，交给后端按「扫到没有新的」处理
    } catch (e) {
      return DEF;
    }
  }

  /* 给按钮 title / 菜单说明用的一句人话（渲染路径也读设置，所以不能写死）。 */
  function updateLimitText() {
    const n = updateLimit();
    return n === 0 ? '不限（扫到没有新的为止）' : ('最新 ' + n + ' 个起（全是新的会继续翻）');
  }

  /* —— 更新用的画质档位 ——
   *
   * 「一键更新」默认走 **original（原画）**。
   *
   * 为什么默认给原画而不是全局的 `highest`（2026-10-03）：
   *   抖音的码率阶梯里，最高的**转码档**常常还不是作者上传的原片 —— 后台转码
   *   压过一轮，画质有损。「更新」这种补新作品的场景，用户要的通常就是
   *   「跟作者发出来的一样」，所以默认探原片。
   *
   * 代价（后端 `_prefer_original_source` 注释里写明的语义，不是猜的）：
   *   每条作品多一次探测请求（超时上限 10s）。所以这是 `original` 这一档
   *   **显式开启**的行为，而不是塞进 `highest` 里让你看不见也关不掉。
   *
   * ⚠️ 但**用户在设置页显式选过画质时，以用户的选择为准** —— 不静默覆盖。
   *    `creators_update_quality` 留空 = 跟随设置页；填了值 = 只用这一档。
   *    留这个口子是因为「原画更慢更大」有人不想要。
   */
  function updateQuality() {
    try {
      /* ① 设置页显式指定了「更新专用画质」→ 就用它。
       *    先看后端缓存（bridge 模式下那才是真正生效的地方）。 */
      const cached = (window.DYApi && window.DYApi.isBridge()) ? window.__ksWebuiSettings : null;
      if (cached) {
        if (cached.creators_update_quality) return String(cached.creators_update_quality);
        if (cached.creators_update_limit !== undefined) { /* 顺带证明缓存已到位 */ }
        if (cached.video_quality && cached.video_quality !== 'highest') {
          return String(cached.video_quality);
        }
      }
      const s = window.DYStore && window.DYStore.state && window.DYStore.state.settings;
      if (!s) return 'original';
      const only = s.creators_update_quality;
      if (only) return String(only);
      const g = s.video_quality;
      if (g && g !== 'highest') return String(g);
      return 'original';
    } catch (e) {
      return 'original';
    }
  }

  function qualityLabel(q) {
    const opts = (window.DYConfig && window.DYConfig.QUALITY) || [];
    const hit = opts.find((o) => o.id === q);
    return hit ? hit.label : q;
  }

  async function updateAll(view) {
    /* 演示模式没有真实下载能力，退回模拟同步，不假装提交了任务。 */
    if (!window.DYApi.isBridge()) return syncAll(view);

    try { await loadAuthors(); } catch (e) { /* 下面按空处理 */ }
    const ks = src === 'kuaishou';
    const targets = (authorCache[src] || []).filter((c) => c.url);
    if (!targets.length) {
      C.toast(ks
        ? '没有可更新的作者 —— 先在卡片上点「设置主页」填一次主页链接'
        : '没有可更新的作者（缺少 sec_uid，构造不出主页链接）', 'err');
      return;
    }

    const LIMIT = updateLimit();
    const LIMIT_TXT = LIMIT === 0 ? '全部（扫到没有新的为止）' : ('最新 ' + LIMIT + ' 个起步，全是新作品会自动继续直到遇到已下载的');
    /* 快手侧没有「画质档位」这回事（按 manifest 最优码率取），所以只对抖音提画质。 */
    const Q = updateQuality();
    const Q_TXT = qualityLabel(Q);
    C.confirm(
      '一键更新最新作品',
      '将依次检查 ' + targets.length + ' 位' + (ks ? '快手' : '') + '作者的「作品」，每人取' + LIMIT_TXT + '；' +
      (ks ? '台账里已有的自动跳过' : '已下载过的自动跳过') +
      '，只下新的。任务在后台排队执行（同时 2 个），进度看「任务中心」。' +
      (ks ? '\n快手接口有风控，连续大批量抓取会被临时限流，跑完隔一会儿再跑更稳。'
           : '\n画质：' + Q_TXT + '（抖音最高的转码档常不是作者原片，更新默认探原片；' +
             '每条多一次探测请求，会慢一些）。') +
      (LIMIT === 0 ? '' :
        '\n提示：快手作者一次新增超过 ' + LIMIT + ' 个也没关系' +
        '\n窗口里全是新作品时会自动继续往深翻，直到出现已下载的作品为止，不会漏。'),
      async () => {
        const btn = document.getElementById('cr-update-all');
        if (btn) btn.disabled = true;
        C.toast('正在提交 ' + targets.length + ' 个更新任务…');
        let ok = 0;
        let fail = 0;
        let firstErr = '';
        for (const c of targets) {
          try {
            if (ks) {
              // 快手：纯数字 mode = 「只下最新 N 个」，0 = 不限；与抖音侧的 number 同一语义
              await window.DYApi.kuaishouDownload(c.url, String(LIMIT));
            } else {
              await window.DYApi.submitDownload({
                url: c.url,
                platform: 'douyin',
                options: { modes: ['post'], number: LIMIT, increase: true, quality: Q },
              });
            }
            ok++;
          } catch (e) {
            fail++;
            if (!firstErr) firstErr = String((e && e.message) || e);
          }
        }
        /* 任务数变了，侧边栏角标要跟上（提交是逐个 await 的，这里一次性刷新即可） */
        if (window.DYApp && window.DYApp.updateTaskBadge) window.DYApp.updateTaskBadge();
        C.toast(
          fail
            ? '已提交 ' + ok + ' 个，失败 ' + fail + ' 个：' + firstErr
            : '已提交 ' + ok + ' 个更新任务，到「任务中心」查看进度',
          fail ? 'err' : 'ok'
        );
        /* 任务已进队列，按钮恢复可用：想再点一次（比如中途补跑失败的）不会拦着 */
        const again = document.getElementById('cr-update-all');
        if (again) again.disabled = false;
      },
      '开始更新'
    );
  }

  /* —— 只更新一位作者的最新作品 ——
   * 参数与「一键更新」完全一致（作品模式 + 最新 N 个 + 增量），只是范围缩到一个人。
   * 不需要二次确认：按钮本身就写着「更新」，点了就是要更新；真下错了也能在任务中心取消。
   */
  async function updateOne(c) {
    const ks = src === 'kuaishou';
    if (!c.url) {
      C.toast(ks ? '这位作者还没有主页链接，先点「设置主页」填一次'
                 : '该作者缺少 sec_uid，构造不出主页链接', 'err');
      return;
    }
    const LIMIT = updateLimit();          // 与「一键更新」同一个来源（设置页可调，0=不限）
    const Q = updateQuality();            // 与「一键更新」同一个来源（默认原画）
    const sel = '[data-act="update"][data-id="' + c.id + '"]';
    const btn = document.querySelector(sel);
    if (btn) btn.disabled = true;
    try {
      if (ks) {
        await window.DYApi.kuaishouDownload(c.url, String(LIMIT));
      } else {
        await window.DYApi.submitDownload({
          url: c.url,
          platform: 'douyin',
          options: { modes: ['post'], number: LIMIT, increase: true, quality: Q },
        });
      }
      if (window.DYApp && window.DYApp.updateTaskBadge) window.DYApp.updateTaskBadge();
      C.toast('已提交「' + c.nickname + '」的更新任务（取' +
              (LIMIT === 0 ? '全部最新作品' : ('最新 ' + LIMIT + ' 个')) +
              (ks ? '' : ('，画质 ' + qualityLabel(Q))) +
              '），到「任务中心」看进度', 'ok');
    } catch (e) {
      C.toast('提交失败：' + String((e && e.message) || e), 'err');
    } finally {
      const again = document.querySelector(sel);
      if (again) again.disabled = false;
    }
  }

  /* —— 快手作者：填/改主页链接 ——
   * 快手台账里**没有**作者 ID，主页 ID 是靠下载时留下的 `works_list_<ID>.json`
   * 与台账作品 ID 求交集推断出来的（见后端 ks_authors）。推不出来的作者
   * （下载时用的是无 ID 的清单、或只下过单条链接）只能请用户贴一次主页链接。
   */
  function openKsProfile(c) {
    C.open(C.modalFrame('设置作者主页：' + c.nickname,
      '<div class="form-item"><div class="form-item__label">作者主页链接</div>' +
      '<input class="input" id="__ksp_url" spellcheck="false" ' +
      'placeholder="https://www.kuaishou.com/profile/3x…  或分享链接 https://v.kuaishou.com/…" value="' + esc(c.url || '') + '"></div>' +
      '<p class="small faint">' +
      '<b>最准</b>：在电脑浏览器打开作者主页，复制<b>地址栏</b>里的 ' +
      '<code>https://www.kuaishou.com/profile/3x…</code>。<br>' +
      '也可以直接粘手机分享出来的短链（<code>v.kuaishou.com/…</code>）—— ' +
      '短链会<b>原样保存</b>，更新时自动跟随跳转。' +
      '<br>只需要一次，之后「更新」就能用了。链接会存在 ' +
      '<code>data/kuaishou/author_profiles.json</code>，不改动台账与任何已下载的文件。</p>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--primary" id="__ksp_ok">保存</button>'));
    document.getElementById('__ksp_ok').addEventListener('click', async (ev) => {
      const url = document.getElementById('__ksp_url').value.trim();
      if (!url) { C.toast('请粘贴主页链接', 'err'); return; }
      ev.currentTarget.disabled = true;
      ev.currentTarget.textContent = '保存中…';
      try {
        const r = await window.DYApi.ksAuthorProfile(c.nickname, url);
        C.close();
        authorCache[src] = null;
        renderSrcTabs();
        renderList();
        C.toast('已保存「' + c.nickname + '」的主页链接，现在可以点「更新」了'
                + (r && r.resolved === false ? '（短链，更新时会自动跟随跳转）' : ''), 'ok');
      } catch (e) {
        ev.currentTarget.disabled = false;
        ev.currentTarget.textContent = '保存';
        C.toast('保存失败：' + String((e && e.message) || e), 'err');
      }
    });
  }

  /* —— 删除作者信息 ——
   * 两个平台的库都没有独立的「关注」表，作者都是从作品记录聚合出来的：
   *   抖音 → aweme 表按 author_name 聚合  → 删 aweme 记录
   *   快手 → 台账按落盘目录名聚合        → 从台账移记录
   * 所以"删除作者"必然同时影响「作品档案」。这是全站仅有的破坏性操作，
   * 因此把后果逐条写在弹窗里（尤其是"磁盘文件保留"和"会重新下载"两点），
   * 而不是一句"确定删除吗"。
   */
  async function deleteAuthor(c) {
    const ks = src === 'kuaishou';
    try {
      const r = ks
        ? await window.DYApi.ksAuthorDelete(c.nickname)
        : await window.DYApi.bridgeAuthorDelete(c.nickname);
      C.close();
      authorCache[src] = null;                    // 缓存失效，重新拉一次作者清单
      othersCache[src] = null;
      renderSrcTabs();
      renderSyncBar();
      renderList();
      C.toast('已删除「' + c.nickname + '」的 ' + r.deleted + ' 条记录（磁盘文件保留）', 'ok');
      if (!ks && r.authorIds && r.authorIds.length > 1) {
        C.toast('注意：该昵称下有 ' + r.authorIds.length + ' 个不同账号，已一并删除', 'err');
      }
    } catch (e) {
      const btn = document.getElementById('__ad_ok');
      if (btn) { btn.disabled = false; btn.textContent = '确认删除'; }
      C.toast('删除失败：' + String((e && e.message) || e), 'err');
    }
  }

  function openDeleteAuthor(c) {
    const ks = src === 'kuaishou';
    C.open(C.modalFrame('删除作者信息',
      '<div class="flex mb-4">' + C.avatar(c.nickname, c.colorIndex, 'lg') +
      '<div class="grow"><div class="row-item__title">' + esc(c.nickname) + '</div>' +
      '<div class="small muted mono">' + esc(c.douyinId) + '</div>' +
      '<div class="small muted mt-2">作品 <b class="num">' + U.group(c.works) + '</b> 条</div></div></div>' +
      '<div class="danger-note">' +
        '<div class="danger-note__title">' + I.icon('alert', 14) + '删除后会发生什么</div>' +
        '<ul class="small">' +
          '<li>' + (ks ? '快手台账里' : '工程库里') + '该作者的 <b>' + U.group(c.works) + '</b> 条作品记录被删除</li>' +
          '<li>「关注」列表与「作品档案」里都不再出现该作者</li>' +
          '<li><b>磁盘上的视频文件原样保留</b>，不会被删</li>' +
          (ks
            ? '<li>删除前自动备份台账（<code>.tasks/ks-backups/</code>，保留最近 5 份），可人工回滚</li>'
            : '<li>删除前自动整库备份（保留最近 2 份），可人工回滚</li>') +
          '<li>之后再点「更新」，库里已经没有这些记录；<b>磁盘文件还在的仍会按文件名跳过</b>，' +
            '只有文件也被删过的作品才会重新下载</li>' +
        '</ul>' +
      '</div>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--danger" id="__ad_ok">确认删除</button>'));
    document.getElementById('__ad_ok').addEventListener('click', (e) => {
      e.currentTarget.disabled = true;
      e.currentTarget.textContent = '删除中…';
      deleteAuthor(c);
    });
  }

  /* —— 卡片操作 —— */
  function onCardAction(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;

    if (window.DYApi.isBridge()) {
      const c = (authorCache[src] || []).find((x) => x.id === id);
      if (!c) return;
      const ks = src === 'kuaishou';
      if (act === 'download') {
        // 快手卡片上不放「下载」主按钮（全量下载挪进「⋯」并带确认），这里防御性返回
        if (ks) return;
        window.DYRouter.go('douyin');
        setTimeout(() => {
          const input = document.getElementById('wb-url');
          if (input) {
            input.value = c.url || '';
            input.dispatchEvent(new Event('input'));
            input.focus();
          }
        }, 80);
        if (!c.url) C.toast('该作者缺少 sec_uid，无法构造主页链接；可从作品档案复制单条链接', 'err');
      } else if (act === 'update') {
        updateOne(c);
      } else if (act === 'set-profile') {
        openKsProfile(c);
      } else if (act === 'more') {
        openMoreBridge(c);
      }
      return;
    }

    const c = window.DYStore.state.creators.find((x) => x.id === id);
    if (!c) return;
    const act2 = btn.dataset.act;
    if (act2 === 'download') {
      // 跳到抖音工作台并预填作者主页链接
      window.DYRouter.go('douyin');
      setTimeout(() => {
        const input = document.getElementById('wb-url');
        if (input) {
          input.value = c.url;
          input.dispatchEvent(new Event('input'));
          input.focus();
        }
      }, 60);
      window.DYStore.markCreatorSeen(id);
      renderList();
    } else if (act2 === 'more') {
      openMore(c);
    } else if (act2 === 'sync-one') {
      btn.disabled = true;
      window.DYStore.syncCreator(id)
        .then((r) => { C.toast('同步完成，新增 ' + r.added + ' 个作品', 'ok'); })
        .catch((err) => { C.toast(err.message, 'err'); })
        .finally(() => { renderSyncBar(); renderList(); });
    } else if (act2 === 'mark-seen') {
      window.DYStore.markCreatorSeen(id);
      renderList();
    } else if (act2 === 'edit-note') {
      openEdit(c);
    } else if (act2 === 'remove') {
      C.confirm('移除作者', '将从关注列表移除「' + c.nickname + '」，已下载的作品档案不受影响。', () => {
        window.DYStore.removeCreator(id);
        renderSyncBar();
        renderList();
        C.toast('已移除', 'ok');
      }, '移除', true);
    } else if (act2 === 'toggle-auto') {
      window.DYStore.updateCreator(id, { autoSync: !c.autoSync });
      openMore(window.DYStore.state.creators.find((x) => x.id === id));
      renderList();
    }
  }

  /* —— bridge 作者更多操作（抖音 / 快手两套菜单） —— */
  function openMoreBridge(c) {
    const note = localNotes()[c.nickname] || '';
    const ks = src === 'kuaishou';
    const head =
      '<div class="flex mb-4">' + C.avatar(c.nickname, c.colorIndex, 'lg') +
      '<div class="grow"><div class="row-item__title">' + esc(c.nickname) + '</div>' +
      '<div class="small muted mono">' + esc(c.douyinId) + '</div>' +
      '<div class="small faint mt-2 ellipsis">' +
      esc(c.url || (ks ? '（还没有主页链接，点下面的「设置主页链接」填一次）'
                       : '（数据库中无 sec_uid，无法构造主页链接）')) +
      '</div></div></div>';
    const rows = ks
      ? [moreRow('br-update', 'refresh', '更新该作者作品',
           c.url ? '只检查' + updateLimitText() + '，台账里已有的跳过'
                 : '还没有主页链接，先用下面的「设置主页链接」'),
         moreRow('br-profile', 'link', c.url ? '修改主页链接' : '设置主页链接',
           c.url ? '会存在本地映射文件里，不改动台账' : '填一次以后就能更新了'),
         moreRow('br-download-all', 'download', '下载该作者全部作品',
           '全量抓取（可能有几百个），会先让你确认'),
         moreRow('br-unfollow', 'users', '取消关注（档案保留）',
           '不再计入关注列表/统计/一键更新，作品仍在作品档案里'),
         moreRow('br-note', 'bookmark', '编辑本地备注', '当前：' + (note || '无')),
         moreRow('br-delete', 'trash', '删除作者信息', '从快手台账移记录（磁盘文件保留）', true)]
      : [moreRow('br-update', 'refresh', '更新该作者作品',
           c.url ? '只检查' + updateLimitText() + '，已下载的跳过（画质 ' + qualityLabel(updateQuality()) + '）' : '缺少 sec_uid，不可用'),
         moreRow('br-copy', 'link', '复制作者主页链接', c.url || '不可用'),
         moreRow('br-download', 'download', '去下载该作者', '预填到抖音工作台（可选模式/画质）'),
         moreRow('br-unfollow', 'users', '取消关注（档案保留）',
           '不再计入关注列表/统计/一键更新，作品仍在作品档案里'),
         moreRow('br-note', 'bookmark', '编辑本地备注', '当前：' + (note || '无')),
         moreRow('br-delete', 'trash', '删除作者信息', '清掉工程库记录（磁盘文件保留）', true)];
    C.open(C.modalFrame(c.nickname,
      head + '<div class="grid" style="gap:10px">' + rows.join('') + '</div>'));
    document.querySelectorAll('[data-more-act]').forEach((b) => {
      b.addEventListener('click', () => {
        const act = b.dataset.moreAct;
        C.close();
        if (act === 'br-update') {
          updateOne(c);
        } else if (act === 'br-profile') {
          openKsProfile(c);
        } else if (act === 'br-download-all') {
          downloadAllKs(c);
        } else if (act === 'br-copy') {
          if (!c.url) { C.toast('该作者缺少 sec_uid，无法复制链接', 'err'); return; }
          U.copyText(c.url).then((ok) => C.toast(ok ? '链接已复制' : '复制失败', ok ? 'ok' : 'err'));
        } else if (act === 'br-download') {
          const fake = { target: document.createElement('button') };
          fake.target.dataset.act = 'download';
          fake.target.dataset.id = c.id;
          onCardAction(fake);
        } else if (act === 'br-note') {
          openBridgeNote(c);
        } else if (act === 'br-unfollow') {
          unfollowAuthor(c);
        } else if (act === 'br-delete') {
          openDeleteAuthor(c);
        }
      });
    });
  }

  /* —— 快手：下载该作者的全部作品（危险量级，必须二次确认） ——
   * 这个按钮的存在理由：首次把一位作者整份搬下来时，「更新最新 20 个」不够用。
   * 但快手侧全量抓取要滚动翻页，几百个作品会跑很久、也更容易撞风控 ——
   * 所以它不在主按钮位，而且要在这里把代价说清楚。 */
  function downloadAllKs(c) {
    if (!c.url) {
      C.toast('这位作者还没有主页链接，先点「设置主页」填一次', 'err');
      return;
    }
    C.confirm(
      '下载全部作品',
      '将抓取「' + c.nickname + '」主页的全部作品并下载。本地台账已有 ' + U.group(c.works) +
      ' 条记录，已在台账里的会自动跳过，所以重复点不会重下。全量抓取要滚动翻页、逐个下载，耗时较长，' +
      '也可能触发快手风控限流。只想补新作品的话，「更新最新作品」就够了。',
      async () => {
        try {
          await window.DYApi.kuaishouDownload(c.url, 'all');
          if (window.DYApp && window.DYApp.updateTaskBadge) window.DYApp.updateTaskBadge();
          C.toast('已提交「' + c.nickname + '」的全量下载任务，到「任务中心」看进度', 'ok');
        } catch (e) {
          C.toast('提交失败：' + String((e && e.message) || e), 'err');
        }
      },
      '仍要全部下载', true
    );
  }

  /* —— 取消关注（非破坏性） ——
   * 与「删除作者信息」是两件事，措辞上也必须分开说清楚：
   *   取消关注 → 只把作者移出关注列表，作品记录与磁盘文件全留着；
   *   删除作者 → 连 aweme / 台账记录一起删（那才是破坏性操作）。
   * 取消之后作者会出现在列表下方的「未加入关注」清单里，可以一键加回来。 */
  function unfollowAuthor(c) {
    C.confirm('取消关注',
      '「' + c.nickname + '」将从关注列表移除：不再计入上方统计，也不会被「一键更新」带上。' +
      '已下载的 ' + U.group(c.works) + ' 条作品记录和磁盘文件都会保留，' +
      '想恢复就在列表下方点「查看 / 加入关注」。',
      async () => {
        try {
          await window.DYApi.authorFollow(src, c.nickname, false);
          othersCache[src] = null;
          authorCache[src] = null;
          renderSrcTabs();
          renderSyncBar();
          renderList();
          C.toast('已取消关注：「' + c.nickname + '」', 'ok');
        } catch (e) {
          C.toast('取消失败：' + String((e && e.message) || e), 'err');
        }
      },
      '取消关注', true);
  }

  function openBridgeNote(c) {
    C.open(C.modalFrame('本地备注：' + c.nickname,
      '<div class="form-item"><div class="form-item__label">备注</div>' +
      '<input class="input" id="__bnote_input" value="' + esc(localNotes()[c.nickname] || '') + '" maxlength="60" ' +
      'placeholder="例如：优先更新" style="font-family:var(--font-sans)"></div>' +
      '<p class="small faint">备注保存在浏览器本地（dyui.creatorNotes），不影响工程数据库。</p>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--primary" id="__bnote_save">保存</button>'));
    document.getElementById('__bnote_save').addEventListener('click', () => {
      setLocalNote(c.nickname, document.getElementById('__bnote_input').value.trim());
      C.close();
      renderList();
      C.toast('备注已保存', 'ok');
    });
  }

  function openMore(c) {
    C.open(C.modalFrame(c.nickname,
      '<div class="flex mb-4">' + C.avatar(c.nickname, c.colorIndex, 'lg') +
      '<div class="grow"><div class="row-item__title">' + esc(c.nickname) + '</div>' +
      '<div class="small muted mono">' + esc(c.douyinId) + '</div>' +
      '<div class="small faint mt-2 ellipsis">' + esc(c.url) + '</div></div></div>' +
      '<div class="grid" style="gap:10px">' +
      moreRow('sync-one', 'refresh', '立即同步', '拉取该作者的最新作品数') +
      moreRow('mark-seen', 'check', '标记新作品已读', '清除「新作品 +N」角标') +
      moreRow('edit-note', 'bookmark', '编辑备注', '当前：' + (c.note || '无')) +
      moreRow('toggle-auto', 'bolt', c.autoSync ? '关闭自动同步' : '开启自动同步',
        '自动同步的作者会在打开应用时尝试刷新') +
      moreRow('remove', 'trash', '移除作者', '从关注列表移除（档案保留）', true) +
      '</div>'));
    document.querySelectorAll('[data-more-act]').forEach((b) => {
      b.addEventListener('click', () => {
        C.close();
        onCardActionFake(b.dataset.moreAct, c.id);
      });
    });
  }

  function moreRow(act, icon, label, desc, danger) {
    return (
      '<button class="btn btn--ghost" data-more-act="' + act + '" style="justify-content:flex-start;' +
      (danger ? 'color:var(--red);border-color:var(--bb-danger-border)' : '') + '">' +
      I.icon(icon, 15) + '<span class="grow" style="text-align:left">' + esc(label) +
      '<span class="small faint" style="display:block;font-weight:400">' + esc(desc) + '</span></span>' +
      I.icon('chevronRight', 14) + '</button>'
    );
  }

  function onCardActionFake(act, id) {
    const fake = { target: document.createElement('button') };
    fake.target.dataset.act = act;
    fake.target.dataset.id = id;
    onCardAction(fake);
  }

  function openEdit(c) {
    C.open(C.modalFrame('编辑备注',
      '<div class="form-item"><div class="form-item__label">备注</div>' +
      '<input class="input" id="__note_input" value="' + esc(c.note || '') + '" maxlength="60" ' +
      'placeholder="例如：城市选题" style="font-family:var(--font-sans)"></div>' +
      '<div class="form-item"><div class="form-item__label">默认下载模式</div><div class="chips" id="__note_modes">' +
      window.DYConfig.MODES.slice(0, 4).map((m) =>
        '<button class="chip' + (c.modes.includes(m.id) ? ' is-on' : '') + '" data-mode="' + m.id + '">' +
        esc(m.label) + '</button>').join('') +
      '</div></div>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--primary" id="__note_save">保存</button>'));
    const modes = c.modes.slice();
    document.querySelectorAll('#__note_modes .chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        const id = chip.dataset.mode;
        const i = modes.indexOf(id);
        if (i >= 0 && modes.length > 1) modes.splice(i, 1);
        else if (i < 0) modes.push(id);
        chip.classList.toggle('is-on', modes.includes(id));
      });
    });
    document.getElementById('__note_save').addEventListener('click', () => {
      const v = document.getElementById('__note_input').value.trim();
      window.DYStore.updateCreator(c.id, { note: v, modes });
      C.close();
      renderList();
      C.toast('已保存', 'ok');
    });
  }

  function openAdd() {
    C.open(C.modalFrame('添加作者',
      '<div class="form-item"><div class="form-item__label">作者主页链接</div>' +
      '<input class="input" id="__add_url" placeholder="https://www.douyin.com/user/…" spellcheck="false"></div>' +
      '<div class="form-item"><div class="form-item__label">昵称</div>' +
      '<input class="input" id="__add_name" placeholder="可留空，将自动识别" style="font-family:var(--font-sans)"></div>' +
      '<div class="form-item"><div class="form-item__label">备注</div>' +
      '<input class="input" id="__add_note" placeholder="选填" style="font-family:var(--font-sans)"></div>' +
      '<p class="small faint">演示环境不会真正请求抖音；接入真实后端后由后端解析主页信息。</p>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--primary" id="__add_ok">添加</button>'));
    document.getElementById('__add_ok').addEventListener('click', () => {
      const url = document.getElementById('__add_url').value.trim();
      if (!/^https?:\/\//.test(url)) {
        C.toast('请输入有效的 http(s) 链接', 'err');
        return;
      }
      const name = document.getElementById('__add_name').value.trim();
      const note = document.getElementById('__add_note').value.trim();
      const c = window.DYStore.addCreator({
        url, nickname: name || '新作者', note,
        douyinId: '@' + (name || 'new_user'),
      });
      C.close();
      renderSyncBar();
      renderList();
      C.toast('已添加：' + c.nickname, 'ok');
    });
  }

  window.DYRouter.register('creators', render);
})();
