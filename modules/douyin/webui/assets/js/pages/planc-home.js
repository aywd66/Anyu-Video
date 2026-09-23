/* ============================================================
 * pages/planc-home.js — 工作台首页（方案C 01 设计 × 真实数据）
 * ------------------------------------------------------------
 * 合并策略：方案C 01 号稿的信息架构（hero/健康/模块/概览/日志）
 * 数据全部来自 DYApi/DYStore 真实层（bridge 模式=工程数据；
 * mock 模式=本地演示数据）。交互：健康重测/模块跳转/日志清空。
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  const LOG_KEY = 'dyui.planc.logs';
  const MAX_LOGS = 60;

  function loadLogs() {
    return U.ls.get(LOG_KEY, []);
  }
  function pushLog(level, tag, msg) {
    const logs = loadLogs();
    logs.unshift({ t: new Date().toTimeString().slice(0, 8), level, tag, msg });
    if (logs.length > MAX_LOGS) logs.length = MAX_LOGS;
    U.ls.set(LOG_KEY, logs);
    return logs;
  }
  function renderLogs() {
    const box = document.getElementById('ph-logs');
    if (!box) return;
    const logs = loadLogs();
    box.innerHTML = logs.length
      ? logs.map((l) =>
          '<div class="log-line"><span class="log-time">' + esc(l.t) + '</span>' +
          '<span class="log-' + esc(l.level) + '">[' + esc(l.level.toUpperCase()) + ' ] ' +
          esc(l.tag) + ' · ' + esc(l.msg) + '</span></div>').join('')
      : '<div class="log-line"><span class="log-info">暂无日志；操作下载/任务后会出现在这里。</span></div>';
  }

  function logTaskEvent(t) {
    const st = window.DYConfig.TASK_STATUS[t.status] || window.DYConfig.TASK_STATUS.pending;
    const level = t.status === 'success' ? 'ok' : t.status === 'failed' ? 'err' : 'info';
    pushLog(level, '任务', (t.title || t.url || '') + ' → ' + st.label);
    renderLogs();
  }

  async function renderHealth() {
    const grid = document.getElementById('ph-health');
    if (!grid) return;
    grid.innerHTML = '<div class="health-item"><div class="health-body"><div class="health-name">检测中…</div></div></div>';
    const bridge = window.DYApi.isBridge();
    let ms = null;
    let healthOk = false;
    const t0 = performance.now();
    try {
      const h = await window.DYApi.health();
      healthOk = h && h.status === 'ok';
      ms = Math.round(performance.now() - t0);
    } catch (e) { ms = -1; }

    const venvOk = bridge; // bridge 模式即说明 venv/依赖可用
    const ksNote = '快手模块通过工具箱桌面端使用（扫码登录在桌面入口）';
    const items = [
      {
        ok: bridge, name: '抖音数据源',
        pill: bridge ? '工程数据' : '演示数据',
        desc: bridge ? 'SQLite 历史库 + 实时任务队列已连接。' : '未连接工程数据，展示内置演示数据。',
        link: 'taskview', linkText: '查看任务中心 →'
      },
      {
        ok: healthOk, name: '服务健康',
        pill: healthOk ? '正常' : '异常',
        desc: healthOk ? ('健康检查通过 · 响应 ' + ms + 'ms。') : '健康检查失败，请检查常驻服务。',
        link: 'settings', linkText: '网络诊断 →'
      },
      {
        ok: true, name: '桌面工具箱',
        pill: '可用',
        desc: ksNote + '。方案C 界面与 WebUI 共用同一数据层。',
        link: 'settings', linkText: '打开设置 →'
      },
    ];
    grid.innerHTML = items.map((it) =>
      '<div class="health-item">' +
      '<span class="dot ' + (it.ok ? 'dot--ok' : 'dot--amber') + '"></span>' +
      '<div class="health-body">' +
      '<div class="health-name">' + esc(it.name) + ' <span class="badge ' + (it.ok ? 'badge--ok' : 'badge--warn') + '">' + esc(it.pill) + '</span></div>' +
      '<div class="health-desc">' + esc(it.desc) + '</div>' +
      '<a class="health-link" data-go="' + esc(it.link) + '">' + esc(it.linkText) + '</a>' +
      '</div></div>').join('');
  }

  async function renderStats() {
    const grid = document.getElementById('ph-stats');
    if (!grid) return;
    let total = 0, success = 0, failed = 0, running = 0, bytes = 0;
    try {
      if (window.DYApi.isBridge()) {
        const feed = await window.DYApi.taskFeed();
        const all = (feed.live || []).concat(feed.history || []);
        all.forEach((t) => {
          total += 1;
          if (t.status === 'success') success += 1;
          else if (t.status === 'failed') failed += 1;
          else if (t.status === 'running' || t.status === 'pending') running += 1;
        });
      } else {
        const tasks = window.DYStore.state.tasks;
        tasks.forEach((t) => {
          total += 1;
          if (t.status === 'success') success += 1;
          else if (t.status === 'failed') failed += 1;
          else if (t.status === 'running' || t.status === 'pending') running += 1;
        });
      }
    } catch (e) { /* 统计失败按 0 呈现，不阻塞首页 */ }
    const rate = total ? Math.round((success / total) * 1000) / 10 : null;
    grid.innerHTML =
      '<div class="stat-card"><div class="stat-icon si-teal">▦</div>' +
      '<div class="stat-value-big">' + U.group(total) + '<small>条</small></div>' +
      '<div class="stat-caption">任务总数（历史+进行中）</div></div>' +
      '<div class="stat-card"><div class="stat-icon si-green">✓</div>' +
      '<div class="stat-value-big">' + U.group(success) + '<small>条</small></div>' +
      '<div class="stat-caption">成功任务</div></div>' +
      '<div class="stat-card"><div class="stat-icon si-red">✕</div>' +
      '<div class="stat-value-big">' + U.group(failed) + '<small>条</small></div>' +
      '<div class="stat-caption">失败任务</div></div>' +
      '<div class="stat-card"><div class="stat-icon si-slate">⏵</div>' +
      '<div class="stat-value-big">' + U.group(running) + '<small>个</small></div>' +
      '<div class="stat-caption">进行中 / 排队</div></div>';
  }

  async function renderRecent() {
    const box = document.getElementById('ph-recent');
    if (!box) return;
    let tasks = [];
    try {
      if (window.DYApi.isBridge()) {
        const feed = await window.DYApi.taskFeed();
        tasks = (feed.live || []).concat(feed.history || []).slice(0, 5);
      } else {
        tasks = window.DYStore.state.tasks.slice(0, 5);
      }
    } catch (e) { tasks = []; }
    box.innerHTML = tasks.length
      ? tasks.map((t) => C.taskRow(t)).join('')
      : C.empty('douyin', 'download');
    box.querySelectorAll('[data-act]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const act = btn.dataset.act;
        const id = btn.dataset.id;
        const store = window.DYStore;
        if (act === 'cancel') { window.DYApi.cancelTask(id); C.toast('任务已取消', 'ok'); }
        else if (act === 'retry') { window.DYApi.retryTask(id); C.toast('已重新排队', 'ok'); }
        else if (act === 'remove') { store.removeTask(id); C.toast('任务已移除', 'ok'); }
        else if (act === 'copy') {
          const t = store.state.tasks.find((x) => x.id === id);
          if (t) U.copyText(t.url).then((ok) => C.toast(ok ? '已复制' : '复制失败', ok ? 'ok' : 'err'));
        }
        renderRecent();
        window.DYApp.updateTaskBadge();
      });
    });
  }

  /* —— 统一输入口（V2 决策 1） ——
   * 用户不该先判断「这是哪个平台的链接」再决定去哪个模块 ——
   * 这是机器擅长的事。粘贴任何链接，这里识别平台并分流：
   *   能在 WebUI 直接下载的（抖音/TikTok/YouTube）→ 带链接跳到对应工作台；
   *   只有桌面模块才有的（快手/clawfetch 直链）→ 明确告知去哪，不假装能下载。 */
  const ROUTE_RULES = [
    { re: /douyin\.com|iesdouyin\.com/i, page: 'douyin', label: '抖音' },
    { re: /tiktok\.com/i,                page: 'tiktok', label: 'TikTok' },
    { re: /youtube\.com|youtu\.be/i,     page: 'youtube', label: 'YouTube' },
  ];
  const DESKTOP_ONLY = [
    { re: /kuaishou\.com|chenzhongtech\.com|ksurl\.cn/i,
      label: '快手', tip: '快手模块在桌面工具箱中运行：请从桌面工具箱主界面进入「快手一键下载器」。' },
    { re: /^https?:\/\//i,
      label: '直链', tip: '直链下载（断点续传 / SHA-256 校验）由桌面工具箱的 clawfetch 模块处理。' },
  ];

  function detectRoute(text) {
    const urls = U.extractUrls(text);
    if (!urls.length) return null;
    const url = urls[0];
    for (const r of ROUTE_RULES) if (r.re.test(url)) return { page: r.page, label: r.label, url };
    for (const r of DESKTOP_ONLY) if (r.re.test(url)) return { desktop: true, label: r.label, tip: r.tip, url };
    return { desktop: true, label: '未知来源', tip: '未识别的平台。若确为直链，请用桌面工具箱的 clawfetch 模块。', url };
  }

  function bindPaste(view) {
    const input = view.querySelector('#ph-paste');
    const hint = view.querySelector('#ph-paste-hint');
    const go = view.querySelector('#ph-paste-go');
    if (!input || !hint || !go) return;

    const updateHint = () => {
      const hit = detectRoute(input.value);
      if (!hit) {
        hint.innerHTML = input.value.trim()
          ? '<span class="dot dot--amber"></span> 未识别到 http(s) 链接，请检查粘贴内容'
          : '';
        return hit;
      }
      if (hit.desktop) {
        hint.innerHTML = '<span class="dot dot--amber"></span> 识别为 <b>' + esc(hit.label) +
          '</b> · ' + esc(hit.tip);
      } else {
        hint.innerHTML = '<span class="dot dot--green"></span> 识别为 <b>' + esc(hit.label) +
          '</b> · 回车或点「识别并分流」继续';
      }
      return hit;
    };

    const submit = () => {
      const hit = updateHint();
      if (!hit) { input.focus(); return; }
      if (hit.desktop) {
        C.toast(hit.tip, 'warn');
        return;
      }
      // 链接接力：工作台渲染后读取并自动填入
      window.DYPrefill = { url: hit.url };
      window.DYRouter.go(hit.page);
    };

    input.addEventListener('input', U.debounce(updateHint, 120));
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    go.addEventListener('click', submit);
  }

  function render(view) {
    view.className = 'content';
    view.innerHTML =
      '<div class="hero">' +
        '<h1>视频下载工具箱，三合一下载中心</h1>' +
        '<p>抖音高清无水印批量下载、快手作者主页整站归档、任意直链断点续传校验 —— 三个模块共用同一套任务队列、台账与历史记录。</p>' +
        '<div class="hero-paste">' +
          '<input class="hero-paste__input" id="ph-paste" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="粘贴链接：抖音 / TikTok / YouTube / 快手 / 直链 —— 自动识别分流" aria-label="粘贴下载链接">' +
          '<button class="btn btn-hero" id="ph-paste-go">' + I.icon('download', 16) + '识别并分流</button>' +
        '</div>' +
        '<div class="hero-paste__hint" id="ph-paste-hint" aria-live="polite"></div>' +
        '<div class="hero-actions">' +
          '<button class="btn btn-hero-line" data-go="tasks">查看任务中心</button>' +
        '</div>' +
        '<div class="hero-meta">' +
          '<div><strong>' + esc(window.DYConfig.APP.name) + '</strong>当前版本 · 保留版 v1.0</div>' +
          '<div><strong>3</strong>下载引擎共用队列</div>' +
          '<div><strong>SQLite</strong>本地台账去重</div>' +
          '<div><strong>离线</strong>数据不出本机</div>' +
        '</div>' +
      '</div>' +

      '<section class="card mt-5">' +
        '<div class="card-head"><h2 class="card__title">环境健康检查</h2>' +
        '<button class="btn btn--ghost btn--sm right" id="ph-recheck">重新检测</button></div>' +
        '<div class="health-grid" id="ph-health"></div>' +
      '</section>' +

      '<section class="mt-5">' +
        '<div class="card-head"><h2 class="card__title">下载模块</h2></div>' +
        '<div class="module-grid">' +
          '<div class="module-card"><div class="module-icon ic-dy">🎵</div>' +
            '<div class="module-name">抖音高清下载器</div><div class="module-sub">批量 · 无水印</div>' +
            '<p class="module-desc">粘贴抖音链接或分享口令即可批量下载，自动识别内容形态并写入本地历史库。</p>' +
            '<div class="tags"><span class="tag">视频 / 图集</span><span class="tag">音乐 · 直播回放</span></div>' +
            '<div class="module-foot"><button class="btn btn--accent" data-go="douyin">进入下载</button>' +
            '<button class="btn btn--ghost btn--sm" data-go="archive">历史库</button></div></div>' +
          '<div class="module-card"><div class="module-icon ic-ks">📹</div>' +
            '<div class="module-name">快手主页下载</div><div class="module-sub">整站归档 · 并行</div>' +
            '<p class="module-desc">从桌面工具箱启动快手模块：粘贴分享链接解析作者主页，多任务并行、台账去重。</p>' +
            '<div class="tags"><span class="tag">全部作品 / 仅最新</span><span class="tag">扫码登录</span></div>' +
            '<div class="module-foot"><button class="btn btn--accent" data-go="queue">多链排队</button>' +
            '<button class="btn btn--ghost btn--sm" data-go="archive">台账</button></div></div>' +
          '<div class="module-card"><div class="module-icon ic-fl">🔗</div>' +
            '<div class="module-name">直链下载 clawfetch</div><div class="module-sub">通用 · 可校验</div>' +
            '<p class="module-desc">任意 http/https 直链：断点续传、失败重试、SHA-256 校验；也可仅解析不下载。</p>' +
            '<div class="tags"><span class="tag">断点续传</span><span class="tag">SHA-256</span><span class="tag">JSONL 台账</span></div>' +
            '<div class="module-foot"><button class="btn btn--primary" data-go="tasks">任务中心</button>' +
            '<button class="btn btn--ghost btn--sm" data-go="settings">设置</button></div></div>' +
        '</div>' +
      '</section>' +

      '<section class="mt-5">' +
        '<div class="card-head"><h2 class="card__title">数据概览</h2></div>' +
        '<div class="stat-grid" id="ph-stats"></div>' +
      '</section>' +

      '<section class="mt-5">' +
        '<div class="card-head"><h2 class="card__title">最近任务</h2>' +
        '<button class="btn btn--ghost btn--sm right" data-go="tasks">全部任务</button></div>' +
        '<div id="ph-recent"></div>' +
      '</section>' +

      '<section class="card mt-5">' +
        '<div class="card-head"><h2 class="card__title">运行日志</h2>' +
        '<button class="btn btn--ghost btn--sm right" id="ph-clear">清空显示</button></div>' +
        '<div class="log-box" id="ph-logs"></div>' +
      '</section>';

    /* —— 交互绑定 —— */
    view.querySelectorAll('[data-go]').forEach((el) =>
      el.addEventListener('click', () => window.DYRouter.go(el.dataset.go)));
    bindPaste(view);
    view.querySelector('#ph-recheck').addEventListener('click', async () => {
      C.toast('重新检测中…', 'ok');
      await renderHealth();
      C.toast('检测完成', 'ok');
    });
    view.querySelector('#ph-clear').addEventListener('click', () => {
      U.ls.set(LOG_KEY, []);
      renderLogs();
      C.toast('日志已清空', 'ok');
    });

    renderHealth();
    renderStats();
    renderRecent();
    renderLogs();
    pushLog('info', '工作台', '首页已加载（' + (window.DYApi.isBridge() ? '工程数据' : '演示数据') + '模式）');
    renderLogs();
  }

  /* 事件订阅：任务状态变化 → 日志 + 概览 + 最近任务 */
  ['task:created', 'task:updated', 'task:removed', 'task:bulk-changed'].forEach((ev) => {
    window.DYUtils.bus.on(ev, (t) => {
      if (t && t.id) logTaskEvent(t);
      renderStats();
      renderRecent();
    });
  });

  window.DYRouter.register('planc-home', render);
})();
