/* ============================================================
 * pages/launcher.js — 启动台（五个 .bat 入口的统一界面）
 * ------------------------------------------------------------
 * 背景：原本五个 .bat 各写各的 —— 一个黑窗 CLI、一个无声 start、
 * 一个 set /p 中文提示、一个带 pause 的错误分支。用户要求
 * 「一整套可以完美结合交互页面 ui 要有统一性」。
 *
 * 做法：这五个入口共用本页作为外壳，用 hash 切片
 *   #/launch/toolbox | douyin | kuaishou | live | direct
 * 顶栏、排版、间距、状态语言全部复用既有 V2 设计系统，
 * 各工具只在「自己的内容与操作」上不同。
 *
 * 关键取舍：抖音下载器与程序下载原本是终端交互程序（终端就是它们
 * 的进度界面）。这里不弹黑窗，改成「页面内收链接 → 后台任务队列」
 * —— 前者用 /api/bridge/download（工程 run.py CLI），
 * 后者用 /api/bridge/direct/download（clawfetch）。两者都已存在，
 * 本页只是把它们接到一个统一的输入口上。
 *
 * mock 模式（直接双击 index.html）拿不到任何本机事实，
 * 如实显示「未连接工程服务」，不伪造就绪状态。
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  /* —— 五个工具的身份与文案 ——
   * 这是"改文案只改这里"的局部版本：本页所有工具相关文案集中在此。
   * ready 字段来自后端 /api/bridge/launcher，这里只放静态身份信息。 */
  const TOOLS = [
    {
      id: 'toolbox',
      label: '工具箱总览',
      name: '视频下载工具箱',
      sub: '四个模块 · 统一入口',
      icon: 'bolt',
      accent: 'cyan',
      blurb: '抖音 · 快手 · 直链 · 直播录制，四个模块各自独立、互不依赖，可单独运行，也可以从这里统一进入。',
      badge: '总览',
    },
    {
      id: 'douyin',
      label: '抖音下载器',
      name: '抖音下载器',
      sub: '视频 · 图集 · 主页 · 合集',
      icon: 'tiktok',
      accent: 'pink',
      blurb: '粘贴抖音作品、主页或分享口令，后台批量下载；默认无水印，支持增量去重，多行长文本会自动挑出链接。',
      badge: '模块 1',
      kind: 'douyin',
      dirKey: 'douyin',
    },
    {
      id: 'kuaishou',
      label: '快手一键下载器',
      name: '快手一键下载器',
      sub: '主页批量 · 扫码登录',
      icon: 'video',
      accent: 'cyan',
      blurb: '解析快手分享链接并批量下载作者主页作品。已登录时直接在页面内后台下载；未登录会引导扫码，登录态落盘后一劳永逸。',
      badge: '模块 2',
      kind: 'kuaishou',
      dirKey: 'kuaishou',
    },
    {
      id: 'direct',
      label: '程序下载',
      name: '程序下载（直链）',
      sub: '断点续传 · SHA-256 校验',
      icon: 'download',
      accent: 'amber',
      blurb: '纯标准库直链下载器。支持中转页解析成真实直链、断点续传、失败重试与 SHA-256 校验；也可以只解析不下载，拿到链子交给别处。',
      badge: '模块 3',
      kind: 'direct',
      dirKey: 'direct',
    },
    {
      id: 'ty',
      label: 'TikTok / YouTube 下载',
      name: 'TikTok / YouTube 下载',
      sub: '粘链接即下 · 自动走代理',
      icon: 'video',
      accent: 'red',
      blurb: 'yt-dlp 原生支持两大海外平台，粘贴视频链接直接下载最高画质 mp4；'
           + '境内网络会自动探测本地代理（Clash 等），探测不到会明确提示先开代理。'
           + '也支持频道/主页链接批量下载。',
      badge: '模块 5',
      kind: 'ty',
      dirKey: 'ty',
    },
    {
      id: 'ytdlp',
      label: 'yt-dlp 万能下载',
      name: 'yt-dlp 万能下载',
      sub: '全站通吃 · 音频提取 · 字幕 · 批量',
      icon: 'download',
      accent: 'green',
      blurb: 'yt-dlp 官方 ~1754 个站点提取器的统一入口：Twitter/X、Instagram、微博、Vimeo、'
           + 'Twitch 回放等专属模块不覆盖的站都走这里。支持下载前预览、音频提取（MP3 等）、'
           + '字幕下载、播放列表批量、Cookie 登录、存档去重与 SponsorBlock。',
      badge: '模块 7',
      kind: 'ytdlp',
      dirKey: 'ytdlp',
    },
    {
      id: 'live',
      label: '直播流录制工具',
      name: '直播流录制工具',
      sub: '开播自动录制 · 分卷 · 无损合并',
      icon: 'radio',
      accent: 'cyan',
      /* 这段文案的分寸很重要：本页是「直播流录制工具」的门面（启动直播录制器.bat
       * 打开的就是它），所以不能再像早期那样一上来就自我否定说「录制在那个窗口里进行」。
       * 但录制内核确实跑在独立的 PyQt6 进程里，也不该假装本页能独立录制。
       * 结论：说清「这是这个工具的入口与总览，录制内核在独立进程」，两句话都成立。 */
      blurb: '监控快手 / 抖音直播间，开播自动录制，断流重连、分卷、无损合并；'
           + '下方的「Stripchat 房间录制」直接粘贴房间链接即可录（18+，含 VR）。',
      badge: '模块 4',
      kind: 'live',
      dirKey: 'live',
      hasSelftest: true,
      hasStripchat: true,
    },
  ];

  const byId = (id) => TOOLS.find((t) => t.id === id) || TOOLS[1];

  /* 当前视图（从 hash 解析）。null = 总览 */
  function currentToolId() {
    const raw = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
    const m = /^launch\/([a-z]+)$/.exec(raw);
    if (!m) return 'toolbox';
    return byId(m[1]).id;
  }

  /* ============================================================
   * 渲染
   * ============================================================ */
  function render(view) {
    const tool = byId(currentToolId());
    view.className = 'content';
    delete view.dataset.platform;

    /* 面包屑：live 是从「启动直播录制器.bat」直接落地的一屏，标「直播流录制」
     * 而不是「启动台 · …」—— 后者是给"从总览点进来"的语境用的。
     * 面包屑同时是回到启动台总览的可见路径，所以仍保留「启动台」可点入口。 */
    const crumb = tool.id === 'live'
      ? '<a class="crumb__link" href="#/launch/toolbox">启动台</a> · 直播流录制'
      : '启动台 · ' + esc(tool.label);

    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div class="page-head__row">' +
          '<div>' +
            '<div class="crumb"><span class="crumb__dot"></span>' + crumb + '</div>' +
            '<h1 class="page-title">' + esc(tool.name) + '</h1>' +
            '<p class="page-sub">' + esc(tool.blurb) + '</p>' +
          '</div>' +
          '<div class="flex">' +
            '<button class="btn btn--ghost btn--sm" id="lt-refresh">' + I.icon('refresh', 14) + '刷新状态</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      tabs(tool.id) +
      '<div id="lt-body"></div>';

    view.querySelector('#lt-refresh').addEventListener('click', () => load(view));
    bindTabs(view);
    load(view);
  }

  /* —— 工具横排 tab：切工具只改 hash，路由会重渲染 ——
   *
   * live 不渲染这排 tab。原因：`启动直播录制器.bat` 直接打开 #/launch/live，
   * 那一屏是「直播流录制工具」自己的门面 —— 顶着五个工具 tab 会让人以为
   * 自己进的是启动台、只是恰好停在直播那一格，于是一进页面先看到四个
   * 跟自己无关的入口，还得先意识到「原来这里是启动台」。
   * 从启动台总览（#/launch/toolbox）进来的路径不受影响：那里五格都在，
   * 点「直播流录制工具」进本页后靠左上角面包屑仍能退回启动台。
   * 其它工具的 tab 行为一律不变。 */
  function tabs(activeId) {
    if (activeId === 'live') return '';
    return (
      '<div class="lt-tabs" role="tablist" aria-label="工具切换">' +
      TOOLS.map((t) =>
        '<button class="lt-tab' + (t.id === activeId ? ' is-on' : '') + '" role="tab" ' +
        'aria-selected="' + (t.id === activeId) + '" data-tool="' + esc(t.id) + '">' +
        '<span class="lt-tab__dot" data-ready="1" aria-hidden="true"></span>' +
        I.icon(t.icon, 14) + esc(t.label) + '</button>'
      ).join('') +
      '</div>'
    );
  }

  function bindTabs(view) {
    view.querySelectorAll('.lt-tab').forEach((b) => {
      b.addEventListener('click', () => {
        const id = b.dataset.tool;
        window.DYRouter.go('launch/' + id);
      });
    });
  }

  /* ============================================================
   * 数据加载
   * ============================================================ */
  async function load(view) {
    const box = view.querySelector('#lt-body');
    if (!box) return;
    box.innerHTML = C.skeletonRows(3);

    const tool = byId(currentToolId());

    /* live 这一屏要三份数据，能力各不相同：
     *   · /api/bridge/launcher        —— 模块就绪与否（其余工具也用它）
     *   · /api/bridge/live            —— 只读总览（config.json 原始事实，离线也能看）
     *   · /api/bridge/live/daemon/status —— 内核实时状态（本页的主体，离线时 503）
     * 三者互不依赖，串行只会白等，必须并发。
     *
     * 内核那份**先 ensure 再 status**：进页面就顺手把内核拉起来，
     * 用户不必先自己找「启动内核」。autostart=true 是幂等的 ——
     * 已在跑就只回状态，不会新起进程。 */
    let d = null;
    let live = null;
    let k = null;
    try {
      if (tool.id === 'live') {
        const [a, b] = await Promise.all([
          window.DYApi.launcher(),
          window.DYApi.live().catch(() => null),
        ]);
        d = a;
        live = b;
        // ensure 失败（模块没就绪等）不该让整页崩，只影响能不能控制内核
        try {
          const ens = await window.DYApi.liveKernelEnsure(true);
          k = ens && ens.ok ? ens : await window.DYApi.liveKernelStatus().catch(() => null);
        } catch (e) {
          k = await window.DYApi.liveKernelStatus().catch(() => null);
        }
        /* ⚠️ ensure 只回 ok/daemon/pid/version/started，**不含** rooms/counts/
         * settings/登录态（它走的是 /health，不是 /status）。而本页主体全要那些
         * 字段 —— 不补这一步，页面会渲染成「内核在线但 0 个监控对象」，
         * 比离线更糟：它在撒谎，用户会以为监控清单被清空了。
         * 判据用 `rooms === undefined`（而不是 !rooms），0 个房间是合法值。 */
        if (k && k.ok && k.rooms === undefined) {
          const full = await window.DYApi.liveKernelStatus().catch(() => null);
          if (full) k = full;
        }
      } else {
        d = await window.DYApi.launcher();
      }
    } catch (e) {
      d = null;
    }

    if (!d) {
      box.innerHTML = offlineNotice(tool);
      return;
    }

    // tab 上的就绪点跟着真实状态走，不是装饰
    syncTabDots(view, d);

    box.innerHTML =
      (tool.id === 'toolbox'
        ? overviewSections(d)
        : toolSections(tool, d, live, k));

    bindActions(view, tool, d, k);

    // 日志与扫码是「进页面就看」的东西，渲染完立即拉一次
    if (tool.id === 'live') {
      lrLogSince = 0;
      lrLogLines = [];   // 整页重载 = 新的一段会话，导出缓冲随之重置
      refreshLog(view);
      startLivePolling(view, k);
    } else {
      stopLivePolling();
    }
    // Stripchat：状态徽标 + 日志窗跟随录制进程（区块在直播页上，离开即停）
    if (tool.id === 'live') {
      startScPolling(view);
    } else {
      stopScPolling();
    }
  }

  /* —— Stripchat 状态轮询（3s）：徽标 / 按钮态 / 日志窗 ——
   * 与 lr* 轮询同一条纪律：timer 挂在页内变量上，load() 重渲染前先停旧的。 */
  let scTimer = null;

  function startScPolling(view) {
    stopScPolling();
    scPollOnce(view);
    scTimer = setInterval(() => {
      if (!view.isConnected) { stopScPolling(); return; }
      scPollOnce(view);
    }, 3000);
  }

  function stopScPolling() {
    if (scTimer) { clearInterval(scTimer); scTimer = null; }
  }

  async function scPollOnce(view) {
    let st;
    try { st = await window.DYApi.scStatus(); } catch (e) { return; }
    const badge = view.querySelector('#sc-badge');
    const hint = view.querySelector('#sc-hint');
    const go = view.querySelector('#sc-go');
    const stop = view.querySelector('#sc-stop');
    const log = view.querySelector('#sc-log');
    const running = !!(st && st.running);
    if (badge) badge.innerHTML = running
      ? C.badge('录制中 · ' + ((st.info && st.info.user) || '') + ((st.info && st.info.vr) ? ' · VR' : ''), 'ok')
      : C.badge('未在录制', '');
    if (go) go.disabled = running;
    if (stop) stop.disabled = !running;
    if (log) {
      const lines = (st && st.log) || [];
      log.innerHTML = lines.length
        ? lines.map((l) => '<div class="lr-log__line">' + esc(l) + '</div>').join('')
        : '<div class="lr-log__empty">暂无日志；开始录制后这里会实时滚动。</div>';
      log.scrollTop = log.scrollHeight;
    }
  }

  function syncTabDots(view, d) {
    view.querySelectorAll('.lt-tab').forEach((b) => {
      const t = d.tools && d.tools[b.dataset.tool];
      const dot = b.querySelector('.lt-tab__dot');
      if (dot) dot.dataset.ready = t && t.ready ? '1' : '0';
    });
  }

  /* mock 模式：如实说明为什么看不到东西、以及怎么让它可用 */
  function offlineNotice(tool) {
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">未连接工程服务</h2></div>' +
        '<div class="empty">' +
          '<div class="empty__icon">' + I.icon('terminal', 34) + '</div>' +
          '<div class="empty__title">启动台需要桥接服务</div>' +
          '<div class="empty__desc">当前页面是直接打开的静态文件，拿不到本机的模块、运行时与目录状态，' +
          '因此不显示任何就绪结论。用工具箱根目录的「启动工具箱」或任一个工具入口启动后，' +
          '本页会显示真实的检查结果。</div>' +
        '</div>' +
      '</section>'
    );
  }

  /* ============================================================
   * 总览视图（toolbox）—— 五个工具一屏看完 + 环境
   * ============================================================ */
  /* ============================================================
   * 统一链接入口：识别 → 路由（2026-11-07）
   * ------------------------------------------------------------
   * 链接输入收敛为两处：
   *   · 启动台的「链接下载」框 —— 识别平台后路由到对应下载通道
   *     （抖音 / 快手 / TikTok-YouTube / 直链）；识别出**直播间**链接
   *     会自动转给录制监控（用户不用管选哪个工具）。
   *   · 直播录制页的「添加监控房间」框 —— 自动识别直播平台。
   *
   * 识别失败的兜底（如实、不静默）：
   *   · 一般 http(s) 链接识别不出平台 → 按「直链下载」处理（直链通道
   *     字面上接受任意 URL，是最合理的兜底），汇总里会写明走了兜底；
   *   · 文本里没有 http(s) 链接 → 不提交，提示连同分享链接一起粘贴；
   *   · 直播框里贴了视频链接 → 不提交（静默转下载会让用户以为在录），
   *     明确提示去「链接下载」入口；
   *   · 抖音直播的 v.douyin.com 分享短链无法从域名区分直播/视频 → 按域名
   *     规则走抖音下载通道，真实类型由后端解析判定。
   * ============================================================ */
  function classifyLink(u) {
    const s = String(u || '');
    if (/live\.kuaishou\.com/i.test(s)) return { type: 'live', label: '快手直播 → 加入监控' };
    if (/live\.douyin\.com/i.test(s)) return { type: 'live', label: '抖音直播 → 加入监控' };
    if (/youtube\.com|youtu\.be|tiktok\.com/i.test(s)) return { type: 'download', channel: 'ty', label: 'TikTok / YouTube → 任务队列' };
    if (/douyin\.com|iesdouyin\.com/i.test(s)) return { type: 'download', channel: 'douyin', label: '抖音 → 任务队列' };
    if (/bilibili\.com|b23\.tv/i.test(s)) return { type: 'download', channel: 'bilibili', label: '哔哩哔哩 → 任务队列' };
    if (/kuaishou\.com/i.test(s)) return { type: 'download', channel: 'kuaishou', label: '快手 → 任务队列' };
    /* 兜底升级（2026-11-13）：原先未识别的平台一律按「直链下载」处理，但
     * clawfetch 只认直链文件；现在优先交给 yt-dlp 万能下载（~1754 站点提取器，
     * Twitter/X、Instagram、微博、Vimeo 等都能吃），ytdlp 未就绪再回落直链。
     * channel 用 'any' 占位，由 dispatchUnified 按就绪状态分派。 */
    if (/^https?:\/\//i.test(s)) return { type: 'download', channel: 'any', label: '万能下载（未识别出平台，交给 yt-dlp）' };
    return null;
  }

  function unifiedSection(d) {
    const tools = d.tools || {};
    const anyReady = ['douyin', 'kuaishou', 'direct', 'ty', 'ytdlp'].some(
      (k) => tools[k] && tools[k].ready);
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">链接下载</h2>' +
        '<span class="small faint">粘贴链接或整段分享文案（可一次多条），自动识别平台并路由到对应下载</span></div>' +
        '<textarea class="input" id="utl-input" rows="3" spellcheck="false" autocomplete="off" ' +
          'placeholder="例如：https://v.douyin.com/xxx、https://v.kuaishou.com/xxx、https://www.bilibili.com/…、' +
          'https://youtube.com/watch?v=…、任意 http(s) 直链…" ' +
          (anyReady ? '' : ' disabled') + ' aria-label="下载链接"></textarea>' +
        '<div class="input-group mt-2">' +
          '<button class="btn btn--primary" id="utl-go"' + (anyReady ? '' : ' disabled') + '>' +
            I.icon('download', 15) + '自动识别并下载</button>' +
          '<span class="small faint" id="utl-hint" aria-live="polite">' +
            '快手分享短链会自动判定直播/作品：直播进录制监控、作品进下载。'
            + '兜底：识别不出平台的 http(s) 链接交给 yt-dlp 万能下载（ytdlp 未就绪时回落直链）；直播间链接自动转给录制监控。</span>' +
        '</div>' +
        '<div id="utl-result" aria-live="polite"></div>' +
      '</section>'
    );
  }

  async function dispatchUnified(view) {
    const input = view.querySelector('#utl-input');
    const btn = view.querySelector('#utl-go');
    const hint = view.querySelector('#utl-hint');
    const result = view.querySelector('#utl-result');
    const raw = (input.value || '').trim();
    const setHint = (h) => { if (hint) hint.innerHTML = h; };

    if (!raw) { C.toast('请先粘贴链接', 'warn'); return; }
    const urls = U.extractUrls(raw);
    if (!urls.length) {
      C.toast('这段文本里没有 http(s) 链接 —— 请连同分享链接一起粘贴', 'warn');
      return;
    }

    /* 识别 → 按通道分桶 */
    const buckets = { douyin: [], kuaishou: [], ty: [], direct: [], bilibili: [], live: [], any: [] };
    urls.forEach((u) => {
      const c = classifyLink(u);
      if (!c) return;
      if (c.type === 'live') buckets.live.push(u);
      else buckets[c.channel].push(u);
    });

    /* 快手分享短链（v.kuaishou.com/…）无法从 URL 区分视频/直播 ——
     * 先到后端跟一次重定向判定：直播的进监控桶、作品的留在快手桶。
     * ⚠️ 推进监控桶的是**原始短链**：监控清单里已有的快手房间全是短链
     * （录制器首次轮询时自己解析出 room_id），与现有成功路径保持同格式。
     * 解析失败时用分享文案关键词兜底：文案里带「直播/围观/开播」就按直播处理。
     * （2026-11-13 修复：直播分享短链此前被误送进作品下载通道；
     *   18:02 追加兜底——resolveLink 是新增方法，旧缓存页面里不存在，
     *   调用抛 TypeError 会被静默吞掉导致短链留在快手桶，所以必须
     *   检查方法存在 + 文案关键词兜底 + index.html 升 api.js 版本号。） */
    const ksShorts = buckets.kuaishou.filter((u) => /\/\/v\.kuaishou\.com\//i.test(u));
    const LIVE_HINT = /直播|围观|开播|看播|直播间/;
    for (const u of ksShorts) {
      let resolved = null;
      if (typeof window.DYApi.resolveLink === 'function') {
        try {
          resolved = await window.DYApi.resolveLink(u);
        } catch (e) { resolved = null; }
      }
      if (resolved) {
        if (resolved.is_live) {
          buckets.live.push(u);
          buckets.kuaishou = buckets.kuaishou.filter((x) => x !== u);
        }
      } else if (LIVE_HINT.test(raw)) {
        // 解析不可用/失败：分享文案明说直播就信文案，进监控由录制器自行解析
        buckets.live.push(u);
        buckets.kuaishou = buckets.kuaishou.filter((x) => x !== u);
      }
    }

    btn.disabled = true;
    setHint('<span class="dot dot--amber dot--pulse"></span> 正在按识别结果分发…');
    try {
    const okLines = [];
    const failLines = [];
    const step = async (name, fn) => {
      try { await fn(); okLines.push(name); }
      catch (e) { failLines.push(name + '：' + (e.message || '提交失败')); }
    };

    if (buckets.douyin.length) {
      await step('抖音 ×' + buckets.douyin.length + ' → 任务队列', async () => {
        await window.DYApi.submitDownload({
          url: buckets.douyin.join(' '),
          options: { modes: ['post'], number: 0, quality: 'highest', increase: true },
        });
      });
    }
    if (buckets.kuaishou.length) {
      await step('快手 ×' + buckets.kuaishou.length + ' → 任务队列', async () => {
        // 与原快手页同口径：未登录先引导扫码，扫码后重新粘贴即可
        const st = await window.DYApi.kuaishouStatus();
        if (st && st.need_login) {
          C.toast('未登录快手，正在唤起扫码窗口；扫码完成后重新粘贴即可', 'warn');
          await window.DYApi.kuaishouLogin();
          return;
        }
        for (const u of buckets.kuaishou) {
          await window.DYApi.kuaishouDownload(u, 'all');
        }
      });
    }
    if (buckets.ty.length) {
      await step('TikTok / YouTube ×' + buckets.ty.length + ' → 任务队列', async () => {
        for (const u of buckets.ty) {
          await window.DYApi.tyDownload(u, 'all');
        }
      });
    }
    if (buckets.direct.length) {
      await step('直链 ×' + buckets.direct.length + ' → 直链队列', async () => {
        /* 多链接走一个批量任务（clawfetch 按域名分链，避免同源 502）——
         * 与原直链页同口径。 */
        await window.DYApi.directDownload(
          buckets.direct.length > 1 ? buckets.direct : buckets.direct[0], null);
      });
    }
    if (buckets.bilibili.length) {
      await step('哔哩哔哩 ×' + buckets.bilibili.length + ' → 任务队列', async () => {
        for (const u of buckets.bilibili) {
          await window.DYApi.biliBridgeSubmit(u, 'video');
        }
      });
    }
    if (buckets.any.length) {
      await step('万能下载 ×' + buckets.any.length + ' → 任务队列', async () => {
        /* 'any' 桶 = 未识别出平台的链接：ytdlp 就绪就交给 yt-dlp 万能下载，
         * 否则回落直链（clawfetch 只认直链，但比直接报错好）。 */
        let ytdlpReady = false;
        try {
          const ld = await window.DYApi.launcher();
          ytdlpReady = !!(ld && ld.tools && ld.tools.ytdlp && ld.tools.ytdlp.ready);
        } catch (e) { /* 查询失败按未就绪处理 */ }
        for (const u of buckets.any) {
          if (ytdlpReady) {
            await window.DYApi.ytdlpDownload(u, {});
          } else {
            await window.DYApi.directDownload(u, null);
          }
        }
      });
    }
    if (buckets.live.length) {
      await step('直播间 ×' + buckets.live.length + ' → 已加入录制监控', async () => {
        for (const u of buckets.live) {
          await window.DYApi.liveAddRoom(u);
        }
      });
    }

    input.value = '';
    const parts = [];
    if (okLines.length) parts.push('✓ ' + okLines.join('；'));
    if (failLines.length) parts.push('✗ ' + failLines.join('；'));
    if (result) {
      result.innerHTML =
        '<div class="' + (failLines.length ? 'error-box' : '') + '" style="' +
        (failLines.length ? '' : 'color:var(--ok)') + 'margin-top:10px;padding:8px 10px">' +
        esc(parts.join('\n')) + '</div>';
    }
    setHint('<span class="dot ' + (failLines.length ? 'dot--amber' : 'dot--green') +
            '"></span> 分发完成：' + okLines.length + ' 类成功' +
            (failLines.length ? ('，' + failLines.length + ' 类失败') : ''));
    C.toast(failLines.length ? '部分链接分发失败，见下方明细' : '已按类型全部分发', 
            failLines.length ? 'warn' : 'ok');
    if (view.querySelector('#lt-recent')) await loadRecent(view);
    } finally {
      /* 恢复按钮：分发完成后必须能再次提交（漏了这句，第二次粘贴就点不动） */
      if (btn.isConnected) btn.disabled = false;
    }
  }

  /* 统一入口绑定：textarea Ctrl/Cmd+Enter 或按钮提交 */
  function bindUnified(view) {
    const go = view.querySelector('#utl-go');
    const input = view.querySelector('#utl-input');
    if (!go || !input) return;
    go.addEventListener('click', () => dispatchUnified(view));
    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      /* 多行框：Enter 换行、Ctrl/Cmd+Enter 提交；框里没有换行时 Enter 也提交
       *（只贴一条还要求组合键很反常） */
      const plain = !/\n/.test(input.value);
      if (e.ctrlKey || e.metaKey || plain) {
        e.preventDefault();
        dispatchUnified(view);
      }
    });
  }

  function overviewSections(d) {
    const tools = d.tools || {};
    const order = ['douyin', 'kuaishou', 'direct', 'ty', 'ytdlp', 'live'];
    const readyCount = order.filter((k) => tools[k] && tools[k].ready).length;

    let html = '';

    /* —— 统一链接入口（2026-11-07）：全工具箱只此一个「下载」输入口，
     *    自动识别平台并路由；直播输入在直播录制页。放在最前面 ——
     *    它是用户进启动台的第一动作。 */
    html += unifiedSection(d);

    // —— 四个模块就绪情况 ——
    html +=
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">模块状态</h2>' +
        '<span class="badge ' + (readyCount === order.length ? 'badge--ok' : 'badge--warn') + '">' +
        readyCount + ' / ' + order.length + ' 就绪</span></div>' +
        '<div class="lt-rows">' +
        order.map((k) => toolRow(k, tools[k], d)).join('') +
        '</div>' +
      '</section>';

    // —— 环境（复用既有 health-grid 语汇）——
    const disk = d.disk || {};
    const envItems = [
      { ok: !!disk && disk.level !== 'danger', name: '保存磁盘',
        desc: disk.free_gb != null ? ('剩余 ' + disk.free_gb + ' GB / ' + disk.total_gb + ' GB') : '无法读取' },
      { ok: tools.live && tools.live.ready, name: '直播录制',
        desc: (d.running && d.running.live) ? '录制内核在线（录制在直播页直控）' : '录制内核未运行' },
      { ok: true, name: '桥接服务', desc: '已连接 · 只读本机状态' },
    ];
    html +=
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">环境</h2></div>' +
        '<div class="health-grid">' +
        envItems.map((it) =>
          '<div class="health-item">' +
          '<span class="dot ' + (it.ok ? 'dot--ok' : 'dot--amber') + '"></span>' +
          '<div class="health-body"><div class="health-name">' + esc(it.name) + '</div>' +
          '<div class="health-desc">' + esc(it.desc) + '</div></div></div>'
        ).join('') +
        '</div>' +
      '</section>';

    // —— 产物目录 ——
    html +=
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">产物目录</h2>' +
        '<span class="small faint">各模块的落点不同，互不干扰 · 点「更改」可自定义保存到哪</span></div>' +
        '<div class="lt-rows">' + dirRows(d) + '</div>' +
      '</section>';

    // —— 最近任务（统一入口分发后，各通道的任务在这里立即可见） ——
    html += recentSection();

    return html;
  }

  /* 单工具就绪行（总览用）：就绪点 + 名称/说明 + 操作 */
  function toolRow(k, t, d) {
    if (!t) return '';
    const meta = TOOLS.find((x) => x.id === k) || {};
    const dir = (d.dirs || {})[meta.dirKey || k] || {};
    return (
      '<div class="lt-row" data-tool-row="' + esc(k) + '">' +
        '<span class="dot ' + (t.ready ? 'dot--ok' : 'dot--amber') + '"></span>' +
        '<div class="lt-row__main">' +
          '<div class="lt-row__name">' + esc(t.name) + '</div>' +
          '<div class="lt-row__desc">' + esc(t.ready ? (t.sub || t.hint || '') : ('缺少依赖 · ' + (t.hint || ''))) + '</div>' +
        '</div>' +
        '<div class="lt-row__acts">' +
          '<button class="btn btn--ghost btn--sm" data-go="launch/' + esc(k) + '">进入</button>' +
          /* 目录按钮只在目录真的存在时可点 —— 「尚未产生产物」时给一个点了报错的
           * 按钮，等于把失败留给用户去撞。不存在就明说，不给假按钮。 */
          (dir.path
            ? (dir.exists
              ? '<button class="btn btn--ghost btn--sm" data-opendir="' + esc(meta.dirKey || k) + '">' +
                I.icon('openFolder', 14) + '目录</button>'
              : '<span class="small faint">尚未产生产物</span>')
            : '') +
        '</div>' +
      '</div>'
    );
  }

  /* 四个产物目录的说明文案。
   * ⚠️ hint 里必须写清「改了之后什么时候生效 / 有什么副作用」，
   *    尤其是快手那条：台账跟着目录走，换目录会重新下一遍 ——
   *    不写清楚用户会以为「我明明下载过，怎么又下一遍」。 */
  const DIR_META = {
    douyin: {
      label: '抖音产物',
      hint: '抖音下载的视频与图集。改这里等于改 modules/douyin/config.yml 的 path，立即生效。',
      reset: true,
    },
    kuaishou: {
      label: '快手产物',
      hint: '按作者名分目录。⚠️ 下载台账跟着目录走：换到新目录后会当成空目录重新下载一遍，'
          + '旧目录里已有的文件不会自动搬过去（要搬家请自己剪切）。',
      reset: true,
    },
    direct: {
      label: '直链下载',
      hint: 'clawfetch 直链下载的文件。改完下一个任务生效。',
      reset: true,
    },
    ty: {
      label: 'TikTok / YouTube 产物',
      hint: '按平台（tiktok/ youtube）与作者分目录。改完下一个任务生效。',
      reset: true,
    },
    ytdlp: {
      label: 'yt-dlp 万能下载产物',
      hint: '万能站点下载的视频与音频，按作者分目录。改完下一个任务生效。',
      reset: true,
    },
    stripchat: {
      label: 'Stripchat 录像',
      hint: '按「房间名_开始时间」分目录，每个目录含合并后的 .ts 成品；'
          + '正在录制的会话停止录制时自动合并。',
      reset: true,
    },
    live: {
      label: '直播录像',
      hint: '录制器的保存根目录。已录好的文件不会移动，下一次开始录制时落到新位置。',
      reset: false,
    },
  };

  function dirRows(d) {
    const dirs = d.dirs || {};
    const show = Object.keys(DIR_META).filter((k) => dirs[k]);
    if (!show.length) return '<p class="muted">无法读取目录信息。</p>';
    return show.map((k) => dirRow(k, dirs[k])).join('');
  }

  /* 一行产物目录 + 藏在下面的「自定义」编辑条。
   *
   * 编辑条默认 hidden：四个目录一屏四行都展开输入框，会让人以为这是
   * 一个「必须现在填完」的表单，而实际上大多数时候只需要点「打开」。 */
  function dirRow(k, g) {
    const m = DIR_META[k] || { label: k, hint: '', reset: false };
    const custom = g.source === 'custom';
    const acts =
      (g.exists
        ? '<button class="btn btn--ghost btn--sm" data-opendir="' + esc(k) + '">' +
          I.icon('openFolder', 14) + '打开</button>'
        : '<span class="small faint">尚未产生产物</span>') +
      '<button class="btn btn--ghost btn--sm" data-diredit="' + esc(k) + '">更改</button>';
    return (
      '<div class="lt-row">' +
        '<span class="dot ' + (g.exists ? 'dot--ok' : 'dot--amber') + '"></span>' +
        '<div class="lt-row__main">' +
          '<div class="lt-row__name">' + esc(m.label) +
            (custom ? ' <span class="badge badge--ok">已自定义</span>' : '') + '</div>' +
          '<div class="lt-row__path">' + esc(g.path) + '</div>' +
        '</div>' +
        '<div class="lt-row__acts">' + acts + '</div>' +
      '</div>' +
      '<div class="lt-diredit" data-direditbox="' + esc(k) + '" hidden>' +
        '<input class="input" type="text" spellcheck="false" autocomplete="off" ' +
          'data-dirinput="' + esc(k) + '" data-orig="' + esc(g.path) + '" ' +
          'value="' + esc(g.path) + '" aria-label="' + esc(m.label) + '保存位置">' +
        '<button class="btn btn--ghost btn--sm" data-dirpick="' + esc(k) + '">浏览…</button>' +
        '<button class="btn btn--primary btn--sm" data-dirsave="' + esc(k) + '">保存</button>' +
        (custom && m.reset
          ? '<button class="btn btn--ghost btn--sm" data-dirreset="' + esc(k) + '">恢复默认</button>'
          : '') +
        '<button class="btn btn--ghost btn--sm" data-dircancel="' + esc(k) + '">取消</button>' +
        (m.hint ? '<div class="lt-diredit__hint">' + esc(m.hint) + '</div>' : '') +
      '</div>'
    );
  }

  /* ============================================================
   * 单工具视图
   * ============================================================ */
  function toolSections(tool, d, live, k) {
    if (tool.id === 'toolbox') return overviewSections(d);

    let html = '';

    // —— 主操作区：输入口 + 主按钮 ——
    // 每个视图最多一个 primary（按钮层级纪律），故这里只放一个主按钮。
    if (tool.id === 'douyin') html += douyinMain(d);
    else if (tool.id === 'direct') html += directMain(d);
    else if (tool.id === 'kuaishou') html += kuaishouMain(d);
    else if (tool.id === 'ty') html += tyMain(d);
    else if (tool.id === 'ytdlp') html += ytdlpMain(d);
    else if (tool.id === 'live') html += liveMain(d, live, k);

    /* —— 直播录制：这一屏是控制台，顺序按「操作 → 观察 → 排障」排 ——
     * 内核控制条（上面）→ 监控清单（可操作）→ 登录态 → 录像库 → 日志。
     * 把日志放最后：它是排障时才看的东西，不该一进来就占屏。 */
    if (tool.id === 'live') {
      html += liveSettingsSection(live, k);
      html += liveRoomsSection(live, d, k);
      html += liveLoginSection(k);
      /* Stripchat 房间录制：另一条录制线（独立进程监护），与监控内核互不相干。
       * 放在登录态之后、状态区之前 —— 它是操作台，不是观察区。 */
      html += scSection(d);
    }

    // —— 状态区（各工具自己的键值，复用 health-grid 语汇） ——
    // —— yt-dlp 维护区（升级 + 高级配置）放在状态区之前 ——
    if (tool.id === 'ytdlp') html += ytdlpMaintainSection();
    html += statusSection(tool, d, live, k);

    // —— 录像库（直播录制的产物，按平台聚合占用） ——
    if (tool.id === 'live') html += liveRecordingsSection(live, k);

    // —— 实时日志（直播录制专属） ——
    if (tool.id === 'live') html += liveLogSection(k);

    // —— 自检（只有直播录制有） ——
    if (tool.hasSelftest) html += selftestSection();

    // —— 最近任务（抖音 / 快手 / 直链共用统一任务队列） ——
    if (tool.kind && tool.kind !== 'live') html += recentSection();

    return html;
  }

  /* —— 抖音：收链接 → 后台任务队列 —— */
  function douyinMain(d) {
    const ready = d.tools && d.tools.douyin && d.tools.douyin.ready;
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">开始下载</h2>' +
        '<span class="small faint">支持分享口令 / 多行长文本，会自动挑出链接</span></div>' +
        '<textarea class="input" id="lt-url" rows="4" spellcheck="false" ' +
          'placeholder="粘贴抖音链接或分享口令，可一次多个（用空格或换行分隔）"' +
          (ready ? '' : ' disabled') + ' aria-label="抖音链接"></textarea>' +
        '<div class="flex mt-3" style="align-items:center">' +
          '<button class="btn btn--primary" id="lt-go"' + (ready ? '' : ' disabled') + '>' +
            I.icon('download', 15) + '开始下载</button>' +
          '<span class="small faint" id="lt-hint" aria-live="polite">' +
            (ready ? '默认下载作品本体，画质最高自动；需要改模式请到「下载 · 抖音下载」页。' : '模块未就绪，暂时无法下载。') +
          '</span>' +
        '</div>' +
      '</section>'
    );
  }

  /* —— 直链：收链接 → clawfetch 后台下载 —— */
  function directMain(d) {
    const ready = d.tools && d.tools.direct && d.tools.direct.ready;
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">开始下载</h2>' +
        '<span class="small faint">直链与分享中转页都可以，可一次多个</span></div>' +
        /* 多行框，不是单行 input：多链接入口必须**看得见**。 */
        '<textarea class="input" id="lt-url" rows="3" spellcheck="false" ' +
          'placeholder="https://… 直链或中转页地址。要一次下多个就每行一个（也可用空格分隔），' +
          '会作为同一个批量任务提交（同一域名顺序下、不同域名并行）"' +
          (ready ? '' : ' disabled') + ' aria-label="下载链接"></textarea>' +
        '<div class="flex flex-wrap mt-3" style="align-items:center">' +
          '<button class="btn btn--primary" id="lt-go"' + (ready ? '' : ' disabled') + '>' +
            I.icon('download', 15) + '下载</button>' +
          '<label class="small" style="display:flex;align-items:center;gap:6px;color:var(--ink-2)">' +
            '<input type="checkbox" id="lt-resolve"> 只解析不下载' +
          '</label>' +
          /* 弱源站开关：临时隧道/自建小站只喂得动很少几路连接，auto 自适应
           * 要先开 8 个、撞出一串 502 才会往下退。勾上就直接单连接开跑。 */
          '<label class="small" style="display:flex;align-items:center;gap:6px;color:var(--ink-2)" ' +
            'title="源站很慢、或一直报 502/超时的时候勾上。直接从单连接开始，避免多连接把源站打哑。">' +
            '<input type="checkbox" id="lt-single"> 兼容弱源站（单连接）' +
          '</label>' +
          '<span class="small faint" id="lt-hint" aria-live="polite">' +
            (ready
              ? ('产物保存到 ' + (((d.dirs || {}).direct || {}).path || 'downloads/direct')
                 + '，支持断点续传与 SHA-256 校验（可在启动台「产物目录」里改）。')
              : '模块未就绪，暂时无法下载。') +
          '</span>' +
        '</div>' +
      '</section>'
    );
  }

  /* —— 快手：已登录后台下载 / 未登录引导 —— */
  function kuaishouMain(d) {
    const ready = d.tools && d.tools.kuaishou && d.tools.kuaishou.ready;
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">开始下载</h2>' +
        '<span class="small faint">粘贴快手分享链接，解析作者主页并批量下载</span></div>' +
        '<div class="input-group">' +
          '<input class="input" id="lt-url" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="https://v.kuaishou.com/… 分享链接"' +
            (ready ? '' : ' disabled') + ' aria-label="快手链接">' +
          '<button class="btn btn--primary" id="lt-go"' + (ready ? '' : ' disabled') + '>' +
            I.icon('download', 15) + '下载</button>' +
        '</div>' +
        '<div class="flex mt-3" style="align-items:center">' +
          '<span class="small faint" id="lt-hint" aria-live="polite">' +
            (ready ? '未登录时会提示扫码；登录态保存在本机，只需扫一次。' : '模块未就绪，暂时无法下载。') +
          '</span>' +
        '</div>' +
      '</section>'
    );
  }

  /* —— TikTok / YouTube：粘链接即下 —— */
  function tyMain(d) {
    const ready = d.tools && d.tools.ty && d.tools.ty.ready;
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">开始下载</h2>' +
        '<span class="small faint">粘贴 TikTok / YouTube 视频链接；频道/主页链接会列出全部作品</span></div>' +
        '<div class="input-group">' +
          '<input class="input" id="lt-url" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="https://www.youtube.com/watch?v=… 或 https://www.tiktok.com/@用户/video/…"' +
            (ready ? '' : ' disabled') + ' aria-label="TikTok/YouTube 链接">' +
          '<button class="btn btn--primary" id="lt-go"' + (ready ? '' : ' disabled') + '>' +
            I.icon('download', 15) + '下载</button>' +
        '</div>' +
        '<div class="flex mt-3" style="align-items:center">' +
          '<span class="small faint" id="lt-hint" aria-live="polite">' +
            (ready ? '境内网络自动走本地代理（Clash 等）；探测不到时会提示先打开代理软件。'
                   : '模块未就绪（缺 kuaishou-env 运行时或 ty 模块）。') +
          '</span>' +
        '</div>' +
      '</section>'
    );
  }

  /* —— yt-dlp 万能下载：全站通吃 + 后处理选项 ——
   *
   * 与 ty 的差别：ty 只吃 TikTok/YouTube（且必须代理），这里吃 yt-dlp
   * 全部其它站点。选项面板只放高频项（画质/音频/字幕/Cookie/存档），
   * 长尾参数走下方「高级配置」（yt-dlp 原生 conf 语法）。 */
  function ytdlpMain(d) {
    const ready = d.tools && d.tools.ytdlp && d.tools.ytdlp.ready;
    const dis = ready ? '' : ' disabled';
    const sel = (id, label, opts, cur) =>
      '<label class="small faint">' + label +
      '<select class="input" id="' + id + '"' + dis + ' style="max-width:150px">' +
      opts.map((o) => '<option value="' + esc(o[0]) + '"' + (o[0] === cur ? ' selected' : '') + '>' +
        esc(o[1]) + '</option>').join('') +
      '</select></label>';
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">开始下载</h2>' +
        '<span class="small faint">粘贴视频/音频页链接（Twitter/X · Instagram · 微博 · Vimeo · Twitch 回放…），播放列表/频道链接整单下载</span></div>' +
        '<div class="input-group">' +
          '<input class="input" id="lt-url" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="https://x.com/用户/status/… · https://weibo.com/… · https://vimeo.com/…"' + dis +
            ' aria-label="万能下载链接">' +
          '<button class="btn" id="yd-preview"' + dis + '>' + I.icon('link', 15) + '预览</button>' +
          '<button class="btn btn--primary" id="lt-go"' + dis + '>' +
            I.icon('download', 15) + '下载</button>' +
        '</div>' +
        '<div class="flex mt-2" style="gap:14px;flex-wrap:wrap;align-items:flex-end">' +
          sel('yd-mode', '列表', [['all', '整单下载'], ['1', '仅单条']], 'all') +
          sel('yd-quality', '画质上限', [['best', '最佳'], ['2160', '≤4K'], ['1080', '≤1080p'], ['720', '≤720p'], ['480', '≤480p']], 'best') +
          sel('yd-audio', '音频提取', [['none', '不提取'], ['mp3', 'MP3'], ['m4a', 'M4A'], ['flac', 'FLAC']], 'none') +
          sel('yd-subs', '字幕', [['none', '不下载'], ['write', '外挂 srt'], ['embed', '内嵌']], 'none') +
          sel('yd-cookies', 'Cookie', [['none', '不用'], ['edge', 'Edge'], ['chrome', 'Chrome'], ['firefox', 'Firefox']], 'none') +
          '<label class="small faint" style="display:flex;align-items:center;gap:6px">' +
            '<input type="checkbox" id="yd-archive"' + dis + '> 存档去重</label>' +
          '<label class="small faint" style="display:flex;align-items:center;gap:6px">' +
            '<input type="checkbox" id="yd-sb"' + dis + '> SponsorBlock</label>' +
          '<input class="input" id="yd-langs" type="text" spellcheck="false"' + dis +
            ' value="zh-CN,zh-Hans,en" style="max-width:170px" aria-label="字幕语言" title="字幕语言，逗号分隔">' +
        '</div>' +
        '<div class="flex mt-3" style="align-items:center">' +
          '<span class="small faint" id="lt-hint" aria-live="polite">' +
            (ready ? '境内站自动直连；Twitter/Instagram 等境外站自动探测本地代理（Clash 等），探测不到会提示先开代理。'
                   : '模块未就绪（缺 kuaishou-env 运行时或 ytdlp 模块）。') +
          '</span>' +
        '</div>' +
        '<div id="yd-preview-box" class="mt-2"></div>' +
      '</section>'
    );
  }

  /* —— yt-dlp 维护区：一键升级 + 高级配置（长尾参数走 yt-dlp 原生 conf） —— */
  function ytdlpMaintainSection() {
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">维护与高级配置</h2>' +
        '<span class="small faint">配置为 yt-dlp 原生语法（--config-location），保存后下一个任务生效；每次保存自动留 .bak</span></div>' +
        '<div class="flex mb-2">' +
          '<button class="btn btn--ghost btn--sm" id="yd-update">' +
            I.icon('refresh', 14) + '升级 yt-dlp</button>' +
          '<button class="btn btn--ghost btn--sm" id="yd-conf-reload">' +
            I.icon('openFolder', 14) + '重读配置</button>' +
          '<button class="btn btn--primary btn--sm" id="yd-conf-save">' +
            I.icon('download', 14) + '保存配置</button>' +
        '</div>' +
        '<textarea class="input" id="yd-conf" rows="5" spellcheck="false" ' +
          'placeholder="# 例：--cookies-from-browser edge' + String.fromCharCode(10) +
          '# 例：--netrc' + String.fromCharCode(10) +
          '# 例：-f bv*[height<=1080]+ba/b" aria-label="yt-dlp 高级配置"></textarea>' +
        '<div id="yd-maintain-out" class="mt-2"></div>' +
      '</section>'
    );
  }

  function renderYtdlpPreview(view, p) {
    const box = view.querySelector('#yd-preview-box');
    if (!box) return;
    if (!p || p.ok === false) {
      box.innerHTML = '<div class="error-box" style="padding:8px 10px">' +
        esc((p && p.error) || '预览失败') + '</div>';
      return;
    }
    const rows = [];
    rows.push('<div class="lt-row__name">' + esc(p.title || '(无标题)') + '</div>');
    if (p.uploader) rows.push('<div class="small faint">作者：' + esc(p.uploader) + '</div>');
    if (p.duration) {
      const s = Math.round(p.duration);
      rows.push('<div class="small faint">时长：' + Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒</div>');
    }
    if (p.type === 'playlist') {
      rows.push('<div class="small faint">类型：播放列表，共 ' + esc(String(p.count)) + ' 个条目</div>');
      if (p.entries_preview && p.entries_preview.length) {
        rows.push('<div class="lr-log mt-2" style="max-height:160px">' +
          p.entries_preview.map((t) => '<div>' + esc(t) + '</div>').join('') + '</div>');
      }
    } else if (p.formats && p.formats.length) {
      const tops = p.formats.filter((f) => f.h).slice(-6).reverse();
      if (tops.length) {
        rows.push('<div class="small faint mt-2">可选画质：' +
          tops.map((f) => esc(f.h + 'p' + (f.fps ? Math.round(f.fps) : '') + (f.ext ? ' · ' + f.ext : ''))).join('、') +
          '</div>');
      }
    }
    box.innerHTML = '<div class="lt-rows" style="border:1px solid var(--line);padding:10px">' +
      '<div class="lt-row"><div class="lt-row__main">' + rows.join('') + '</div></div></div>';
  }

  /* 读取万能下载页的选项（提交下载与预览共用） */
  function collectYtdlpOpts(view) {
    const g = (id) => { const el = view.querySelector('#' + id); return el ? el.value : null; };
    const gc = (id) => { const el = view.querySelector('#' + id); return !!(el && el.checked); };
    return {
      mode: g('yd-mode') || 'all',
      quality: g('yd-quality') || 'best',
      audio_format: g('yd-audio') || 'none',
      subs: g('yd-subs') || 'none',
      subs_langs: g('yd-langs') || 'zh-CN,zh-Hans,en',
      cookies_browser: g('yd-cookies') || 'none',
      archive: gc('yd-archive'),
      sponsorblock: gc('yd-sb'),
    };
  }

  /* —— Stripchat 房间录制：直播页内的第二条录制线 ——
   *
   * 与上面的监控内核不同：这里一次只录一个房间，由桥接直接监护一个录制进程
   * （mouflon 解密 + 4 路 CDN 竞速都在那个进程里）。两台「机器」互不相干：
   * 停 Stripchat 不影响快手/抖音监控，反之亦然。
   * 状态徽标与日志区由 scPollOnce 每 3 秒刷新（load() 里随 live 页一起起轮询）。 */
  function scSection(d) {
    const ready = d.tools && d.tools.stripchat && d.tools.stripchat.ready;
    return (
      '<section class="section section--flush">' +
        '<div class="section__head"><h2 class="card__title">Stripchat 房间录制</h2>' +
        '<span class="small faint">18+ · 含 VR 与白标镜像 · 一次一个房间 · 停止时自动合并</span></div>' +
        '<div class="input-group">' +
          '<input class="input" id="sc-url" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="https://stripchat.com/房间名  ·  VR: https://vr.stripchat.com/cam/房间名"' +
            (ready ? '' : ' disabled') + ' aria-label="Stripchat 房间链接">' +
          '<button class="btn btn--primary" id="sc-go"' + (ready ? '' : ' disabled') + '>' +
            I.icon('play', 15) + '录制</button>' +
          '<button class="btn" id="sc-stop" disabled>' + I.icon('stop', 15) + '停止</button>' +
        '</div>' +
        '<div class="flex mt-3" style="align-items:center">' +
          '<span id="sc-badge">' + C.badge('未在录制', '') + '</span>' +
          '<span class="small faint" id="sc-hint" aria-live="polite">' +
            (ready ? '支持 stripchat / vr.stripchat / xhamsterlive / stripol / camwhorestv；'
                 + '录制进程在后台运行，关掉本窗口不影响。'
                 : '模块未就绪（modules/stripchat/stripchat_recorder.py 缺失）。') +
          '</span>' +
          '<button class="btn btn--ghost btn--sm" id="sc-open">' + I.icon('openFolder', 14) + '打开录像目录</button>' +
        '</div>' +
        '<div class="lr-log mt-2" id="sc-log" role="log" aria-live="polite" style="max-height:220px">' +
          '<div class="lr-log__empty">暂无日志；开始录制后这里会实时滚动（最近 120 行）。</div></div>' +
      '</section>'
    );
  }

  /* —— 直播流录制工具：录制控制台 ——
   *
   * 这一屏是「把录制窗口搬进网页」后的主界面：网页直接驱动无界面录制内核
   * （daemon.py，由桥接的 /api/bridge/live/daemon/* 代理）。
   * 2026-11-07 起 PyQt 桌面窗口退役：它的全部功能（设置编辑、导出日志、
   * 复制原始输入等）已并入本页，「打开桌面窗口」入口随之删除 ——
   * 直播流录制只有一个页面（本页），不再有第二个窗口。
   *
   * 状态来源分两层，务必分清：
   *   · `d`    = /api/bridge/launcher（模块就绪与否）
   *   · `live` = /api/bridge/live（旧的只读总览，config.json 原始事实）
   *   · `k`    = /api/bridge/live/daemon/status（内核实时状态，本页的主体） */
  function liveMain(d, live, k) {
    const ready = d.tools && d.tools.live && d.tools.live.ready;
    const online = !!(k && k.daemon !== false && k.ok);
    const running = !!(k && k.running);
    const counts = (k && k.counts) || {};

    /* 内核离线时本页不该装作能操作：如实说明 + 给一个「启动录制内核」。
     * 自动拉起由 load() 负责（进页面就试一次），这里是兜底的手动入口。 */
    const badge = online
      ? (running
          ? C.badge('监控中', 'ok')
          : C.badge('内核在线 · 未监控', 'warn'))
      : C.badge('内核未运行', '');

    return (
      '<section class="section section--flush">' +
        '<div class="section__head">' +
          '<h2 class="card__title">录制内核</h2>' + badge +
        '</div>' +

        '<div class="lt-rows">' +
          '<div class="lt-row">' +
            '<div class="lt-row__main">' +
              '<div class="lt-row__name">网页直控 · 无界面内核</div>' +
              '<div class="lt-row__desc">' +
                (online
                  ? (running
                      ? '正在按监控清单轮询开播状态。下方可随时停录、增删房间、查看实时日志。'
                      : '内核已就绪但尚未开始监控。点「开始监控」后即开始轮询。')
                  : '内核尚未启动。点「启动录制内核」即可，之后本页所有录制操作都可用。' +
                    '（模块未就绪时按钮不可用：' + (ready ? '就绪' : '缺少依赖') + '）') +
              '</div>' +
            '</div>' +
            '<div class="lt-row__acts">' +
              (online
                ? (running
                    ? '<button class="btn btn--sm" id="lt-kstop">' + I.icon('stop', 14) + '停止监控</button>' +
                      '<button class="btn btn--ghost btn--sm" id="lt-kcheck">' + I.icon('refresh', 14) + '立即检查</button>'
                    : '<button class="btn btn--primary btn--sm" id="lt-kstart">' + I.icon('play', 14) + '开始监控</button>')
                : '<button class="btn btn--primary btn--sm" id="lt-kensure"' + (ready ? '' : ' disabled') + '>' +
                  I.icon('power', 14) + '启动录制内核</button>') +
            '</div>' +
          '</div>' +
        '</div>' +

        /* 概览计数：只有内核在线时才有意义，离线就不摆一排 0 假装统计 */
        (online
          ? '<div class="lr-stats">' +
              lrStat('监控对象', (counts.total != null ? counts.total : 0), '个') +
              lrStat('启用', (counts.enabled != null ? counts.enabled : 0), '个') +
              lrStat('正在录制', (counts.recording != null ? counts.recording : 0), '个', counts.recording > 0) +
              lrStat('开播中', (counts.live != null ? counts.live : 0), '个') +
              lrStat('磁盘剩余', U.bytes((counts.free_gb || 0) * 1024 * 1024 * 1024), '') +
            '</div>'
          : '') +

        /* 磁盘暂停是「看着在录、其实没录」的高危状态，必须显眼 */
        (online && k && k.disk_paused
          ? '<div class="error-box" style="margin-top:var(--sp-4)">' +
            '<h3>磁盘空间不足，录制已暂停</h3>' +
            '<p class="muted">剩余空间低于阈值，内核已停止写入新分片以免写满磁盘。' +
            '清理空间后会自动恢复。</p></div>'
          : '') +

        '<div class="card__title" style="margin:var(--sp-5) 0 var(--sp-2)">添加监控房间</div>' +
        '<div class="input-group">' +
          '<input class="input" id="lt-room" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="https://live.douyin.com/… 或 https://live.kuaishou.com/u/…（平台自动识别）" ' +
            'aria-label="直播链接"' + (online ? '' : ' disabled') + '>' +
          '<button class="btn" id="lt-addroom"' + (online ? '' : ' disabled') + '>' +
            I.icon('plus', 15) + '加入监控</button>' +
        '</div>' +
        '<div class="small faint mt-2" id="lt-hint" aria-live="polite">' +
          (online
            ? '开播后自动录制，下播自动停止并合并分片。新加的房间立刻生效，无需重启。'
            : '内核未运行，暂时不能改动监控清单。先点上方「启动录制内核」。') +
        '</div>' +

        /* 停止内核（收尾合并分片，可能耗时）。桌面窗口入口已随合并删除：
         * 本页就是唯一的直播流录制界面。 */
        '<div class="flex mt-3" style="align-items:center;gap:var(--sp-3);flex-wrap:wrap">' +
          '<button class="btn btn--ghost btn--sm" id="lt-koff"' + (online ? '' : ' disabled') + '>' +
            I.icon('power', 14) + '停止录制内核</button>' +
          '<span class="small faint">设置改动即时落盘；改保存路径后新录像写进新目录。</span>' +
        '</div>' +
      '</section>'
    );
  }

  /* —— 概览计数格子 —— */
  function lrStat(label, value, unit, hot) {
    return (
      '<div class="lr-stat' + (hot ? ' lr-stat--hot' : '') + '">' +
        '<div class="lr-stat__num">' + esc(String(value)) +
          '<span class="lr-stat__unit">' + esc(unit) + '</span></div>' +
        '<div class="lr-stat__label">' + esc(label) + '</div>' +
      '</div>'
    );
  }


  /* ============================================================
   * 直播流录制工具专属区块
   * ------------------------------------------------------------
   * 数据来自 /api/bridge/live（q_live）。它只读、零副作用：
   * 监控清单来自 config.json 的 rooms，登录态只报布尔（不泄 Cookie 内容），
   * 录像占用按平台聚合。任何一项拿不到都不编造，如实说「读不到」。
   * ============================================================ */

  const LR_PLATFORM_LABEL = { kuaishou: '快手', douyin: '抖音' };

  /* 内核状态 → 徽章色调。键取自 MonitorEngine.snapshot() 的 status 字段。
   * 未列出的状态不编色调（返回空串），由调用方如实显示原始文案 ——
   * 给不认识的状态随便涂个色，等于在界面上假装懂了它。 */
  const LR_STATUS_TONE = {
    recording: 'ok', live: 'ok', merging: 'warn',
    reconnecting: 'warn', error: 'err', paused: 'warn',
  };

  /* —— 监控清单：内核在线时是可操作的控制表，离线时退回只读总览 ——
   *
   * 两个数据源能力不同，必须分清：
   *   · 内核在线 → `k.rooms`（snapshot() 实时行：状态/时长/体积/分段数/错误）
   *   · 内核离线 → `live.rooms`（config.json 里的原始条目，只有 enabled/备注）
   * 离线时**不要**假装有实时进度，把「不可操作」说清楚即可。 */
  function liveRoomsSection(live, d, k) {
    const online = !!(k && k.daemon !== false && k.ok);
    const rows = online ? (k.rooms || []) : ((live && live.rooms) || []);

    if (!online && !live) {
      return (
        '<section class="section">' +
          '<div class="section__head"><h2 class="card__title">监控清单</h2></div>' +
          '<p class="muted">读取不到监控清单（录制模块状态接口未返回数据）。</p>' +
        '</section>'
      );
    }

    const enabled = rows.filter((r) => r.enabled).length;

    let body;
    if (!rows.length) {
      body =
        '<div class="empty">' +
          '<div class="empty__icon">' + I.icon('radio', 30) + '</div>' +
          '<div class="empty__title">还没有监控对象</div>' +
          '<div class="empty__desc">在上方粘贴直播间链接加入监控清单，开播后会自动开始录制。</div>' +
        '</div>';
    } else if (online) {
      body = liveRoomTable(rows);
    } else {
      /* 离线只读表：只有 config.json 能确定的事实 */
      body =
        '<div class="lr-table-wrap"><table class="table"><thead><tr>' +
        '<th>平台</th><th>房间标识</th><th>主播昵称</th><th>备注</th><th>启用</th>' +
        '</tr></thead><tbody>' +
        rows.map((r) =>
          '<tr>' +
            '<td>' + C.badge(LR_PLATFORM_LABEL[r.platform] || r.platform || '—',
                             r.platform === 'douyin' ? 'pink' : (r.platform === 'kuaishou' ? 'cyan' : '')) + '</td>' +
            '<td><code class="mono small">' + esc(r.room_id || r.raw_input || '—') + '</code></td>' +
            '<td>' + esc(r.nickname || '—') + '</td>' +
            '<td>' + esc(r.remark || '—') + '</td>' +
            '<td>' + (r.enabled ? C.badge('是', 'ok') : C.badge('否', '')) + '</td>' +
          '</tr>'
        ).join('') +
        '</tbody></table></div>';
    }

    return (
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">监控清单</h2>' +
        '<span class="num small faint">' + rows.length + ' 个对象' +
        (rows.length ? ' · ' + enabled + ' 个启用' : '') + '</span></div>' +
        body +
        (online
          ? '<p class="small faint" style="margin:12px 0 0">' +
            '房间号是稳定标识，昵称可能被主播随时改；两者都会随录像文件名一起记下来。' +
            '「停录」只停下当前这一场，房间仍在监控清单里，下次开播会继续录。</p>'
          : '<p class="small faint" style="margin:12px 0 0">' +
            '清单存在录制器的 config.json 里。启动录制内核后，这里会变成可操作的控制台。' +
            '加入监控只写清单、不弹窗。</p>') +
      '</section>'
    );
  }

  /* —— 实时房间表：状态 / 时长 / 体积 / 分段 + 单房间操作 ——
   *
   * 用表格而不是卡片列表：这里是「一眼扫过所有房间现在怎么样」的场景，
   * 对齐的列比一叠卡片刻度更好读（与 V2「去卡片化」的判断一致）。 */
  function liveRoomTable(rows) {
    return (
      '<div class="lr-table-wrap"><table class="table"><thead><tr>' +
      '<th>平台</th><th>房间 / 主播</th><th>状态</th><th>标题</th>' +
      '<th class="num">已录</th><th class="num">体积</th><th class="num">分段</th><th></th>' +
      '</tr></thead><tbody>' +
      rows.map((r) => {
        const recording = r.status === 'recording' || r.status === 'reconnecting'
                          || r.status === 'merging';
        /* 主播名与房间号是两个独立事实：昵称可能为空（尚未解析出），
         * 房间号可能为空（分享短链还没解析）。都不要用 '—' 以外的占位去编。 */
        const name = r.nickname || '未知主播';
        const room = r.room_id || '';
        return (
          /* data-key 是就地更新的锚点：轮询只改这一行的可变列，不重绘结构 */
          '<tr data-key="' + esc(r.key) + '">' +
            '<td>' + C.badge(LR_PLATFORM_LABEL[r.platform] || r.platform || '—',
                             r.platform === 'douyin' ? 'pink' : (r.platform === 'kuaishou' ? 'cyan' : '')) + '</td>' +
            '<td>' +
              '<div>' + esc(name) + '</div>' +
              (room ? '<div class="mono small faint">' + esc(room) + '</div>'
                    : '<div class="small faint">房间号待解析</div>') +
            '</td>' +
            '<td data-cell="status">' + roomStatusCell(r) + '</td>' +
            '<td class="cell-clip">' + esc(r.title || '—') + '</td>' +
            '<td class="num mono">' + (recording ? esc(U.duration(r.recorded_seconds || 0)) : '—') + '</td>' +
            '<td class="num mono">' + (recording ? esc(U.bytes(r.total_bytes || 0)) : '—') + '</td>' +
            '<td class="num mono">' + (recording ? esc(String(r.segment_count || 0)) : '—') + '</td>' +
            '<td>' +
              '<div class="lr-row-acts">' +
                (recording
                  ? '<button class="btn btn--ghost btn--xs" data-room-stop="' + esc(r.key) + '">停录</button>'
                  : '') +
                '<button class="btn btn--ghost btn--xs" data-room-toggle="' + esc(r.key) + '" ' +
                  'data-next="' + (r.enabled ? '0' : '1') + '">' +
                  (r.enabled ? '停用' : '启用') + '</button>' +
                /* 桌面窗口右键菜单里的「复制原始输入」搬进来：粘错链接想核对
                 * 原文时，不用再跑去翻 config.json。 */
                '<button class="btn btn--ghost btn--xs" data-room-copy="' + esc(r.key) + '" ' +
                  'data-copy="' + esc(r.raw_input || r.room_id || '') + '">复制链接</button>' +
                /* 该房间的录制目标文件夹：output_dir 由内核快照给出（正在录时是
                 * 本场输出目录，还没录过则回落到录像根目录），直接交给
                 * /live/open-dir 打开 —— 后端会校验目录必须落在录像根目录内。 */
                (r.output_dir
                  ? '<button class="btn btn--ghost btn--xs" data-room-dir="' + esc(r.output_dir) + '" ' +
                    'title="打开录制文件夹：' + esc(r.output_dir) + '">打开文件夹</button>'
                  : '') +
                '<button class="btn btn--ghost btn--xs btn--danger" data-room-del="' + esc(r.key) + '" ' +
                  'data-room-name="' + esc(name) + '">删除</button>' +
              '</div>' +
              (r.last_error
                ? '<div class="small" style="color:var(--danger)">' + esc(r.last_error) + '</div>'
                : '') +
            '</td>' +
          '</tr>'
        );
      }).join('') +
      '</tbody></table></div>'
    );
  }

  /* 状态单元格（初始渲染与轮询就都走这一个函数，保证两处永远一致） */
  function roomStatusCell(r) {
    const tone = LR_STATUS_TONE[r.status] || '';
    const label = r.status_label || r.status || '—';
    return (tone ? C.badge(label, tone) : esc(label)) +
           (r.enabled ? '' : ' <span class="small faint">已停用</span>');
  }


  /* —— 录制设置：桌面窗口「设置」对话框的网页版 ——
   *
   * 2026-11-07 桌面窗口退役，其设置对话框的全部可写项搬进本页。
   * 提交走 daemon 的 /settings 白名单（daemon._SETTING_CASTS）：
   * 数值范围以白名单为准，前端不重复校验 —— 越界值由后端如实拒绝，
   * 前端把 applied / rejected 如实展示，不粉饰。
   * 常用项平铺；重连/检测这类低频项收进 <details>（原生折叠，零样式依赖）。
   * 内核离线时只读展示 config.json 那份值并说明原因 —— 不假装能改。 */
  function lrField(label, inner, hint) {
    return (
      '<div class="form-item">' +
        '<div class="form-item__label">' + esc(label) + '</div>' + inner +
        (hint ? '<div class="small faint" style="margin-top:2px">' + esc(hint) + '</div>' : '') +
      '</div>'
    );
  }

  function lrNumField(key, s, def, label, hint, step) {
    const v = (s && s[key] !== undefined && s[key] !== null) ? s[key] : def;
    return lrField(label,
      '<input class="input" type="number" data-lrset="' + esc(key) + '" value="' +
      esc(String(v)) + '"' + (step ? ' step="' + esc(String(step)) + '"' : '') + '>', hint);
  }

  function lrCheckField(key, s, label, hint) {
    const on = !!(s && s[key]);
    return (
      '<label class="sto-opt" style="display:flex;align-items:flex-start;gap:6px;margin:0">' +
        '<input type="checkbox" data-lrset="' + esc(key) + '"' + (on ? ' checked' : '') + '>' +
        '<span>' + esc(label) +
          (hint ? ' <span class="small faint">' + esc(hint) + '</span>' : '') + '</span>' +
      '</label>'
    );
  }

  function liveSettingsSection(live, k) {
    const online = !!(k && k.daemon !== false && k.ok);
    const s = (online && k && k.settings) || (live && live.settings) || {};

    const grid = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));' +
                 'gap:var(--sp-3) var(--sp-5);align-items:start">';

    const common =
      lrField('保存路径',
        '<input class="input" type="text" data-lrset="save_path" spellcheck="false" ' +
        'value="' + esc(String(s.save_path || '')) + '">',
        '录像根目录；后端保存前会先验证目录可写') +
      lrNumField('poll_interval', s, 60, '轮询间隔（秒）',
        '检测开播状态的间隔，过短易触发风控；监控多时建议 120 秒以上') +
      lrField('画质偏好',
        '<select class="input" data-lrset="quality_preference">' +
          '<option value="origin"' + (s.quality_preference === 'high' ? '' : ' selected') + '>原画优先（1080P）</option>' +
          '<option value="high"' + (s.quality_preference === 'high' ? ' selected' : '') + '>超清优先（720P）</option>' +
        '</select>') +
      lrNumField('max_file_size_gb', s, 2, '单分片大小上限（GB）',
        '单个分片达到该体积后自动分卷', 0.5) +
      lrNumField('max_concurrent_recordings', s, 3, '同时录制上限（个）') +
      lrNumField('min_free_disk_gb', s, 5, '磁盘剩余下限（GB）',
        '低于该值内核暂停录制，清理后自动恢复') +
      '<div>' +
        lrCheckField('enable_fallback', s, '原画失败时自动降级录 720P') +
        '<div style="margin-top:6px">' + lrCheckField('convert_to_mp4', s, '完成后转封装 MP4（-c copy 不重编码）') + '</div>' +
        '<div style="margin-top:6px">' + lrCheckField('keep_original_after_mp4', s, '同时保留原始 FLV') + '</div>' +
      '</div>';

    const advanced =
      lrNumField('check_concurrency', s, 4, '并发检测数',
        '同时进行的检测请求数，建议 4~8，过高易触发风控') +
      lrNumField('max_monitor_rooms', s, 200, '监控对象上限（个）') +
      lrNumField('max_duration_minutes', s, 0, '单分片时长上限（分钟）',
        '0 表示不限时长，只按体积分卷') +
      lrNumField('reconnect_interval', s, 15, '重连间隔（秒）',
        '拉流中断后等多久重连；重连写入新分片，已录内容不丢') +
      lrNumField('stall_timeout', s, 60, '卡顿判定阈值（秒）',
        '输出文件连续多久没增长即判定卡顿并重连') +
      lrNumField('max_reconnect_attempts', s, 0, '最大重连次数',
        '连续失败多少次后放弃该场直播，0 表示无限重连') +
      lrField('ffmpeg 路径',
        '<input class="input" type="text" data-lrset="ffmpeg_path" spellcheck="false" ' +
        'value="' + esc(String(s.ffmpeg_path || 'ffmpeg')) + '">',
        '留空或填 ffmpeg 表示用 PATH 里的') +
      '<div>' +
        lrCheckField('playwright_enabled', s, '允许 Playwright（扫码登录需要）') +
        '<div style="margin-top:6px">' + lrCheckField('headless_browser', s, '登录浏览器无头模式') + '</div>' +
      '</div>';

    return (
      '<section class="section" id="lt-settings">' +
        '<div class="section__head"><h2 class="card__title">录制设置</h2>' +
          '<span class="small faint">' +
            (online
              ? '改动保存后立即落盘 config.json；并发/上限类改动重启内核后完全生效'
              : '内核未运行，暂不能修改。启动录制内核后这里可编辑') +
          '</span></div>' +
        (online
          ? '<div>' + common + '</div>' +
            '<details style="margin-top:var(--sp-4)">' +
              '<summary class="small" style="cursor:pointer;color:var(--ink-2)">高级（检测 / 重连 / 登录浏览器）</summary>' +
              '<div style="margin-top:var(--sp-3)">' + advanced + '</div>' +
            '</details>' +
            '<div class="flex mt-3" style="align-items:center">' +
              '<button class="btn btn--primary btn--sm" id="lt-settings-save">' +
                I.icon('check', 14) + '保存设置</button>' +
              '<span class="small faint" id="lt-settings-hint" aria-live="polite"></span>' +
            '</div>'
          : '<p class="muted">内核未运行。这里显示的是 config.json 里保存的值；' +
            '启动录制内核后即可在此修改（原桌面窗口的全部设置项都已并入本页）。</p>') +
      '</section>'
    );
  }

  function bindLiveSettings(view) {
    const save = view.querySelector('#lt-settings-save');
    if (!save) return;
    save.addEventListener('click', async () => {
      const box = view.querySelector('#lt-settings');
      if (!box) return;
      const patch = {};
      box.querySelectorAll('[data-lrset]').forEach((el) => {
        const key = el.dataset.lrset;
        if (el.type === 'checkbox') patch[key] = el.checked;
        else if (el.type === 'number') {
          const n = parseFloat(el.value);
          if (!isNaN(n)) patch[key] = n;
        } else patch[key] = el.value.trim();
      });
      save.disabled = true;
      try {
        const r = await window.DYApi.liveKernelSettings(patch);
        const applied = r.applied || {};
        const rejected = r.rejected || {};
        const appliedKeys = Object.keys(applied);
        if (appliedKeys.length) {
          C.toast('已保存 ' + appliedKeys.length + ' 项设置', 'ok');
        }
        const rejKeys = Object.keys(rejected);
        const hint = view.querySelector('#lt-settings-hint');
        if (rejKeys.length) {
          const msg = rejKeys.map((key) => key + '：' + rejected[key]).join('；');
          C.toast('有 ' + rejKeys.length + ' 项未生效：' + msg, 'warn');
          if (hint) hint.textContent = '未生效：' + msg;
        } else if (hint) {
          hint.textContent = appliedKeys.length
            ? '已保存：' + appliedKeys.join('、')
            : '没有需要保存的改动';
        }
        /* 回显盘上真正生效的值（后端可能把越界值收敛了）——用 effective
         * 而不是用户输入的值，否则界面会自信地显示一个没生效的数字。 */
        await load(view);
      } catch (e) {
        C.toast(e.message || '保存失败', e.offline ? 'warn' : 'err');
      } finally {
        if (save.isConnected) save.disabled = false;
      }
    });
  }

  /* —— 录像库：按平台聚合的占用分布 —— */
  function liveRecordingsSection(live, k) {
    if (!live) return '';
    const rec = live.recordings || {};
    const groups = rec.groups || [];
    const total = rec.total || 0;
    /* 录像目录优先取内核的实时设置：config.json 刚被网页改过时，
     * 只读总览那份可能还是旧的（它读盘时机不同）。 */
    const saveDir = (k && k.settings && k.settings.save_path) || live.save_dir || '';

    let body;
    if (!groups.length) {
      body = '<p class="muted">录像目录为空，还没有录制产物。</p>';
    } else {
      /* 占比以「最大的一组」为满格 —— 用总量做分母时，多平台并存会让
       * 每一根条都短得看不出差别，反而丢掉"哪个平台占得多"这个信息。 */
      const max = Math.max.apply(null, groups.map((g) => g.bytes || 0)) || 1;
      body = '<div class="lr-groups">' +
        groups.map((g) => {
          const pct = Math.max(2, Math.round(((g.bytes || 0) / max) * 100));
          return (
            '<div class="lr-group">' +
              '<div class="lr-group__head"><span>' + esc(g.name || '其他') + '</span>' +
              '<span class="num">' + esc(U.bytes(g.bytes || 0)) + '</span></div>' +
              '<div class="lr-group__bar"><div class="lr-group__fill" style="width:' + pct + '%"></div></div>' +
            '</div>'
          );
        }).join('') + '</div>';
    }

    return (
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">录像库</h2>' +
        '<span class="num small faint">共 ' + esc(U.bytes(total)) + (rec.partial ? '（部分）' : '') + '</span></div>' +
        '<p class="small faint" style="margin:0 0 12px">' + esc(saveDir) +
          (live.save_exists ? '' : ' · 尚未录制过') + '</p>' +
        body +
      '</section>'
    );
  }

  /* —— 实时日志：内核的环形缓冲，按 seq 增量拉取 ——
   *
   * 刻意不读日志文件：它在录像目录（可能被迁到别的盘），且是追加写的、
   * 没有「本次会话从哪开始」的概念。环形缓冲语义清晰且零磁盘 IO。 */
  function liveLogSection(k) {
    const online = !!(k && k.daemon !== false && k.ok);
    return (
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">运行日志</h2>' +
        '<span class="small faint" id="lt-log-meta">' +
          (online ? '自动刷新' : '内核未运行') + '</span></div>' +
        (online
          ? '<div class="lr-log" id="lt-log" role="log" aria-live="polite">' +
              '<div class="lr-log__empty">正在读取…</div></div>' +
            '<div class="flex mt-2" style="align-items:center">' +
              '<button class="btn btn--ghost btn--sm" id="lt-log-clear">清空显示</button>' +
              /* 桌面窗口的「导出日志」（Ctrl+E）并入本页：导出的是会话内
               * 累计缓冲，与屏幕上看到的完全一致。 */
              '<button class="btn btn--ghost btn--sm" id="lt-log-export">' +
                I.icon('openFolder', 14) + '导出日志</button>' +
              '<span class="small faint">日志保留最近若干条；内核重启后重新计数。</span>' +
            '</div>'
          : '<p class="muted">内核未运行，暂无日志。启动后这里会实时滚动显示轮询、开播、录制与合并过程。</p>') +
      '</section>'
    );
  }

  /* —— 网页内扫码登录 ——
   *
   * 二维码由后端推 base64 PNG（Playwright 抠出来的真实码），本页只负责显示。
   * 登录判据用的是「登录后才有」的 Cookie，由内核轮询后写入 config.json，
   * 所以这里看到「已登录」就一定是真的落盘了。 */
  function liveLoginSection(k) {
    const online = !!(k && k.daemon !== false && k.ok);
    const login = (k && k.login) || {};
    const sess = (k && k.login_session) || {};

    /* 登录态三态展示：快手不过登录就监控不到任意指定主播，这条差异必须说清，
     * 否则用户会以为「加了房间却没录」是程序坏了。 */
    return (
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">登录态</h2>' +
        '<span class="small faint">扫码在本页完成，登录信息保存在本机</span></div>' +
        '<div class="health-grid">' +
          lrLoginItem('快手', !!login.kuaishou,
            login.kuaishou ? 'Cookie 就绪，可监控任意指定主播'
                           : '未登录，只能监控推荐流覆盖到的房间', 'kuaishou', online) +
          lrLoginItem('抖音', !!login.douyin,
            login.douyin ? 'Cookie 就绪' : '未登录（抖音无需登录即可解析，可选）', 'douyin', online) +
        '</div>' +
        '<div id="lt-login-panel">' + liveLoginPanel(sess, online) + '</div>' +
      '</section>'
    );
  }

  function lrLoginItem(name, ok, desc, platform, online) {
    return (
      '<div class="health-item">' +
        '<span class="dot ' + (ok ? 'dot--ok' : 'dot--amber') + '"></span>' +
        '<div class="health-body"><div class="health-name">' + esc(name) + '</div>' +
        '<div class="health-desc">' + esc(desc) + ' ' +
          (online
            ? '<button class="btn btn--ghost btn--xs" data-login="' + esc(platform) + '">' +
              (ok ? '重新扫码' : '扫码登录') + '</button>'
            : '') +
        '</div></div>' +
      '</div>'
    );
  }

  /* 扫码面板：state 机 idle/starting/waiting/success/failed/cancelled/unavailable */
  function liveLoginPanel(sess, online) {
    if (!online) {
      return '<p class="small faint" style="margin:12px 0 0">内核未运行，无法扫码。启动内核后即可在此登录。</p>';
    }
    const state = (sess && sess.state) || 'idle';

    if (state === 'starting') {
      return '<div class="lr-qr"><div class="small faint">' + I.icon('refresh', 14) +
        ' 正在启动浏览器并获取二维码…（首次可能要几秒）</div></div>';
    }
    if (state === 'waiting' && sess.qr) {
      const who = LR_PLATFORM_LABEL[sess.platform] || sess.platform || '';
      return (
        '<div class="lr-qr">' +
          '<div class="lr-qr__box">' +
            /* qr_version 变化才重设 src：内核只在字节真变了才 +1，
             * 所以这里按版本号去重不会让图片闪烁重载。 */
            '<img id="lt-qr-img" class="lr-qr__img" alt="' + esc(who) + '登录二维码" ' +
              'data-v="' + esc(String(sess.qr_version || 0)) + '" ' +
              'src="data:image/png;base64,' + esc(sess.qr) + '">' +
          '</div>' +
          '<div class="lr-qr__side">' +
            '<div class="lr-qr__title">用「' + esc(who) + '」App 扫码登录</div>' +
            '<ol class="lr-qr__steps">' +
              '<li>打开 ' + esc(who) + ' App</li>' +
              '<li>进入「扫一扫」</li>' +
              '<li>扫描左侧二维码并确认</li>' +
            '</ol>' +
            '<div class="small faint" id="lt-qr-state" aria-live="polite">等待扫码…（已等待 ' +
              Math.round(sess.elapsed || 0) + ' 秒）</div>' +
            '<div class="flex mt-2">' +
              '<button class="btn btn--ghost btn--sm" id="lt-qr-refresh">刷新二维码</button>' +
              '<button class="btn btn--ghost btn--sm" id="lt-qr-cancel">取消</button>' +
            '</div>' +
          '</div>' +
        '</div>'
      );
    }
    if (state === 'success') {
      return '<div class="lr-qr"><div class="lr-qr__done">' + I.icon('check', 16) +
        ' 登录成功，Cookie 已保存（' + esc(String(sess.cookie_chars || 0)) + ' 字符）。此提示可关闭。</div>' +
        '<button class="btn btn--ghost btn--sm mt-2" id="lt-qr-dismiss">知道了</button></div>';
    }
    if (state === 'failed') {
      return '<div class="error-box" style="margin-top:12px"><h3>登录未完成</h3>' +
        '<p class="muted">' + esc(sess.error || '未知原因') + '</p>' +
        '<button class="btn btn--ghost btn--sm" id="lt-qr-dismiss">关闭</button></div>';
    }
    if (state === 'cancelled') {
      return '<p class="small faint" style="margin:12px 0 0">已取消扫码。</p>';
    }
    return '<p class="small faint" style="margin:12px 0 0">点上方「扫码登录」开始。二维码显示在本页，无需切换窗口。</p>';
  }

  /* —— 状态区：各工具自己的键值 —— */
  function statusSection(tool, d, live, k) {
    const t = (d.tools || {})[tool.id] || {};
    const dir = (d.dirs || {})[tool.dirKey] || {};
    const items = [];

    items.push({ ok: !!t.ready, name: '模块就绪', desc: t.ready ? '依赖齐全' : '缺少依赖，见下方目录' });

    if (tool.id === 'live') {
      /* 这里只放「环境」类事实：运行时就绪、录像目录、内核版本。
       * 登录态与内核运行态各自已有专段（登录态段、录制内核段），
       * 在这里再列一遍只会让同一件事出现两次、且两处可能不同步。 */
      const online = !!(k && k.daemon !== false && k.ok);
      const settings = (k && k.settings) || (live && live.settings) || {};
      items.push({
        ok: online,
        name: '录制内核',
        desc: online
          ? ('无界面内核在线（v' + esc(String((k && k.version) || '?')) + '）')
          : '未运行，点上方「启动录制内核」',
      });
      if (settings.save_path) {
        items.push({
          ok: true,
          name: '录像保存位置',
          desc: settings.save_path,
        });
      }
      if (settings.quality_preference) {
        items.push({
          ok: true,
          name: '画质偏好',
          desc: settings.quality_preference === 'origin' ? '原画优先' : '高清优先',
        });
      }
      if (dir.path) {
        items.push({ ok: !!dir.exists, name: '产物目录',
          desc: dir.exists ? '已存在' : '尚未产生产物，首次录制自动创建' });
      }
      return (
        '<section class="section">' +
          '<div class="section__head"><h2 class="card__title">环境</h2></div>' +
          '<div class="health-grid">' +
          items.map((it) =>
            '<div class="health-item">' +
            '<span class="dot ' + (it.ok ? 'dot--ok' : 'dot--amber') + '"></span>' +
            '<div class="health-body"><div class="health-name">' + esc(it.name) + '</div>' +
            '<div class="health-desc">' + esc(it.desc) + '</div></div></div>'
          ).join('') +
          '</div>' +
        '</section>'
      );
    }

    if (dir.path) {
      items.push({ ok: !!dir.exists, name: '产物目录',
        desc: dir.exists ? '已存在' : '尚未产生产物（首次下载自动创建）' });
    }

    let html =
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">状态</h2></div>' +
        '<div class="health-grid">' +
        items.map((it) =>
          '<div class="health-item">' +
          '<span class="dot ' + (it.ok ? 'dot--ok' : 'dot--amber') + '"></span>' +
          '<div class="health-body"><div class="health-name">' + esc(it.name) + '</div>' +
          '<div class="health-desc">' + esc(it.desc) + '</div></div></div>'
        ).join('') +
        '</div>';

    if (dir.path) {
      html +=
        '<div class="flex mt-3" style="align-items:center">' +
          '<span class="lt-row__path grow">' + esc(dir.path) + '</span>' +
          (dir.exists
            ? '<button class="btn btn--ghost btn--sm" data-opendir="' + esc(tool.dirKey) + '">' +
              I.icon('openFolder', 14) + '打开目录</button>'
            : '') +
        '</div>';
    }

    return html + '</section>';
  }

  /* —— 自检（直播录制） —— */
  function selftestSection() {
    return (
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">环境自检</h2>' +
        '<span class="small faint">检查依赖 / ffmpeg / 配置 / 解析层；录制异常时先跑这个</span></div>' +
        '<div class="flex mb-3">' +
          '<button class="btn btn--ghost btn--sm" id="lt-selftest">' +
            I.icon('shield', 14) + '开始自检</button>' +
        '</div>' +
        '<div id="lt-out-wrap"></div>' +
      '</section>'
    );
  }

  /* —— 最近任务 —— */
  function recentSection() {
    return (
      '<section class="section">' +
        '<div class="section__head"><h2 class="card__title">最近任务</h2>' +
        '<button class="btn btn--ghost btn--sm" data-go="tasks">全部任务</button></div>' +
        '<div id="lt-recent">' + C.skeletonRows(2) + '</div>' +
      '</section>'
    );
  }

  /* ============================================================
   * 交互绑定
   * ============================================================ */
  /* ============================================================
   * 直播录制控制台：轮询与增量日志
   * ============================================================
   * 为什么用轮询而不是 SSE/WebSocket：桥接是 aiohttp，加一条推送通道要
   * 动服务端架构；而录制状态本来就是「分钟级变化」的东西（轮询间隔 60s），
   * 5 秒一次的轮询对本地回环几乎没有成本，实现与排查都简单得多。
   *
   * 定时器是**模块级单例**：切页/重渲染必须停掉旧的，否则离开这一屏
   * 还在后台刷接口、并且拿旧 DOM 去写（会静默失效或抛错）。
   */
  const LIVE_POLL_MS = 5000;
  let lrPollTimer = null;
  let lrLogSince = 0;
  /* 会话内累计的日志行（文本）。桌面窗口的「导出日志」并入网页后，
   * 导出的就是这份累计缓冲 —— 「清空显示」会同时清掉它，与所见一致。 */
  let lrLogLines = [];

  function stopLivePolling() {
    if (lrPollTimer) {
      clearInterval(lrPollTimer);
      lrPollTimer = null;
    }
  }

  function startLivePolling(view, k) {
    stopLivePolling();
    // 内核离线时不轮询：没有任何东西可刷，白白打接口
    if (!k || k.daemon === false || !k.ok) return;
    lrPollTimer = setInterval(() => { pollLiveOnce(view); }, LIVE_POLL_MS);
  }

  /* 一次刷新：状态 + 增量日志。
   * 房间表状态变化频繁（开播/下播/分片），但要能就地更新 —— 整页重渲染会
   * 打断用户正在输入的房间链接、也会把日志滚动位置冲掉。所以这里**只更新
   * 值**，不动结构。只有房间增删这种结构变化才走整页 load()。 */
  async function pollLiveOnce(view) {
    if (!view.isConnected) { stopLivePolling(); return; }
    let k = null;
    try {
      k = await window.DYApi.liveKernelStatus();
    } catch (e) {
      return; // 临时抖动不当作离线，等下一轮
    }
    if (!k || k.daemon === false || !k.ok) {
      // 内核下线了（被桌面窗口关掉/崩了）→ 停轮询并提示刷新
      stopLivePolling();
      updateLiveMeta(view, k);
      return;
    }
    updateRoomTableInPlace(view, k);
    updateLiveMeta(view, k);
    refreshLog(view);
  }

  /* —— 就地更新房间表的「可变列」：状态/标题/时长/体积/分段 ——
   *
   * 行用 data-key 对齐。找不到对应行说明结构变了（刚加了房间），
   * 这时交回给调用方决定要不要整页重载 —— 这里只跳过，不猜。 */
  function updateRoomTableInPlace(view, k) {
    const table = view.querySelector('table.table');
    if (!table) return;
    const rows = k.rooms || [];
    const byKey = {};
    rows.forEach((r) => { byKey[r.key] = r; });

    table.querySelectorAll('tbody tr[data-key]').forEach((tr) => {
      const r = byKey[tr.dataset.key];
      if (!r) return;
      const recording = r.status === 'recording' || r.status === 'reconnecting'
                        || r.status === 'merging';
      const cells = tr.querySelectorAll('td');
      // 列序与 liveRoomTable 严格对应：平台/房间/状态/标题/已录/体积/分段/操作
      if (cells[2] && cells[2].dataset.cell === 'status') {
        cells[2].innerHTML = roomStatusCell(r);
      }
      if (cells[3]) cells[3].textContent = r.title || '—';
      if (cells[4]) cells[4].textContent = recording ? U.duration(r.recorded_seconds || 0) : '—';
      if (cells[5]) cells[5].textContent = recording ? U.bytes(r.total_bytes || 0) : '—';
      if (cells[6]) cells[6].textContent = recording ? String(r.segment_count || 0) : '—';
    });
  }

  function updateLiveMeta(view, k) {
    const meta = view.querySelector('#lt-log-meta');
    if (!meta) return;
    if (!k || k.daemon === false || !k.ok) {
      meta.textContent = '内核已离线，点上方「启动录制内核」';
      return;
    }
    const t = new Date();
    const pad = (n) => (n < 10 ? '0' + n : String(n));
    meta.textContent = '自动刷新 · ' + pad(t.getHours()) + ':' + pad(t.getMinutes())
                       + ':' + pad(t.getSeconds());
  }

  /* —— 增量拉日志并追加到滚动区 ——
   *
   * 只 append，不重绘整块：重绘会把用户的滚动位置冲回顶部，
   * 而「正在看日志」时被弹回顶部是最恼人的体验之一。 */
  async function refreshLog(view) {
    const box = view.querySelector('#lt-log');
    if (!box) return;
    let d;
    try {
      d = await window.DYApi.liveKernelLogs(lrLogSince);
    } catch (e) {
      return;
    }
    if (d.daemon === false) return;
    const items = d.items || [];
    if (typeof d.seq === 'number') lrLogSince = d.seq;

    if (!items.length) {
      if (!box.dataset.filled) {
        box.innerHTML = '<div class="lr-log__empty">暂无日志（内核刚启动或还没轮询到）</div>';
      }
      return;
    }
    box.dataset.filled = '1';
    const empty = box.querySelector('.lr-log__empty');
    if (empty) empty.remove();

    // 只有本来就在底部时才自动滚 —— 用户上翻看历史时不要抢滚动条
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;

    const frag = document.createDocumentFragment();
    items.forEach((it) => {
      const line = document.createElement('div');
      line.className = 'lr-log__line lr-log__line--' + (it.level || 'info');
      line.textContent = (it.time ? '[' + it.time + '] ' : '') + (it.message || it.text || '');
      frag.appendChild(line);
      lrLogLines.push(line.textContent);
    });
    box.appendChild(frag);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  /* ============================================================
   * 产物目录：自定义保存位置
   * ============================================================ */
  function dirBox(view, k) {
    return view.querySelector('[data-direditbox="' + k + '"]');
  }
  function dirInput(view, k) {
    return view.querySelector('[data-dirinput="' + k + '"]');
  }

  function bindDirEdit(view) {
    const $ = (s) => view.querySelector(s);
    const $$ = (s) => Array.prototype.slice.call(view.querySelectorAll(s));

    // 展开 / 收起编辑条
    $$('[data-diredit]').forEach((b) => {
      b.addEventListener('click', () => {
        const box = dirBox(view, b.dataset.diredit);
        if (!box) return;
        box.hidden = !box.hidden;
        if (!box.hidden) {
          const i = dirInput(view, b.dataset.diredit);
          if (i) { i.focus(); i.select(); }
        }
      });
    });

    // 取消：收起并把输入框还原成打开时的值（否则下次展开看到的是半截改过的路径）
    $$('[data-dircancel]').forEach((b) => {
      b.addEventListener('click', () => {
        const k = b.dataset.dircancel;
        const box = dirBox(view, k);
        const i = dirInput(view, k);
        if (i && i.dataset.orig != null) i.value = i.dataset.orig;
        if (box) box.hidden = true;
      });
    });

    // 输入框回车即保存（手打路径时最常见的动作）
    $$('[data-dirinput]').forEach((i) => {
      i.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const btn = view.querySelector('[data-dirsave="' + i.dataset.dirinput + '"]');
        if (btn) btn.click();
      });
    });

    /* 浏览：弹系统「选择文件夹」。
     * 对话框由桥接服务进程弹出，受限环境下会失败 —— 这时明确告诉用户
     * 直接手打路径，不能让按钮一直转圈。 */
    $$('[data-dirpick]').forEach((b) => {
      b.addEventListener('click', async () => {
        const k = b.dataset.dirpick;
        const i = dirInput(view, k);
        const old = b.textContent;
        b.disabled = true;
        b.textContent = '选择中…';
        try {
          const r = await window.DYApi.toolPickDir((i && i.value) || '');
          if (r && r.path) {
            if (i) i.value = r.path;
          } else {
            C.toast('没有选择目录', 'warn');
          }
        } catch (e2) {
          C.toast(e2.message || '无法弹出选择框，请直接输入路径', 'warn');
        } finally {
          b.disabled = false;
          b.textContent = old;
        }
      });
    });

    $$('[data-dirsave]').forEach((b) => {
      b.addEventListener('click', async () => {
        const k = b.dataset.dirsave;
        const i = dirInput(view, k);
        const val = ((i && i.value) || '').trim();
        if (!val) { C.toast('请先填写路径', 'warn'); if (i) i.focus(); return; }
        const old = b.textContent;
        b.disabled = true;
        b.textContent = '保存中…';
        try {
          const r = await window.DYApi.toolSetDir(k, val);
          C.toast(r.message || '已保存', 'ok');
          await load(view);   // 重拉一次，页面显示的路径必须是落盘后回读的真值
        } catch (e2) {
          C.toast(e2.message || '保存失败', 'err');
        } finally {
          b.disabled = false;
          b.textContent = old;
        }
      });
    });

    $$('[data-dirreset]').forEach((b) => {
      b.addEventListener('click', async () => {
        const k = b.dataset.dirreset;
        const old = b.textContent;
        b.disabled = true;
        try {
          const r = await window.DYApi.toolSetDir(k, '');
          C.toast(r.message || '已恢复默认位置', 'ok');
          await load(view);
        } catch (e2) {
          C.toast(e2.message || '恢复失败', 'err');
        } finally {
          b.disabled = false;
          b.textContent = old;
        }
      });
    });
  }

  /* ============================================================
   * 交互绑定
   * ============================================================ */
  function bindActions(view, tool, d, k) {
    const $ = (s) => view.querySelector(s);

    // —— 通用：跳转 / 打开目录 ——
    view.querySelectorAll('[data-go]').forEach((b) => {
      b.addEventListener('click', () => window.DYRouter.go(b.dataset.go));
    });
    view.querySelectorAll('[data-opendir]').forEach((b) => {
      b.addEventListener('click', async () => {
        try {
          const r = await window.DYApi.toolOpenDir(b.dataset.opendir);
          C.toast('已打开目录', 'ok');
        } catch (e) {
          // 目录不存在是正常状态（还没下载过），如实告知，不当作错误弹红
          C.toast(e.message || '打开失败', /不存在/.test(e.message || '') ? 'warn' : 'err');
        }
      });
    });

    // —— 产物目录：自定义保存位置 ——
    bindDirEdit(view);

    // —— Stripchat 录制（模块 6）：开始 / 停止 / 打开目录 ——
    const scGo = $('#sc-go');
    if (scGo) {
      scGo.addEventListener('click', async () => {
        const input = $('#sc-url');
        const hint = $('#sc-hint');
        const url = ((input && input.value) || '').trim();
        if (!url) { C.toast('请先粘贴房间链接', 'warn'); return; }
        scGo.disabled = true;
        try {
          await window.DYApi.scStart(url);
          if (input) input.value = '';
          if (hint) hint.textContent = '录制已启动：后台进程运行中，停止时自动合并。';
          C.toast('录制已启动', 'ok');
          scPollOnce(view);
        } catch (e) {
          C.toast(e.message || '启动失败', 'err');
        } finally {
          scGo.disabled = false;
        }
      });
      const scInput = $('#sc-url');
      if (scInput) scInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); scGo.click(); }
      });
    }
    const scStopBtn = $('#sc-stop');
    if (scStopBtn) {
      scStopBtn.addEventListener('click', async () => {
        scStopBtn.disabled = true;
        try {
          const r = await window.DYApi.scStop();
          C.toast(r && r.graceful === false ? '已强制结束（合并可能不完整）' : '已停止并合并', 'ok');
          scPollOnce(view);
        } catch (e) {
          C.toast(e.message || '停止失败', 'err');
        } finally {
          scStopBtn.disabled = false;
        }
      });
    }
    const scOpen = $('#sc-open');
    if (scOpen) {
      scOpen.addEventListener('click', async () => {
        try {
          await window.DYApi.scOpenDir();
          C.toast('已打开目录', 'ok');
        } catch (e) {
          C.toast(e.message || '打开失败', /不存在/.test(e.message || '') ? 'warn' : 'err');
        }
      });
    }

    // —— 主操作（抖音 / 直链 / 快手） ——
    // —— 统一链接入口（识别 → 路由） ——
    bindUnified(view)


    // —— 单工具页：输入框提交（抖音 / 快手 / 直链 / TikTok-YouTube） ——
    const go = $('#lt-go');
    if (go) {
      const input = $('#lt-url');
      const submit = () => submitDownload(view, tool, input, go);
      go.addEventListener('click', submit);
      if (input) {
        input.addEventListener('keydown', (e) => {
          if (e.key !== 'Enter') return;
          /* 快手/ty 单行框 Enter 提交；抖音/直链多行框 Ctrl/Cmd+Enter 提交，
           * 直链框里没有换行时 Enter 也提交（只贴一条还要求组合键很反常）。 */
          let ok;
          if (tool.id === 'kuaishou' || tool.id === 'ty' || tool.id === 'ytdlp') ok = true;
          else if (tool.id === 'douyin') ok = !!(e.ctrlKey || e.metaKey);
          else ok = !/\n/.test(input.value) || !!(e.ctrlKey || e.metaKey);
          if (ok) {
            e.preventDefault();
            submit();
          }
        });
      }
    }

    // —— 直链：只解析切换按钮文案 ——
    const resolve = $('#lt-resolve');
    if (resolve) {
      resolve.addEventListener('change', () => {
        const btn = $('#lt-go');
        btn.innerHTML = resolve.checked
          ? I.icon('link', 15) + '解析'
          : I.icon('download', 15) + '下载';
      });
    }

    // —— yt-dlp 万能下载：预览 / 升级 / 高级配置 ——
    const ydPrev = $('#yd-preview');
    if (ydPrev) {
      ydPrev.addEventListener('click', async () => {
        const input = $('#lt-url');
        const url = ((input && input.value) || '').trim();
        const box = $('#yd-preview-box');
        if (!/^https?:\/\//.test(url)) {
          C.toast('请先粘贴 http(s) 链接', 'warn');
          return;
        }
        ydPrev.disabled = true;
        if (box) box.innerHTML = '<div class="small faint"><span class="dot dot--amber dot--pulse"></span> 正在解析…（境外站可能需要十几秒）</div>';
        try {
          const p = await window.DYApi.ytdlpPreview(url);
          renderYtdlpPreview(view, p);
        } catch (e) {
          renderYtdlpPreview(view, { ok: false, error: e.message || '预览失败' });
        } finally {
          ydPrev.disabled = false;
        }
      });
    }
    const ydUpd = $('#yd-update');
    if (ydUpd) {
      ydUpd.addEventListener('click', async () => {
        ydUpd.disabled = true;
        const out = $('#yd-maintain-out');
        if (out) out.innerHTML = '<div class="small faint"><span class="dot dot--amber dot--pulse"></span> 正在升级 yt-dlp…（约 1~2 分钟）</div>';
        try {
          const r = await window.DYApi.ytdlpUpdate();
          if (out) out.innerHTML = '<div class="error-box" style="padding:8px 10px;max-height:220px;overflow:auto">' +
            esc(r.output || (r.ok ? '升级完成' : '升级失败')) + '</div>';
          C.toast(r.ok ? ('yt-dlp 已升级到 ' + (r.version || '最新版')) : '升级失败，详见输出', r.ok ? 'ok' : 'err');
        } catch (e) {
          if (out) out.innerHTML = '<div class="error-box" style="padding:8px 10px">' + esc(e.message || '升级失败') + '</div>';
          C.toast(e.message || '升级失败', 'err');
        } finally {
          ydUpd.disabled = false;
        }
      });
    }
    const ydConf = $('#yd-conf');
    const loadYdConf = async () => {
      if (!ydConf) return;
      try {
        const r = await window.DYApi.ytdlpConfGet();
        if (ydConf.value.trim() === '') ydConf.value = r.content || '';
      } catch (e) { /* 配置读不到就让 textarea 保持空 */ }
    };
    loadYdConf();
    const ydConfReload = $('#yd-conf-reload');
    if (ydConfReload) {
      ydConfReload.addEventListener('click', async () => {
        try {
          const r = await window.DYApi.ytdlpConfGet();
          ydConf.value = r.content || '';
          C.toast('已重读配置', 'ok');
        } catch (e) {
          C.toast(e.message || '读取失败', 'err');
        }
      });
    }
    const ydConfSave = $('#yd-conf-save');
    if (ydConfSave) {
      ydConfSave.addEventListener('click', async () => {
        ydConfSave.disabled = true;
        try {
          await window.DYApi.ytdlpConfSave(ydConf.value || '');
          C.toast('配置已保存，下一个任务生效', 'ok');
        } catch (e) {
          C.toast(e.message || '保存失败', 'err');
        } finally {
          ydConfSave.disabled = false;
        }
      });
    }

    // —— 直播控制台：内核控制 / 单房间操作 / 扫码 / 日志 / 设置 ——
    // （桌面窗口入口已随合并删除：本页就是唯一的直播流录制界面。）

    /* 内核动作的通用包装：动作都会改变状态，成功后就地刷新一次。
     * 抽出来是因为下面有 6 个按钮走的是同一条路 —— 各写一遍必然分叉。 */
    const kernelAction = (btn, fn, okMsg) => {
      if (!btn) return;
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
          const r = await fn();
          C.toast((r && r.message) || okMsg || '已执行', 'ok');
          await load(view);
        } catch (e) {
          // 内核离线/端口被占等情况要如实说，并把「怎么恢复」也带上
          C.toast(e.message || '操作失败', e.offline ? 'warn' : 'err');
          if (e.offline) await load(view);
        } finally {
          if (btn.isConnected) btn.disabled = false;
        }
      });
    };

    kernelAction($('#lt-kensure'), () => window.DYApi.liveKernelEnsure(true), '录制内核已启动');
    kernelAction($('#lt-kstart'), () => window.DYApi.liveKernelStart(), '监控已启动');
    kernelAction($('#lt-kstop'), () => window.DYApi.liveKernelStop(), '监控已停止');
    kernelAction($('#lt-kcheck'), () => window.DYApi.liveKernelCheck(), '已触发一次检查');

    // 停止内核：要收尾合并分片，可能耗时，所以按钮上先给个进行中的反馈
    const koff = $('#lt-koff');
    if (koff) {
      koff.addEventListener('click', async () => {
        koff.disabled = true;
        const old = koff.innerHTML;
        koff.textContent = '正在停止（收尾合并中）…';
        try {
          const r = await window.DYApi.liveKernelShutdown();
          C.toast((r && r.message) || '录制内核已停止', 'ok');
          await load(view);
        } catch (e) {
          C.toast(e.message || '停止失败', 'err');
          if (koff.isConnected) { koff.disabled = false; koff.innerHTML = old; }
        }
      });
    }

    // —— 单房间操作：停录 / 启停用 / 删除 ——
    view.querySelectorAll('[data-room-stop]').forEach((b) => {
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const r = await window.DYApi.liveKernelStopRoom(b.dataset.roomStop);
          C.toast((r && r.message) || '已停止该房间的录制', 'ok');
          await load(view);
        } catch (e) {
          C.toast(e.message || '操作失败', 'err');
          if (b.isConnected) b.disabled = false;
        }
      });
    });

    view.querySelectorAll('[data-room-toggle]').forEach((b) => {
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const next = b.dataset.next === '1';
          const r = await window.DYApi.liveKernelToggleRoom(b.dataset.roomToggle, next);
          C.toast((r && r.message) || (next ? '已启用' : '已停用'), 'ok');
          await load(view);
        } catch (e) {
          C.toast(e.message || '操作失败', 'err');
          if (b.isConnected) b.disabled = false;
        }
      });
    });

    /* 删除是不可撤销的破坏性操作，必须二次确认，且确认文案里带上主播名
     * —— 「确定删除？」这种没有宾语的问句，用户没法确认自己删的是哪一条。 */
    view.querySelectorAll('[data-room-del]').forEach((b) => {
      b.addEventListener('click', async () => {
        const name = b.dataset.roomName || '该房间';
        if (!window.confirm('确定把「' + name + '」从监控清单中删除？\n\n' +
                            '只删除监控对象，已录好的文件不受影响。')) return;
        b.disabled = true;
        try {
          const r = await window.DYApi.liveKernelRemoveRoom(b.dataset.roomDel);
          C.toast((r && r.message) || '已删除', 'ok');
          await load(view);
        } catch (e) {
          C.toast(e.message || '删除失败', 'err');
          if (b.isConnected) b.disabled = false;
        }
      });
    });

    // 复制原始输入（桌面窗口右键菜单的「复制原始输入」并入本页）
    view.querySelectorAll('[data-room-copy]').forEach((b) => {
      b.addEventListener('click', async () => {
        const text = b.dataset.copy || '';
        if (!text) { C.toast('这条还没有可复制的链接', 'warn'); return; }
        try {
          await navigator.clipboard.writeText(text);
          C.toast('已复制到剪贴板', 'ok');
        } catch (e) {
          // 剪贴板 API 在非 https 环境（127.0.0.1 例外）或旧内核下可能不可用
          C.toast('复制失败：' + (e.message || '剪贴板不可用'), 'err');
        }
      });
    });

    /* 打开该房间的录制目标文件夹：路径在按钮的 data-room-dir 里（内核快照
     * 的 output_dir），打开动作由桥接后端完成 —— 网页本身没有权限拉起
     * 资源管理器。路径不存在时后端如实回 404，这里把话转给用户。 */
    view.querySelectorAll('[data-room-dir]').forEach((b) => {
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const r = await window.DYApi.liveOpenDir(b.dataset.roomDir);
          C.toast('已打开：' + (r.path || '录像文件夹'), 'ok');
        } catch (e) {
          C.toast(e.message || '打开失败', 'err');
        }
        if (b.isConnected) b.disabled = false;
      });
    });

    // —— 扫码登录 ——
    bindLogin(view);

    // —— 录制设置（桌面窗口设置对话框的网页版） ——
    bindLiveSettings(view);

    // —— 日志：清空只清显示，不影响内核缓冲 ——
    const logClear = $('#lt-log-clear');
    if (logClear) {
      logClear.addEventListener('click', () => {
        const box = $('#lt-log');
        if (box) {
          box.dataset.filled = '';
          box.innerHTML = '<div class="lr-log__empty">已清空显示。新日志会继续追加。</div>';
        }
        lrLogLines = [];   // 导出的是「所见」——清空显示同时清掉累计缓冲
      });
    }

    // 导出日志：把会话内累计的行写成 txt 下载（桌面窗口 Ctrl+E 的网页版）
    const logExport = $('#lt-log-export');
    if (logExport) {
      logExport.addEventListener('click', () => {
        if (!lrLogLines.length) { C.toast('还没有可导出的日志', 'warn'); return; }
        const t = new Date();
        const pad = (n) => (n < 10 ? '0' + n : String(n));
        const stamp = t.getFullYear() + pad(t.getMonth() + 1) + pad(t.getDate()) +
                      '-' + pad(t.getHours()) + pad(t.getMinutes()) + pad(t.getSeconds());
        /* BOM 让 Windows 记事本按 UTF-8 读，否则中文日志打开是乱码 */
        const blob = new Blob(['\ufeff' + lrLogLines.join('\r\n') + '\r\n'],
                              { type: 'text/plain;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'live-log-' + stamp + '.txt';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        C.toast('已导出 ' + lrLogLines.length + ' 行日志', 'ok');
      });
    }

    const addRoom = $('#lt-addroom');
    if (addRoom) {
      const input = $('#lt-room');
      const doAdd = async () => {
        const rawVal = (input.value || '').trim();
        if (!rawVal) { C.toast('请先粘贴直播链接', 'warn'); return; }
        /* 识别兜底：贴进来的不是直播间链接（视频/直链等）就**不提交**——
         * 静默转下载会让用户以为在录。视频请走启动台「链接下载」入口。
         * ⚠️ 例外：v.kuaishou.com 分享短链从 URL 区分不了视频/直播 ——
         *   先到后端跟一次重定向判定；解析不可用/失败时放行交给录制器
         *   自行解析（监控清单里已有 4 条短链房间都是这么解析成功的）。
         *   分享文案整段粘贴也支持：取提取出的第一条链接做判定。 */
        const urls = U.extractUrls(rawVal);
        const firstUrl = urls[0] || rawVal;
        const cls = classifyLink(firstUrl);
        if (cls && cls.type !== 'live') {
          const isKsShort = /\/\/v\.kuaishou\.com\//i.test(firstUrl);
          let isLive = false;
          if (isKsShort && typeof window.DYApi.resolveLink === 'function') {
            try {
              const r = await window.DYApi.resolveLink(firstUrl);
              isLive = !!r.is_live;
            } catch (e) { isLive = true; /* 解析失败：放行，录制器自行判定 */ }
          } else if (isKsShort) {
            isLive = true;   /* 解析方法缺失（旧缓存）：放行，录制器自行判定 */
          }
          if (!isLive) {
            C.toast('识别为「' + cls.label + '」，不是直播间链接 —— ' +
                    '视频请在启动台「链接下载」入口粘贴', 'warn');
            return;
          }
        }
        addRoom.disabled = true;
        try {
          const r = await window.DYApi.liveAddRoom(rawVal);
          input.value = '';
          C.toast(r.message || '已加入监控清单', 'ok');
          /* 加完立刻重载：监控清单是本页的主体内容，新加的那条必须当场出现。
           * 早先只写一句 hint、不重载，用户看到"已加入"却在表里找不到那行，
           * 只能自己想到去点"刷新状态" —— 那是把该页面做的事推给了用户。
           * 重载会重建 DOM，所以提示写在重载之后的新节点上。 */
          await load(view);
          const hint = view.querySelector('#lt-hint');
          if (hint) hint.innerHTML = '<span class="dot dot--green"></span> ' + esc(r.message || '已加入监控清单');
        } catch (e) {
          C.toast(e.message || '添加失败', 'err');
        } finally {
          addRoom.disabled = false;
        }
      };
      addRoom.addEventListener('click', doAdd);
      if (input) input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doAdd(); });
    }

    const st = $('#lt-selftest');
    if (st) {
      st.addEventListener('click', async () => {
        const wrap = $('#lt-out-wrap');
        st.disabled = true;
        wrap.innerHTML = '<div class="small faint">正在自检…（会探测 ffmpeg / 运行时 / 网络，可能几秒）</div>';
        try {
          const r = await window.DYApi.toolSelftest('live');
          wrap.innerHTML =
            '<pre class="lt-out ' + (r.ok ? 'lt-out--ok' : 'lt-out--bad') + '">' +
            esc(r.output || '（无输出）') + '</pre>';
          C.toast(r.ok ? '自检通过' : '自检发现问题', r.ok ? 'ok' : 'warn');
        } catch (e) {
          wrap.innerHTML = '<div class="error-box"><h3>自检失败</h3><p class="muted">' +
            esc(e.message || String(e)) + '</p></div>';
          C.toast(e.message || '自检失败', 'err');
        } finally {
          st.disabled = false;
        }
      });
    }

    // —— 最近任务 ——
    if (view.querySelector('#lt-recent')) loadRecent(view, tool);
  }

  /* ============================================================
   * 扫码登录：本页渲染二维码 + 轮询登录态
   * ============================================================
   * 为什么要轮询：扫码是**用户侧异步事件**（什么时候扫、扫完确认要几秒都不由我们
   * 决定），没有推送通道就只能轮。间隔取 1.5s —— 与后端 LoginSession 内部
   * 查登录态的节拍一致，比它更密没有意义（后端本来就是这个频率）。
   *
   * 定时器同样必须可停：会话结束或切页都要停掉，否则会一直打接口。
   */
  const LOGIN_POLL_MS = 1500;
  let lrLoginTimer = null;

  function stopLoginPolling() {
    if (lrLoginTimer) {
      clearInterval(lrLoginTimer);
      lrLoginTimer = null;
    }
  }

  function bindLogin(view) {
    const start = (platform) => {
      (async () => {
        try {
          await window.DYApi.liveKernelLoginStart(platform);
        } catch (e) {
          C.toast(e.message || '无法开始扫码', e.offline ? 'warn' : 'err');
          return;
        }
        renderLoginPanel(view, { state: 'starting', platform: platform });
        pollLogin(view);
      })();
    };

    view.querySelectorAll('[data-login]').forEach((b) => {
      b.addEventListener('click', () => start(b.dataset.login));
    });

    const refresh = view.querySelector('#lt-qr-refresh');
    if (refresh) {
      refresh.addEventListener('click', async () => {
        refresh.disabled = true;
        try {
          await window.DYApi.liveKernelLoginRefresh();
          await pullLoginOnce(view);
        } catch (e) {
          C.toast(e.message || '刷新失败', 'err');
        } finally {
          if (refresh.isConnected) refresh.disabled = false;
        }
      });
    }

    const cancel = view.querySelector('#lt-qr-cancel');
    if (cancel) {
      cancel.addEventListener('click', async () => {
        cancel.disabled = true;
        try {
          await window.DYApi.liveKernelLoginCancel();
          await pullLoginOnce(view);
        } catch (e) {
          C.toast(e.message || '取消失败', 'err');
        } finally {
          if (cancel.isConnected) cancel.disabled = false;
        }
      });
    }

    // 成功/失败后的「关闭」只清掉面板，不重载整页（避免把日志滚动位置冲掉）
    const dismiss = view.querySelector('#lt-qr-dismiss');
    if (dismiss) {
      dismiss.addEventListener('click', () => {
        stopLoginPolling();
        renderLoginPanel(view, { state: 'idle' });
      });
    }

    /* 如果进页面时已有会话在跑（比如用户刷新了浏览器），把它接回来，
     * 否则会出现「后端在等扫码、页面却显示未开始」的错位。 */
    (async () => {
      try {
        const st = await window.DYApi.liveKernelLoginStatus();
        if (st && (st.state === 'waiting' || st.state === 'starting')) {
          renderLoginPanel(view, st);
          pollLogin(view);
        }
      } catch (e) { /* 内核没跑等情况：不接管，等用户点按钮 */ }
    })();
  }

  function pollLogin(view) {
    stopLoginPolling();
    lrLoginTimer = setInterval(() => { pullLoginOnce(view); }, LOGIN_POLL_MS);
  }

  async function pullLoginOnce(view) {
    if (!view.isConnected) { stopLoginPolling(); return; }
    let st;
    try {
      st = await window.DYApi.liveKernelLoginStatus();
    } catch (e) {
      return;
    }
    if (!st || st.state === 'unavailable') { stopLoginPolling(); return; }
    renderLoginPanel(view, st);
    // 终态就停轮询；success 也停（登录态已落盘，没必要继续问）
    if (st.state === 'success' || st.state === 'failed' || st.state === 'cancelled') {
      stopLoginPolling();
      if (st.state === 'success') {
        C.toast('登录成功，登录信息已保存', 'ok');
        /* 登录态变了要刷新上方「登录态」那两格（它们是初始渲染的静态值）。
         * 只重渲染那一块，不动整页 —— 整页重载会把刚显示的成功提示清掉。 */
        syncLoginBadges(view, st.platform);
      }
    }
  }

  /* 就地把健康格里的「未登录」改成「已登录」，不必整页重载 */
  function syncLoginBadges(view, platform) {
    const label = LR_PLATFORM_LABEL[platform] || platform || '';
    view.querySelectorAll('.health-item').forEach((item) => {
      const name = item.querySelector('.health-name');
      if (!name || name.textContent !== label) return;
      const dot = item.querySelector('.dot');
      if (dot) { dot.className = 'dot dot--ok'; }
      const desc = item.querySelector('.health-desc');
      if (desc) {
        desc.innerHTML = platform === 'kuaishou'
          ? 'Cookie 就绪，可监控任意指定主播 <button class="btn btn--ghost btn--xs" data-login="kuaishou">重新扫码</button>'
          : 'Cookie 就绪 <button class="btn btn--ghost btn--xs" data-login="douyin">重新扫码</button>';
        // 新按钮要重新绑（重渲染 desc 会丢掉原来的监听）
        const b = desc.querySelector('[data-login]');
        if (b) b.addEventListener('click', () => { /* 由 bindLogin 的委托没覆盖，这里直接触发 */ 
          window.DYApi.liveKernelLoginStart(platform)
            .then(() => { renderLoginPanel(view, { state: 'starting', platform }); pollLogin(view); })
            .catch((e) => C.toast(e.message || '无法开始扫码', 'err'));
        });
      }
    });
  }

  /* 整块重渲染扫码面板：它是个小状态机，逐字段打补丁不如整体重建清晰 */
  function renderLoginPanel(view, sess) {
    const box = view.querySelector('#lt-login-panel');
    if (!box) return;
    box.innerHTML = liveLoginPanel(sess, true);
    // 重建后原有的监听全丢，必须重新绑（只绑面板内部的那几个）
    const refresh = box.querySelector('#lt-qr-refresh');
    if (refresh) refresh.addEventListener('click', async () => {
      refresh.disabled = true;
      try { await window.DYApi.liveKernelLoginRefresh(); await pullLoginOnce(view); }
      catch (e) { C.toast(e.message || '刷新失败', 'err'); }
      finally { if (refresh.isConnected) refresh.disabled = false; }
    });
    const cancel = box.querySelector('#lt-qr-cancel');
    if (cancel) cancel.addEventListener('click', async () => {
      cancel.disabled = true;
      try { await window.DYApi.liveKernelLoginCancel(); await pullLoginOnce(view); }
      catch (e) { C.toast(e.message || '取消失败', 'err'); }
      finally { if (cancel.isConnected) cancel.disabled = false; }
    });
    const dismiss = box.querySelector('#lt-qr-dismiss');
    if (dismiss) dismiss.addEventListener('click', () => {
      stopLoginPolling();
      renderLoginPanel(view, { state: 'idle' });
    });
  }

  /* —— 提交下载：按 kind 分流到已有端点 —— */

  /* —— 提交下载：按 kind 分流到已有端点 —— */
  async function submitDownload(view, tool, input, btn) {
    const raw = (input.value || '').trim();
    const hint = view.querySelector('#lt-hint');
    const setHint = (html) => { if (hint) hint.innerHTML = html; };

    if (!raw) {
      setHint('<span class="dot dot--amber"></span> 请先粘贴链接');
      C.toast('请先粘贴链接', 'warn');
      return;
    }

    // 与工具箱桌面端同口径：只认 http(s)，其余一律本地拦下
    const urls = U.extractUrls(raw);
    if (!urls.length) {
      setHint('<span class="dot dot--amber"></span> 这段文本里没有找到 http(s) 链接');
      C.toast('未识别到链接', 'warn');
      return;
    }

    btn.disabled = true;
    setHint('<span class="dot dot--amber dot--pulse"></span> 正在提交…');

    try {
      if (tool.id === 'douyin') {
        const res = await window.DYApi.submitDownload({
          url: urls.join(' '),
          options: { modes: ['post'], number: 0, quality: 'highest', increase: true },
        });
        input.value = '';
        setHint('<span class="dot dot--green"></span> 已提交 ' + urls.length + ' 个链接到任务队列');
        C.toast('已加入任务队列', 'ok');
        renderRecent(view, tool);
      } else if (tool.id === 'ty') {
        let okN = 0;
        for (let i = 0; i < urls.length; i++) {
          try {
            await window.DYApi.tyDownload(urls[i], 'all');
            okN++;
          } catch (e) {
            C.toast((urls.length > 1 ? ('第 ' + (i + 1) + ' 个：') : '') +
              (e.message || '提交失败'), 'err');
          }
        }
        if (okN) {
          input.value = '';
          setHint('<span class="dot dot--green"></span> 已提交 ' + okN + '/' + urls.length +
            ' 个链接到任务队列');
          C.toast('已加入任务队列', 'ok');
        }
      } else if (tool.id === 'ytdlp') {
        const opts = collectYtdlpOpts(view);
        let okN = 0;
        for (let i = 0; i < urls.length; i++) {
          try {
            await window.DYApi.ytdlpDownload(urls[i], opts);
            okN++;
          } catch (e) {
            C.toast((urls.length > 1 ? ('第 ' + (i + 1) + ' 个：') : '') +
              (e.message || '提交失败'), 'err');
          }
        }
        if (okN) {
          input.value = '';
          view.querySelector('#yd-preview-box') &&
            (view.querySelector('#yd-preview-box').innerHTML = '');
          setHint('<span class="dot dot--green"></span> 已提交 ' + okN + '/' + urls.length +
            ' 个链接到任务队列');
          C.toast('已加入任务队列', 'ok');
        }
      } else if (tool.id === 'direct') {
        const resolveOnly = view.querySelector('#lt-resolve');
        if (resolveOnly && resolveOnly.checked) {
          // 只解析：不产生任务，交给 clawfetch --resolve，结果直接展示。
          setHint('<span class="dot dot--amber dot--pulse"></span> 正在解析 ' +
            urls.length + ' 个链接…');
          const outs = [];
          let okN = 0;
          for (let i = 0; i < urls.length; i++) {
            if (urls.length > 1) {
              setHint('<span class="dot dot--amber dot--pulse"></span> 正在解析 ' +
                (i + 1) + '/' + urls.length + '…');
            }
            try {
              const r = await window.DYApi.directResolve(urls[i]);
              if (r && r.ok) okN++;
              outs.push('########## ' + (i + 1) + '. ' + urls[i] + '\n' + ((r && r.output) || '(无输出)'));
            } catch (e) {
              outs.push('########## ' + (i + 1) + '. ' + urls[i] + '\n[失败] ' + (e.message || e));
            }
          }
          setHint('<span class="dot ' + (okN === urls.length ? 'dot--green' : 'dot--amber') +
            '"></span> 解析完成：' + okN + '/' + urls.length + ' 个成功，结果见下方');
          C.toast(okN ? '解析完成' : '解析失败', okN ? 'ok' : 'err');
          renderInlineLog(view, outs.join('\n\n'));
        } else {
          const single = view.querySelector('#lt-single');
          const opts = (single && single.checked) ? { segments: 1 } : null;
          /* 多链接走**一个**批量任务：clawfetch 按域名分链，拆成 N 个任务
           * 会 N 个进程各自开 8 连接，同一源站被压出 502。 */
          const task = await window.DYApi.directDownload(
            urls.length > 1 ? urls : urls[0], opts);
          input.value = '';
          const n = (task && task.count) || urls.length;
          setHint('<span class="dot dot--green"></span> 已加入任务队列：' + n + ' 个链接' +
            (n > 1 ? '（同一域名顺序下、不同域名并行；进度见下方）' : ''));
          C.toast('已加入任务队列', 'ok');
          renderRecent(view, tool);
        }
      } else if (tool.id === 'kuaishou') {
        // 先查登录态：登录就后台直下，未登录引导扫码
        const st = await window.DYApi.kuaishouStatus();
        if (st && st.need_login) {
          setHint('<span class="dot dot--amber"></span> 未登录快手，正在唤起扫码窗口…');
          C.toast('未登录快手，请在弹出的窗口扫码', 'warn');
          await window.DYApi.kuaishouLogin();
          setHint('<span class="dot dot--amber"></span> 扫码完成后再次点「下载」即可后台执行');
        } else {
          const r = await window.DYApi.kuaishouDownload(urls[0], 'all');
          input.value = '';
          setHint('<span class="dot dot--green"></span> 已加入任务队列，进度见下方');
          C.toast('已加入任务队列', 'ok');
          renderRecent(view, tool);
        }
      }
    } catch (e) {
      setHint('<span class="dot dot--amber"></span> ' + esc(e.message || '提交失败'));
      C.toast(e.message || '提交失败', 'err');
    } finally {
      btn.disabled = false;
    }
  }

  /* —— 最近任务：复用统一任务队列 —— */
  async function loadRecent(view) {
    renderRecent(view, byId(currentToolId()));
  }

  async function renderRecent(view, tool) {
    const box = view.querySelector('#lt-recent');
    if (!box) return;
    box.innerHTML = C.skeletonRows(2);
    let items = [];
    try {
      const d = await window.DYApi.tasks();
      items = (d && d.tasks) || [];
      // 按 kind 过滤：本工具的页面只显示本工具的任务，避免"在抖音页看到快手任务"的错位
      const want = tool.kind;
      if (want) items = items.filter((t) => (t.kind || 'douyin') === want);
    } catch (e) {
      box.innerHTML = '<p class="muted">任务队列不可读。</p>';
      return;
    }
    if (!items.length) {
      box.innerHTML = '<p class="muted">还没有任务。提交后会出现在这里。</p>';
      return;
    }
    box.innerHTML = items.slice(0, 6).map((t) => C.taskRow(normalizeTask(t))).join('');
    bindTaskRowActions(box);
  }

  /* 后端任务的字段名与前端 taskRow 期望的不完全一致，这里做一次窄适配 */
  function normalizeTask(t) {
    const statusMap = {
      running: 'running', pending: 'pending', success: 'success',
      failed: 'failed', canceled: 'canceled',
    };
    // ⚠️ 进度必须取后端解析出的真实值，不能 `status === 'running' ? 50 : 0` 拍常数
    //    （那是「进度条一直 50% 不动」的元凶之一）。null = 总量未知，渲染不确定态。
    const pg = t.progress_info || {};
    return {
      id: t.id,
      // 批量直链任务用「N 个链接」当标题：只显示第 1 条 URL 会让人以为
      // 另外几条没提交成功，然后重复提交。
      title: t.title || (t.count > 1 ? ('批量下载 · ' + t.count + ' 个链接') : (t.url || '（无标题）')),
      url: t.url || '',
      // 直链任务：后端从日志里取回的「真实直链」。复制按钮认它，不回放取件码地址。
      resolved_urls: t.resolved_urls || [],
      status: statusMap[t.status] || 'pending',
      type: t.type || 'video',
      mode: t.kind === 'direct' ? '直链' : t.kind === 'kuaishou' ? '快手主页' : '抖音',
      createdAt: t.created_at || t.createdAt || new Date().toISOString(),
      total: pg.total || 0,
      success: pg.done || 0,
      failed: pg.failed || 0,
      skipped: pg.skipped || 0,
      paid: pg.paid || 0,
      progress: (pg.progress === null || pg.progress === undefined) ? null : pg.progress,
      stage: pg.stage || '',
      note: pg.note || '',
      tail: pg.tail || [],
      elapsed: pg.elapsed || 0,
      source: 'bridge-live',
      error: t.error || '',
      // 输出目录（按 kind 从工具产物目录取）与目录名
      outputDir: t.output_dir || null,
      outputName: t.output_name || null,
    };
  }

  function bindTaskRowActions(box) {
    box.querySelectorAll('[data-act="cancel"]').forEach((b) => {
      b.addEventListener('click', async () => {
        try { await window.DYApi.cancelTask(b.dataset.id); C.toast('已请求取消', 'ok'); }
        catch (e) { C.toast(e.message || '取消失败', 'err'); }
      });
    });
    box.querySelectorAll('[data-act="remove"]').forEach((b) => {
      b.addEventListener('click', () => {
        const row = box.querySelector('[data-task-row="' + b.dataset.id + '"]');
        if (row) row.remove();
      });
    });
    box.querySelectorAll('[data-act="copy"]').forEach((b) => {
      b.addEventListener('click', () => {
        // 用按钮上带的 data-copy，不再抓行里那个 .mono 文本 ——
        // 行里显示的是**截断过的** url（U.truncate 52），复制出去就是个坏链接。
        const text = b.dataset.copy || '';
        if (!text) { C.toast('这个任务没有可复制的链接', 'err'); return; }
        U.copyText(text);
        C.toast('已复制到剪贴板', 'ok');
      });
    });
    box.querySelectorAll('[data-act="opendir"]').forEach((b) => {
      b.addEventListener('click', () => {
        window.DYApi.openPath(b.dataset.openpath || '')
          .then(() => C.toast('已打开输出目录', 'ok'))
          .catch((e) => C.toast(e.message || '打开失败', 'err'));
      });
    });
  }

  /* 直链"只解析"的返回：没有任务行可展示，直接落一段等宽输出 */
  function renderInlineLog(view, output) {
    const box = view.querySelector('#lt-recent');
    if (!box) return;
    if (!output) { box.innerHTML = '<p class="muted">解析已完成，但后端没有返回文本输出。</p>'; return; }
    box.innerHTML = '<pre class="lt-out">' + esc(output) + '</pre>';
  }

  /* 支持 #/launch 与 #/launch/<id> 两种 hash：路由只需注册 'launch'，
   * 页面自己读完整 hash 决定渲染哪个子视图（router.parse 会按第一段回退）。
   * 注意不要把各工具的 id 抢注册成本页路由 —— douyin 已有独立工作台页面，
   * 抢占会让顶栏「抖音下载」跳到启动台，造成入口错位。 */
  window.DYRouter.register('launch', render);
})();
