/* ============================================================
 * pages/favorites.js — 收藏与喜欢
 * ------------------------------------------------------------
 * 五个 tab：
 *   我的作品 / 我的喜欢 / 我的收藏 / 我的关注  ← 当前登录抖音账号的真实内容
 *   我的标记                                  ← 本地「还想要」清单（与登录无关）
 *
 * 前四个 tab 的数据由桥接服务的 account_service 用**登录态页面通道**拉取后
 * 落盘缓存（纯 HTTP 会被抖音 Argus 风控 403）。前端只读缓存，点「同步」才
 * 触发后端刷新 —— 喜欢/关注动辄几百条、要翻几十页，自动轮询等于拿风控额度
 * 换新鲜度。
 *
 * 「我的标记」是原来那套本地收藏夹，数据在 .data/favorites.json，
 * 跟抖音登录态没有任何关系，单独保留一个 tab，功能一字未改。
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  // 两个平台的 tab 不一样，而且**不是同一套能力**：
  //   抖音：作品 / 喜欢 / 收藏 / 关注（名单）  ← 四个都实测可用
  //   快手：作品 / 关注动态                     ← 网页版没有喜欢、收藏入口，
  //                                               「关注」也只给关注流、不给名单
  // 所以这里按平台列 tab，而不是一套 tab 加一堆 if。
  const TABS_BY_PLATFORM = {
    douyin: [
      { id: 'post',      label: '我的作品', icon: 'video' },
      { id: 'like',      label: '我的喜欢', icon: 'heart' },
      { id: 'collect',   label: '我的收藏', icon: 'bookmark' },
      { id: 'following', label: '我的关注', icon: 'users' },
    ],
    kuaishou: [
      { id: 'post',   label: '我的作品', icon: 'video' },
      { id: 'follow', label: '关注动态', icon: 'users' },
    ],
  };
  const PLATFORMS = [
    { id: 'douyin', label: '抖音', icon: 'tiktok' },
    { id: 'kuaishou', label: '快手', icon: 'bolt' },
  ];
  const MINE_TAB = { id: 'mine', label: '我的标记', icon: 'star' };

  let platform = 'douyin';
  let tab = 'post';
  const ACC = { status: null, list: {}, loading: false, err: '', syncTimer: null, loginTimer: null };
  const fav = { folders: [], items: [], cur: 'unfiled', filter: 'all', loading: false };

  function currentTabs() {
    return (TABS_BY_PLATFORM[platform] || TABS_BY_PLATFORM.douyin).concat([MINE_TAB]);
  }
  function tabMeta(id) {
    return currentTabs().filter(function (t) { return t.id === id; })[0] || {};
  }
  function isAccountTab(id) {
    return (TABS_BY_PLATFORM[platform] || []).some(function (t) { return t.id === id; });
  }

  /* —— 小工具 —— */
  function fmtNum(n) {
    n = Number(n) || 0;
    if (n >= 100000000) return (n / 100000000).toFixed(1).replace(/\.0$/, '') + ' 亿';
    if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + ' 万';
    return String(n);
  }
  function fmtDur(sec) {
    sec = Math.round(Number(sec) || 0);
    if (!sec) return '';
    const m = Math.floor(sec / 60);
    return (m ? m + ':' : '0:') + String(sec % 60).padStart(2, '0');
  }

  /* ============================================================
   * 渲染入口
   * ============================================================ */
  function render(view) {
    view.className = 'content';
    delete view.dataset.platform;

    if (!window.DYApi.isBridge()) {
      // 双击 index.html 的演示模式：账号数据必须有桥接服务（要读本机 Cookie、
      // 要开浏览器页面通道），这里如实说明，不拿假数据糊弄。
      view.innerHTML =
        '<div class="page-head page-head--compact"><div class="page-head__row">' +
          '<div><div class="crumb"><span class="crumb__dot"></span>账号内容 · 收藏与喜欢</div>' +
          '<h1 class="page-title">收藏与喜欢</h1></div>' +
        '</div></div>' +
        C.empty('favorites', 'bookmark', {
          title: '演示模式读不到账号内容',
          desc: '当前是直接打开的静态页面，账号的作品/喜欢/收藏/关注需要桥接服务（用工具箱启动）才能读取。',
        });
      return;
    }

    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div class="page-head__row">' +
          '<div><div class="crumb"><span class="crumb__dot"></span>账号内容 · 收藏与喜欢</div>' +
          '<h1 class="page-title">收藏与喜欢</h1></div>' +
          '<div class="flex" style="gap:8px">' +
            '<div class="chips" id="fv-platform">' +
              PLATFORMS.map(function (p) {
                return '<button class="chip' + (p.id === platform ? ' is-on' : '') +
                  '" data-platform="' + p.id + '">' + esc(p.label) + '</button>';
              }).join('') +
            '</div>' +
            '<button class="btn btn--ghost btn--sm" id="fv-login">' + I.icon('key', 14) + '重新登录</button>' +
            '<button class="btn btn--primary btn--sm" id="fv-sync">' + I.icon('refresh', 14) + '同步</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div id="fv-acct"></div>' +
      '<div class="mb-4" id="fv-tabs"></div>' +
      '<div id="fv-body"></div>';

    view.querySelector('#fv-login').addEventListener('click', onLogin);
    view.querySelector('#fv-sync').addEventListener('click', onSync);
    view.querySelector('#fv-platform').addEventListener('click', function (e) {
      const b = e.target.closest('[data-platform]');
      if (!b || b.dataset.platform === platform) return;
      platform = b.dataset.platform;
      tab = 'post';
      ACC.status = null;
      ACC.list = {};
      view.querySelectorAll('#fv-platform .chip').forEach(function (x) {
        x.classList.toggle('is-on', x.dataset.platform === platform);
      });
      refreshStatus();
    });
    view.querySelector('#fv-tabs').addEventListener('click', function (e) {
      const t = e.target.closest('[data-tab]');
      if (!t) return;
      tab = t.dataset.tab;
      renderTabs();
      loadTab(tab);
    });
    view.querySelector('#fv-body').addEventListener('click', onBodyAction);

    refreshStatus();
  }

  /* ============================================================
   * 账号卡 + tab 栏
   * ============================================================ */
  function refreshStatus() {
    // 「我的标记」的条数也要进 tab 栏，否则没点进那个 tab 前一直显示 0（假数字）。
    window.DYApi.favItems('all').then(function (d) {
      fav.items = d.items || [];
      renderTabs();
    }).catch(function () { /* 标记清单读不出来不影响账号内容 */ });

    window.DYApi.accountStatus(platform).then(function (st) {
      ACC.status = st;
      renderAcct();
      renderTabs();
      loadTab(tab);
      if (st.sync && st.sync.running) pollSync();
      // 页面刷新后若登录还在进行中，接着轮询
      if (st.login && st.login.state === 'waiting') pollLogin();
    }).catch(function (e) {
      ACC.err = e.message;
      renderAcct();
      renderBody();
    });
  }

  function renderAcct() {
    const box = document.getElementById('fv-acct');
    if (!box) return;
    const st = ACC.status;
    if (!st) { box.innerHTML = ''; return; }
    const p = st.profile || {};
    const logged = !!p.sec_uid;
    const sync = st.sync || {};
    const login = st.login || {};

    // 为什么读不到账号：登录失败、还是登录成功但同步失败？
    // 后端会把原因放在 login.message / login.sync_error / sync.error 里，
    // 前端只写「还没读取到登录账号」等于把唯一的线索藏起来，用户只能反复点。
    const why = login.state === 'failed'
      ? (login.message || '')
      : (sync.error || login.sync_error || '');

    if (!logged) {
      const tip = platform === 'kuaishou'
        ? '快手的主会话 Cookie 不跨浏览器保留，所以「扫码 → 同步」是一次性做完的：扫码成功后会自动同步一次。'
        : '点「重新登录」扫码，登录成功后会自动同步一次。';
      box.innerHTML =
        '<div class="card acct-card acct-card--warn">' +
          '<div class="flex" style="gap:12px;align-items:center">' +
            '<div class="acct-card__avatar">' + I.icon(why ? 'alert' : 'key', 20) + '</div>' +
            '<div class="grow">' +
              '<div class="acct-card__name">' +
                (login.state === 'failed' ? '扫码登录未完成' : (why ? '读取账号失败' : '还没读取到登录账号')) +
              '</div>' +
              '<div class="small muted">' + esc(why || tip) + '</div>' +
            '</div>' +
            '<button class="btn btn--primary btn--sm" id="fv-acct-login">扫码登录</button>' +
          '</div>' +
        '</div>';
      const b = box.querySelector('#fv-acct-login');
      if (b) b.addEventListener('click', onLogin);
      return;
    }

    const counts = st.counts || {};
    // 登录进行中时把「已扫码，请在手机上确认」这类实时提示摆在账号卡上 ——
    // 否则用户只能靠猜，扫码后不知道该在手机上点确认。
    // 同步失败也要摆出来：账号读到了、内容没读到，原因必须看得见。
    const progress = login.state === 'waiting'
      ? '<div class="acct-card__sync">' + I.icon('loader', 13) + ' ' +
        esc(login.message || '等待扫码…') + '</div>'
      : (sync.running
          ? '<div class="acct-card__sync">' + I.icon('loader', 13) + ' ' +
            esc(sync.message || '同步中…') + '</div>'
          : (sync.error
              ? '<div class="acct-card__sync acct-card__sync--err">' + I.icon('alert', 13) + ' ' +
                esc(sync.error) + '</div>'
              : (st.updated_at
                  ? '<div class="acct-card__sync muted">上次同步 ' + esc(U.fromNow(st.updated_at)) + '</div>'
                  : '<div class="acct-card__sync muted">还没同步过，点右上角「同步」拉取</div>')));

    // 快手的统计项和抖音不同：没有「喜欢/收藏」可展示，硬摆上去等于骗人。
    const stats = platform === 'kuaishou'
      ? '<span><b class="num">' + fmtNum(p.aweme_count) + '</b> 作品</span>' +
        '<span><b class="num">' + fmtNum(p.following_count) + '</b> 关注</span>' +
        '<span><b class="num">' + fmtNum(p.follower_count) + '</b> 粉丝</span>'
      : '<span><b class="num">' + fmtNum(p.aweme_count) + '</b> 作品</span>' +
        '<span><b class="num">' + fmtNum(p.favoriting_count) + '</b> 喜欢</span>' +
        '<span><b class="num">' + fmtNum(counts.collect || 0) + '</b> 收藏</span>' +
        '<span><b class="num">' + fmtNum(p.following_count) + '</b> 关注</span>' +
        '<span><b class="num">' + fmtNum(p.follower_count) + '</b> 粉丝</span>';

    box.innerHTML =
      '<div class="card acct-card">' +
        '<div class="acct-card__main">' +
          '<div class="acct-card__avatar">' +
            (p.avatar ? '<img src="' + esc(p.avatar) + '" alt="" loading="lazy">' : I.icon('users', 20)) +
          '</div>' +
          '<div class="grow" style="min-width:0">' +
            '<div class="acct-card__name">' + esc(p.nickname || '账号') +
              (p.unique_id ? '<span class="mono small muted"> @' + esc(p.unique_id) + '</span>' : '') +
            '</div>' +
            '<div class="acct-card__stats">' + stats + '</div>' +
          '</div>' +
          progress +
        '</div>' +
      '</div>';
  }

  function renderTabs() {
    const box = document.getElementById('fv-tabs');
    if (!box) return;
    const st = ACC.status || {};
    const p = st.profile || {};
    const counts = st.counts || {};
    // tab 上的数字优先用「实际同步到的条数」：账号资料里的 favoriting_count（660）
    // 是平台自己的统计，接口实际能拉到的往往更少（私密/已删/审核中不会返回）。
    // 两个数字并排出现会让人以为丢数据，所以 tab 用可下载条数，账号卡保留官方数字。
    const total = {
      post: counts.post || p.aweme_count || 0,
      like: counts.like || p.favoriting_count || 0,
      collect: counts.collect || 0,
      following: counts.following || p.following_count || 0,
      follow: counts.follow || 0,
      mine: fav.items.length || 0,
    };
    box.innerHTML = C.tabs(currentTabs().map(function (t) {
      return { id: t.id, label: t.label, count: total[t.id] || 0 };
    }), tab);
  }

  /* ============================================================
   * tab 内容
   * ============================================================ */
  function loadTab(id) {
    if (id === 'mine') { renderLocal(); return; }
    renderBody();                       // 先出骨架
    if (ACC.list[id] && ACC.list[id].synced) { renderBody(); return; }
    ACC.loading = true;
    renderBody();
    window.DYApi.accountList(id, 0, platform).then(function (d) {
      ACC.list[id] = d;
      ACC.loading = false;
      ACC.err = '';
      if (tab === id) renderBody();
      renderTabs();
    }).catch(function (e) {
      ACC.loading = false;
      ACC.err = e.message;
      if (tab === id) renderBody();
    });
  }

  function renderBody() {
    if (tab === 'mine') return;         // 本地 tab 由 renderLocal 自己接管
    const box = document.getElementById('fv-body');
    if (!box) return;

    if (ACC.loading) { box.innerHTML = C.skeletonRows(4); return; }
    if (ACC.err) {
      box.innerHTML = '<div class="error-box"><h3>读取失败</h3><p class="muted">' +
        esc(ACC.err) + '</p></div>';
      return;
    }
    const meta = tabMeta(tab);
    const block = ACC.list[tab];
    const platName = platform === 'kuaishou' ? '快手' : '抖音';
    if (!block || !block.synced) {
      // 同步失败过就直说失败原因，别拿「还没同步过」糊过去 ——
      // 用户刚扫完码，看到"还没同步过"只会以为按钮没生效。
      const syncErr = ((ACC.status || {}).sync || {}).error;
      if (syncErr) {
        box.innerHTML = '<div class="error-box"><h3>同步失败</h3><p class="muted">' +
          esc(syncErr) + '</p><p class="small muted">已登录的账号信息不会丢，重新点右上角「同步」或' +
          '「重新登录」再试一次。</p></div>';
        return;
      }
      box.innerHTML = C.empty('favorites', meta.icon || 'bookmark', {
        title: '还没同步过「' + (meta.label || '') + '」',
        desc: platform === 'kuaishou'
          ? '点右上角「同步」从快手账号拉取；如果提示登录失效，点「重新登录」扫码，扫完会自动同步一次。'
          : '点右上角「同步」从' + platName + '账号拉取。作品/喜欢/收藏/关注一次同步完，之后打开都是秒开。',
      });
      return;
    }
    const items = block.items || [];
    if (!items.length) {
      box.innerHTML = C.empty('favorites', meta.icon || 'bookmark', {
        title: '这个列表是空的',
        desc: platform === 'kuaishou'
          ? '快手的「关注动态」只给最近几页，没内容通常是关注的人最近没发作品。'
          : '账号里确实没有内容，或者同步时接口只返回了空页（限速）。可以再点一次「同步」试试。',
      });
      return;
    }

    if (tab === 'following') {
      box.innerHTML =
        '<div class="acct-list-head"><span class="small muted">共 ' + items.length + ' 人</span></div>' +
        '<div class="acct-users">' + items.map(userRow).join('') + '</div>';
      return;
    }

    const pending = items.filter(function (x) { return !x.local; }).length;
    box.innerHTML =
      '<div class="acct-list-head">' +
        '<span class="small muted">共 ' + items.length + ' 条' +
          '，其中 <b class="num">' + pending + '</b> 条未落地</span>' +
        (pending ? '<button class="btn btn--ghost btn--sm" data-act="dl-all">' +
          I.icon('download', 13) + '下载未落地的 ' + pending + ' 条</button>' : '') +
      '</div>' +
      '<div class="acct-grid">' + items.map(workCard).join('') + '</div>';
  }

  function workCard(it) {
    const cover = it.cover
      ? '<img class="acct-item__img" src="' + esc(it.cover) + '" alt="" loading="lazy">'
      : '<div class="acct-item__ph">' + I.icon(it.type === 'image' ? 'image' : 'video', 22) + '</div>';
    return (
      '<div class="acct-item" data-id="' + esc(it.id) + '">' +
        '<div class="acct-item__cover">' + cover +
          (it.duration ? '<span class="acct-item__dur">' + fmtDur(it.duration) + '</span>' : '') +
          (it.local ? '<span class="acct-item__done">' + I.icon('check', 11) + '已下载</span>' : '') +
        '</div>' +
        '<div class="acct-item__body">' +
          '<div class="acct-item__title" title="' + esc(it.desc) + '">' +
            esc(it.desc || '（无标题）') + '</div>' +
          '<div class="acct-item__meta">' +
            '<span>' + I.icon('heart', 11) + ' ' + fmtNum(it.digg) + '</span>' +
            '<span>' + esc(it.create_time ? U.fromNow(it.create_time * 1000) : '-') + '</span>' +
          '</div>' +
          '<div class="acct-item__acts">' +
            '<button class="btn btn--primary btn--sm grow" data-act="dl" data-url="' + esc(it.share_url) +
              '" data-id="' + esc(it.id) + '">' + I.icon('download', 12) + '下载</button>' +
            '<button class="btn btn--ghost btn--sm btn--icon" data-act="open" data-url="' +
              esc(it.share_url) + '" title="在浏览器打开">' + I.icon('external', 13) + '</button>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  function userRow(u) {
    return (
      '<div class="acct-user">' +
        '<div class="acct-user__avatar">' +
          (u.avatar ? '<img src="' + esc(u.avatar) + '" alt="" loading="lazy">' : I.icon('users', 16)) +
        '</div>' +
        '<div class="grow" style="min-width:0">' +
          '<div class="acct-user__name">' + esc(u.nickname || '（无昵称）') + '</div>' +
          '<div class="acct-user__meta">' +
            '<span class="mono">@' + esc(u.unique_id || '-') + '</span>' +
            '<span>粉丝 ' + fmtNum(u.follower_count) + '</span>' +
            '<span>作品 ' + fmtNum(u.aweme_count) + '</span>' +
          '</div>' +
          (u.signature ? '<div class="acct-user__sign small muted">' +
            esc(U.truncate(u.signature, 90)) + '</div>' : '') +
        '</div>' +
        '<div class="flex" style="gap:8px">' +
          '<button class="btn btn--primary btn--sm" data-act="dluser" data-url="' + esc(u.home_url) +
            '">' + I.icon('download', 12) + '下载TA的作品</button>' +
          '<button class="btn btn--ghost btn--sm btn--icon" data-act="open" data-url="' +
            esc(u.home_url) + '" title="打开主页">' + I.icon('external', 13) + '</button>' +
        '</div>' +
      '</div>'
    );
  }

  /* ============================================================
   * 动作
   * ============================================================ */
  function onBodyAction(e) {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;

    if (act === 'open') {
      if (el.dataset.url) window.open(el.dataset.url, '_blank');
      return;
    }
    if (act === 'dl') {
      submitOne(el.dataset.url, el);
      return;
    }
    if (act === 'dluser') {
      // 下载某个作者的全部作品 = 一次主页批量任务，交给工作台（那里有模式/数量选项）
      window.DYPrefill = { url: el.dataset.url };
      window.DYRouter.go('douyin');
      return;
    }
    if (act === 'dl-all') {
      const block = ACC.list[tab] || { items: [] };
      const todo = block.items.filter(function (x) { return !x.local; });
      if (!todo.length) return;
      C.confirm('下载未落地的 ' + todo.length + ' 条',
        '会为这 ' + todo.length + ' 条作品各建一个下载任务，排队执行。已下载过的会自动跳过。',
        function () { submitMany(todo); }, '开始下载');
      return;
    }
  }

  function submitOne(url, btn) {
    if (!url) return;
    if (btn) { btn.disabled = true; btn.innerHTML = I.icon('loader', 12) + '提交中'; }
    // 两个平台走的是**不同的下载器**：抖音是 run.py，快手是 kuaishou_headless.py。
    // 把快手链接丢给抖音那套只会解析失败，所以这里必须分叉。
    const job = platform === 'kuaishou'
      ? window.DYApi.kuaishouDownload(url, '1')
      : window.DYApi.bridgeSubmit(url, {});
    job.then(function () {
      C.toast('已加入下载队列', 'ok');
      if (btn) { btn.innerHTML = I.icon('check', 12) + '已加入'; }
    }).catch(function (e) {
      C.toast(e.needLogin ? '快手未登录：请先点右上角「重新登录」扫码' : e.message, 'err');
      if (btn) { btn.disabled = false; btn.innerHTML = I.icon('download', 12) + '下载'; }
    });
  }

  function submitMany(items) {
    let ok = 0, fail = 0;
    const queue = items.slice();
    function next() {
      const it = queue.shift();
      if (!it) {
        C.toast('已提交 ' + ok + ' 条' + (fail ? '，失败 ' + fail + ' 条' : ''), fail ? 'err' : 'ok');
        return;
      }
      const job = platform === 'kuaishou'
        ? window.DYApi.kuaishouDownload(it.share_url, '1')
        : window.DYApi.bridgeSubmit(it.share_url, {});
      job.then(function () {
        ok += 1;
      }).catch(function () {
        fail += 1;
      }).then(function () {
        setTimeout(next, 120);          // 轻微间隔，别把队列接口打满
      });
    }
    next();
  }

  /* ============================================================
   * 同步（后端后台跑，前端轮询进度）
   * ============================================================ */
  function onSync() {
    const btn = document.getElementById('fv-sync');
    if (btn) { btn.disabled = true; btn.innerHTML = I.icon('loader', 14) + '同步中…'; }
    window.DYApi.accountSync(null, platform).then(function (d) {
      // 登录窗口还开着时后端不排队（那个同步注定失败，会话会随窗口一起消失）
      if (d && d.started === false) {
        C.toast(d.message || '登录进行中，扫码成功后会自动同步', 'ok');
        if (btn) { btn.disabled = false; btn.innerHTML = I.icon('refresh', 14) + '同步'; }
        if (!ACC.loginTimer) pollLogin();
        return;
      }
      pollSync();
    }).catch(function (e) {
      C.toast(e.message, 'err');
      if (btn) { btn.disabled = false; btn.innerHTML = I.icon('refresh', 14) + '同步'; }
    });
  }

  function pollSync() {
    if (ACC.syncTimer) clearTimeout(ACC.syncTimer);
    window.DYApi.accountStatus(platform).then(function (st) {
      ACC.status = st;
      renderAcct();
      const sync = st.sync || {};
      if (sync.running) {
        ACC.syncTimer = setTimeout(pollSync, 2000);
        return;
      }
      const btn = document.getElementById('fv-sync');
      if (btn) { btn.disabled = false; btn.innerHTML = I.icon('refresh', 14) + '同步'; }
      if (sync.error) {
        C.toast('同步失败：' + sync.error, 'err');
      } else {
        C.toast('同步完成', 'ok');
        ACC.list = {};                  // 缓存已更新，丢掉前端旧数据
        renderTabs();
        loadTab(tab);
      }
    }).catch(function () {
      ACC.syncTimer = setTimeout(pollSync, 3000);
    });
  }

  /* ============================================================
   * 扫码登录
   * ============================================================ */
  function onLogin() {
    window.DYApi.accountLogin(platform).then(function (st) {
      C.toast(st.message || '已打开登录窗口，请扫码', 'ok');
      pollLogin();
    }).catch(function (e) { C.toast(e.message, 'err'); });
  }

  function pollLogin() {
    if (ACC.loginTimer) clearTimeout(ACC.loginTimer);
    window.DYApi.accountLoginStatus(platform).then(function (st) {
      if (st.state === 'waiting') {
        // 把「已扫码，请在手机上确认」这类提示实时刷到账号卡上
        if (ACC.status) { ACC.status.login = st; renderAcct(); }
        ACC.loginTimer = setTimeout(pollLogin, 2000);
        return;
      }
      if (st.state === 'ok') {
        C.toast(st.message || '登录成功', 'ok');
        ACC.list = {};
        refreshStatus();
        // 快手是「登录 + 同步」在同一个浏览器实例里一次做完的（会话 Cookie 不跨实例），
        // 所以这里不能再补一次同步 —— 那时会话已经没了，只会立刻报登录失效。
        if (platform !== 'kuaishou') onSync();
      } else if (st.state === 'failed') {
        C.toast(st.message || '登录失败', 'err');
      }
    }).catch(function () {
      ACC.loginTimer = setTimeout(pollLogin, 3000);
    });
  }

  /* ============================================================
   * 「我的标记」—— 本地收藏夹（原有功能，未改动）
   * ------------------------------------------------------------
   * 与账号收藏无关：这是用户自己在本地标记「还想要、还没落地」的清单，
   * 由桥接服务落 .data/favorites.json，换浏览器也不丢。
   * ============================================================ */
  function renderLocal() {
    const box = document.getElementById('fv-body');
    if (!box) return;
    box.innerHTML =
      '<div class="flex flex-wrap mb-4" style="gap:10px">' +
        '<div class="chips" id="fav-filter">' +
          '<button class="chip is-on" data-filter="all">全部</button>' +
          '<button class="chip" data-filter="todo">未下载</button>' +
          '<button class="chip" data-filter="done">已下载</button>' +
        '</div>' +
        '<span class="small muted" id="fav-count" style="margin-left:auto"></span>' +
        '<button class="btn btn--ghost btn--sm" id="fav-newfolder">' + I.icon('plus', 14) + '新建收藏夹</button>' +
        '<button class="btn btn--primary btn--sm" id="fav-add">' + I.icon('plus', 14) + '添加作品</button>' +
      '</div>' +
      '<div class="fav-layout">' +
        '<aside class="fav-side">' +
          '<div class="fav-side__head">收藏夹</div>' +
          '<div id="fav-folders" class="fav-folders"></div>' +
        '</aside>' +
        '<section class="fav-main"><div id="fav-items"></div></section>' +
      '</div>';

    bindLocal(box);
    reloadFav();
  }

  function bindLocal(view) {
    view.querySelector('#fav-newfolder').addEventListener('click', newFolder);
    view.querySelector('#fav-add').addEventListener('click', addItem);
    view.querySelector('#fav-folders').addEventListener('click', onFolderAction);
    view.querySelector('#fav-filter').addEventListener('click', function (e) {
      const c = e.target.closest('[data-filter]');
      if (!c) return;
      fav.filter = c.dataset.filter;
      view.querySelectorAll('#fav-filter .chip').forEach(function (x) {
        x.classList.toggle('is-on', x === c);
      });
      renderItems();
    });
    view.querySelector('#fav-items').addEventListener('click', onItemAction);
  }

  function reloadFav() {
    window.Promise.all([window.DYApi.favFolders(), window.DYApi.favItems('all')]).then(function (r) {
      fav.folders = r[0].folders || [];
      fav.items = r[1].items || [];
      if (!fav.folders.some(function (f) { return f.id === fav.cur; })) fav.cur = 'unfiled';
      renderFolders();
      renderItems();
      renderTabs();
    }).catch(function (e) {
      const box = document.getElementById('fav-items');
      if (box) box.innerHTML = '<div class="error-box"><h3>读取收藏夹失败</h3><p class="muted">' +
        esc(e.message) + '</p></div>';
    });
  }

  function renderFolders() {
    const box = document.getElementById('fav-folders');
    if (!box) return;
    box.innerHTML = '<button class="fav-folder' + (fav.cur === 'all' ? ' is-on' : '') +
      '" data-fid="all"><span class="fav-folder__name">' + I.icon('layers', 15) + '全部</span>' +
      '<span class="num">' + fav.items.length + '</span></button>' +
      fav.folders.map(function (f) {
        return '<button class="fav-folder' + (fav.cur === f.id ? ' is-on' : '') +
          '" data-fid="' + esc(f.id) + '">' +
          '<span class="fav-folder__name">' + I.icon('folder', 15) + esc(f.name) + '</span>' +
          '<span class="num">' + (f.count || 0) + '</span>' +
          (f.system ? '' : '<span class="fav-folder__tools" data-tools="' + esc(f.id) + '">' +
            '<span data-fact="rename" data-fid="' + esc(f.id) + '" data-fname="' + esc(f.name) +
            '" title="重命名">' + I.icon('edit', 13) + '</span>' +
            '<span data-fact="del" data-fid="' + esc(f.id) + '" title="删除">' +
            I.icon('trash', 13) + '</span></span>') +
          '</button>';
      }).join('');
  }

  function renderItems() {
    const box = document.getElementById('fav-items');
    if (!box) return;
    const count = document.getElementById('fav-count');
    let list = fav.items;
    if (fav.cur !== 'all') {
      list = list.filter(function (x) { return (x.folder || 'unfiled') === fav.cur; });
    }
    if (fav.filter === 'todo') list = list.filter(function (x) { return !x.done; });
    if (fav.filter === 'done') list = list.filter(function (x) { return x.done; });
    if (count) count.textContent = list.length + ' 条';
    if (!list.length) {
      box.innerHTML = C.empty('favorites', 'bookmark', fav.items.length
        ? { title: '当前筛选下没有条目', desc: '点上面的「全部 / 未下载 / 已下载」换个筛选条件看看。' }
        : { title: '还没有标记任何作品', desc:
            '这里是你自己标记的「还想要」清单，不需要登录抖音。' +
            '点右上角「添加作品」把链接存进来，或去「任务记录 → 作品档案」点卡片上的 ♡。' });
      return;
    }
    box.innerHTML = '<div class="fav-list">' + list.map(itemRow).join('') + '</div>';
  }

  function itemRow(it) {
    const folder = fav.folders.filter(function (f) { return f.id === (it.folder || 'unfiled'); })[0] ||
      { name: '未分类' };
    const done = !!it.done;
    return (
      '<div class="fav-item' + (done ? ' is-done' : '') + '">' +
        '<div class="fav-item__main">' +
          '<div class="flex" style="gap:10px">' +
            '<button class="fav-item__check" data-act="toggle" data-id="' + esc(it.id) +
              '" title="' + (done ? '标记为未下载' : '标记为已下载') + '">' +
              (done ? I.icon('circleCheck', 18) : I.icon('circleDot', 18)) + '</button>' +
            '<div class="grow" style="min-width:0">' +
              '<div class="fav-item__title' + (done ? ' fav-item__title--done' : '') + '">' +
                esc(it.title) + '</div>' +
              '<div class="fav-item__meta"><span class="mono">' + esc(U.truncate(it.url, 64)) + '</span>' +
              '<span>' + esc(folder.name) + '</span>' +
              '<span>' + esc(U.fromNow(it.added_at)) + '</span></div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="fav-item__actions">' +
          '<select class="input fav-item__move" data-act="move" data-id="' + esc(it.id) +
            '" title="移动到收藏夹">' +
            fav.folders.map(function (f) {
              return '<option value="' + esc(f.id) + '"' +
                ((it.folder || 'unfiled') === f.id ? ' selected' : '') + '>' + esc(f.name) + '</option>';
            }).join('') + '</select>' +
          '<button class="btn btn--ghost btn--sm btn--icon" data-act="rename" data-id="' + esc(it.id) +
            '" data-title="' + esc(it.title) + '" title="重命名">' + I.icon('edit', 14) + '</button>' +
          '<button class="btn btn--primary btn--sm" data-act="download" data-id="' + esc(it.id) +
            '" data-url="' + esc(it.url) + '">' + I.icon('download', 13) + '下载</button>' +
          '<button class="btn btn--ghost btn--sm btn--icon" data-act="remove" data-id="' + esc(it.id) +
            '" title="移除">' + I.icon('trash', 14) + '</button>' +
        '</div>' +
      '</div>'
    );
  }

  /* —— 文件夹 —— */
  function newFolder() {
    const close = C.open(C.modalFrame('新建收藏夹',
      '<div class="form-item"><div class="form-item__label">名称</div>' +
      '<input class="input" id="fav-folder-name" placeholder="例如：待下载的教程" autocomplete="off"></div>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--primary" id="fav-folder-ok">创建</button>'));
    const ok = function () {
      const name = document.getElementById('fav-folder-name').value.trim();
      if (!name) { C.toast('名称不能为空', 'err'); return; }
      window.DYApi.favFolderAct('create', { name: name })
        .then(function (d) { close(); fav.cur = d.id; reloadFav(); C.toast('已创建「' + name + '」', 'ok'); })
        .catch(function (e) { C.toast(e.message, 'err'); });
    };
    document.getElementById('fav-folder-ok').addEventListener('click', ok);
    document.getElementById('fav-folder-name').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') ok();
    });
    setTimeout(function () { document.getElementById('fav-folder-name').focus(); }, 0);
  }

  function onFolderAction(e) {
    const t = e.target.closest('[data-fact]');
    if (t) {
      const id = t.dataset.fid;
      const name = t.dataset.fname;
      if (t.dataset.fact === 'rename') renameFolder(id, name);
      else if (t.dataset.fact === 'del') deleteFolder(id, name);
      return;
    }
    const f = e.target.closest('[data-fid]');
    if (f) { fav.cur = f.dataset.fid; renderFolders(); renderItems(); }
  }

  function renameFolder(id, oldName) {
    const close = C.open(C.modalFrame('重命名收藏夹',
      '<div class="form-item"><div class="form-item__label">名称</div>' +
      '<input class="input" id="fav-rename-input" value="' + esc(oldName) + '" autocomplete="off"></div>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--primary" id="fav-rename-ok">保存</button>'));
    const ok = function () {
      const name = document.getElementById('fav-rename-input').value.trim();
      if (!name) { C.toast('名称不能为空', 'err'); return; }
      window.DYApi.favFolderAct('rename', { id: id, name: name })
        .then(function () { close(); reloadFav(); C.toast('已重命名', 'ok'); })
        .catch(function (e) { C.toast(e.message, 'err'); });
    };
    document.getElementById('fav-rename-ok').addEventListener('click', ok);
    document.getElementById('fav-rename-input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') ok();
    });
    setTimeout(function () { document.getElementById('fav-rename-input').select(); }, 0);
  }

  function deleteFolder(id, name) {
    C.confirm('删除收藏夹', '将删除「' + name + '」，其中的作品会回收进「未分类」，不会丢失。', function () {
      window.DYApi.favFolderAct('delete', { id: id })
        .then(function () {
          if (fav.cur === id) fav.cur = 'unfiled';
          reloadFav(); C.toast('已删除', 'ok');
        })
        .catch(function (e) { C.toast(e.message, 'err'); });
    }, '删除', true);
  }

  /* —— 条目 —— */
  function addItem() {
    const options = fav.folders.map(function (f) {
      return '<option value="' + esc(f.id) + '"' + (f.id === fav.cur ? ' selected' : '') + '>' +
        esc(f.name) + '</option>';
    }).join('');
    const close = C.open(C.modalFrame('添加作品',
      '<div class="form-item"><div class="form-item__label">作品链接</div>' +
      '<input class="input" id="fav-add-url" placeholder="粘贴抖音作品链接" ' +
      'style="font-family:var(--font-mono)" autocomplete="off"></div>' +
      '<div class="form-item"><div class="form-item__label">备注标题（可留空）</div>' +
      '<input class="input" id="fav-add-title" placeholder="可选，方便以后认出来"></div>' +
      '<div class="form-item"><div class="form-item__label">归入收藏夹</div>' +
      '<select class="input" id="fav-add-folder">' + options + '</select></div>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--primary" id="fav-add-ok">添加</button>', true));
    const ok = function () {
      const url = document.getElementById('fav-add-url').value.trim();
      if (!url) { C.toast('请粘贴作品链接', 'err'); return; }
      window.DYApi.favItemAct('add', {
        url: url,
        title: document.getElementById('fav-add-title').value.trim(),
        folder: document.getElementById('fav-add-folder').value,
      }).then(function (d) {
        close();
        fav.cur = d.item.folder || 'unfiled';
        reloadFav();
        C.toast('已收藏', 'ok');
      }).catch(function (e) { C.toast(e.message, 'err'); });
    };
    document.getElementById('fav-add-ok').addEventListener('click', ok);
    document.getElementById('fav-add-url').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') ok();
    });
    setTimeout(function () { document.getElementById('fav-add-url').focus(); }, 0);
  }

  function onItemAction(e) {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const id = el.dataset.id;
    const act = el.dataset.act;
    if (act === 'toggle') {
      window.DYApi.favItemAct('toggle-done', { id: id }).then(reloadFav)
        .catch(function (e2) { C.toast(e2.message, 'err'); });
    } else if (act === 'move') {
      el.addEventListener('change', function () {
        window.DYApi.favItemAct('move', { id: id, folder: el.value }).then(function () {
          C.toast('已移动', 'ok'); reloadFav();
        }).catch(function (e2) { C.toast(e2.message, 'err'); });
      }, { once: true });
    } else if (act === 'rename') {
      renameItem(id, el.dataset.title);
    } else if (act === 'remove') {
      C.confirm('移除作品', '仅从标记里移除，不影响本地已下载文件。', function () {
        window.DYApi.favItemAct('remove', { id: id }).then(function () {
          reloadFav(); C.toast('已移除', 'ok');
        }).catch(function (e2) { C.toast(e2.message, 'err'); });
      }, '移除', true);
    } else if (act === 'download') {
      window.DYPrefill = { url: el.dataset.url };
      window.DYRouter.go('douyin');
    }
  }

  function renameItem(id, oldTitle) {
    const close = C.open(C.modalFrame('重命名作品',
      '<div class="form-item"><div class="form-item__label">备注标题</div>' +
      '<input class="input" id="fav-item-rename" value="' + esc(oldTitle) + '" autocomplete="off"></div>',
      '<button class="btn btn--ghost" data-modal-close>取消</button>' +
      '<button class="btn btn--primary" id="fav-item-rename-ok">保存</button>'));
    const ok = function () {
      const title = document.getElementById('fav-item-rename').value.trim();
      if (!title) { C.toast('标题不能为空', 'err'); return; }
      window.DYApi.favItemAct('rename', { id: id, title: title })
        .then(function () { close(); reloadFav(); C.toast('已重命名', 'ok'); })
        .catch(function (e) { C.toast(e.message, 'err'); });
    };
    document.getElementById('fav-item-rename-ok').addEventListener('click', ok);
    document.getElementById('fav-item-rename').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') ok();
    });
    setTimeout(function () { document.getElementById('fav-item-rename').select(); }, 0);
  }

  window.DYRouter.register('favorites', render);
})();
