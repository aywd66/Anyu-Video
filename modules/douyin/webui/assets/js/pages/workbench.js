/* ============================================================
 * pages/workbench.js — 平台工作台（抖音 / TikTok / YouTube 共用模板）
 * ------------------------------------------------------------
 * 页面结构（对照参考截图）：
 *   面包屑 → 大标题（尾句带平台强调色） → 说明
 *   → 主输入卡（URL 标签 + 输入组 + 识别状态行）
 *   → 本次设置卡（模式/数量/画质等，仅影响本次任务）
 *   → 最近任务（该平台；空态带引导）
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  /* 每个平台记住“本次设置”（会话内保持，不持久化——与真实后端的 overrides 对应） */
  const runOptions = {
    douyin: { modes: ['post'], number: 0, quality: 'highest', increase: true, ytFormat: 'video' },
    tiktok: { modes: ['post'], number: 0, quality: 'highest', increase: true, ytFormat: 'video' },
    youtube: { modes: ['post'], number: 0, quality: 'highest', increase: true, ytFormat: 'video' },
  };

  function render(view, platformId) {
    const cfg = window.DYConfig;
    const P = cfg.PLATFORMS[platformId];
    const opt = runOptions[platformId];
    view.className = 'content acc-' + P.accent;
    view.dataset.platform = platformId;

    view.innerHTML =
      /* —— 页头 —— */
      '<div class="crumb"><span class="crumb__dot"></span>' + esc(P.crumb) + '</div>' +
      '<div class="page-head">' +
        '<h1 class="page-title">' + esc(P.titleA) + '<span class="accent">' + esc(P.titleB) + '</span></h1>' +
        '<p class="page-sub">' + esc(P.sub) + '</p>' +
      '</div>' +

      /* —— 主输入卡 —— */
      '<section class="card card--hero mb-5" aria-label="链接输入">' +
        '<div class="card__label"><span class="card__label-dot"></span>' + esc(P.cardLabel) + '</div>' +
        '<div class="card__title">' + esc(P.cardTitle || '下载链接') + '</div>' +
        '<div class="input-group mt-4">' +
          '<input class="input" id="wb-url" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="' + esc(P.placeholder) + '" aria-label="下载链接">' +
          '<button class="btn btn--primary btn--lg" id="wb-go">' + I.icon('download', 18) + esc(cfg.TEXT.download) + '</button>' +
        '</div>' +
        '<div class="flex mt-3 small" id="wb-status" style="gap:8px;min-height:20px"></div>' +
        '<div class="flex mt-2" style="gap:8px">' +
          '<button class="btn btn--ghost btn--sm" id="wb-sample">' + I.icon('link', 13) + '填入示例链接</button>' +
          '<span class="small muted" style="align-self:center">' + esc(P.tips) + '</span>' +
        '</div>' +
      '</section>' +

      /* —— 本次设置卡 —— */
      '<section class="card mb-5" aria-label="本次下载设置">' +
        '<div class="flex mb-4"><div><div class="card__title" style="margin:0">本次下载设置</div>' +
        '<p class="small faint mt-2" style="margin:0">只影响本次任务；长期默认值请到「设置」页修改。</p></div></div>' +
        '<div id="wb-options"></div>' +
      '</section>' +

      /* —— 最近任务 —— */
      '<section aria-label="最近任务">' +
        '<div class="flex mb-4"><h2 class="card__title" style="margin:0">最近任务</h2>' +
        '<button class="btn btn--ghost btn--sm right" data-go="tasks">全部任务' + I.icon('chevronRight', 14) + '</button></div>' +
        '<div id="wb-recent"></div>' +
      '</section>';

    renderOptions(view, platformId, opt);
    renderRecent(view, platformId);
    if (window.DYApi.isBridge() && platformId !== 'douyin') {
      // 桥接模式能力边界：CLI 仅覆盖抖音
      view.querySelector('#wb-go').disabled = true;
      view.querySelector('#wb-status').innerHTML =
        '<span class="dot dot--amber"></span><span class="muted">桥接模式当前仅支持抖音链接下载；TikTok / YouTube 请使用官方桌面端</span>';
    }

    /* —— 交互绑定 —— */
    const input = view.querySelector('#wb-url');
    const statusEl = view.querySelector('#wb-status');
    const goBtn = view.querySelector('#wb-go');

    const updateStatus = () => {
      const urls = U.extractUrls(input.value);
      if (!urls.length) {
        statusEl.innerHTML = input.value.trim()
          ? '<span class="dot dot--amber"></span><span class="muted">未识别到 http(s) 链接，请检查粘贴内容</span>'
          : '<span class="dot dot--faint"></span><span class="faint">' + esc(P.accepts) + '</span>';
        return [];
      }
      if (urls.length === 1) {
        const t = U.guessUrlType(urls[0]);
        const hint = cfg.URL_TYPE_HINT[t] || cfg.URL_TYPE_HINT.unknown;
        statusEl.innerHTML =
          '<span class="dot dot--green"></span><span>' + esc(cfg.TEXT.recognized) + ': <b>' +
          esc(hint.label) + '</b> · ' + esc(hint.desc) + '</span><code class="mono faint ellipsis">' +
          esc(U.truncate(urls[0], 56)) + '</code>';
      } else {
        statusEl.innerHTML =
          '<span class="dot dot--green"></span><span>已识别 <b class="num">' + urls.length +
          '</b> 条链接 · 将按顺序加入「多链排队」</span>';
      }
      return urls;
    };
    input.addEventListener('input', U.debounce(updateStatus, 120));

    view.querySelector('#wb-sample').addEventListener('click', () => {
      input.value = P.sample;
      updateStatus();
      input.focus();
    });

    view.querySelectorAll('[data-go]').forEach((b) =>
      b.addEventListener('click', () => window.DYRouter.go(b.dataset.go)));

    // 统一输入口的接力：工作台首页粘贴的链接带过来，自动填入并触发识别。
    // 用完即删，避免用户下次手动进工作台时被上次的链接吓一跳。
    if (window.DYPrefill && window.DYPrefill.url) {
      input.value = window.DYPrefill.url;
      delete window.DYPrefill;
      updateStatus();
      input.focus();
    }

    goBtn.addEventListener('click', async () => {
      const urls = updateStatus();
      if (!urls.length) {
        input.classList.add('is-invalid');
        C.toast('请先粘贴有效的下载链接', 'err');
        setTimeout(() => input.classList.remove('is-invalid'), 1200);
        input.focus();
        return;
      }
      goBtn.disabled = true;
      try {
        if (window.DYApi.isBridge() && platformId !== 'douyin') {
          C.toast('桥接模式当前仅支持抖音链接；TikTok/YouTube 请使用官方桌面端', 'err');
          return;
        }
        if (window.DYApi.isBridge()) {
          // 真实下载：逐条提交，桥接服务内部排队（并发 2）
          const bridgeOpts = { modes: opt.modes, number: opt.number, quality: opt.quality, increase: opt.increase };
          for (let i = 0; i < urls.length; i++) {
            await window.DYApi.submitDownload({ url: urls[i], platform: 'douyin', options: bridgeOpts });
          }
          C.toast(urls.length > 1
            ? '已提交 ' + urls.length + ' 条真实下载任务（自动排队）'
            : '已提交真实下载任务，到任务中心查看进度', 'ok');
        } else if (urls.length === 1) {
          const url = urls[0];
          const type = U.guessUrlType(url);
          const t = await window.DYApi.submitDownload({
            url, platform: platformId, type,
            title: titleFor(url, type), mode: modeLabel(opt),
            total: opt.number || undefined,
          });
          C.toast('已添加任务：' + U.truncate(t.title, 24), 'ok');
        } else {
          urls.forEach((url, i) => window.DYStore.createTask({
            url, platform: platformId, type: U.guessUrlType(url),
            title: '批量链接 #' + (i + 1), mode: modeLabel(opt),
          }));
          C.toast('已加入队列 ' + urls.length + ' 条，到任务中心查看进度', 'ok');
        }
        input.value = '';
        updateStatus();
        await renderRecent(view, platformId);
        window.DYApp.updateTaskBadge();
      } catch (e) {
        C.toast('添加失败：' + (e.message || e), 'err');
      } finally {
        goBtn.disabled = false;
      }
    });
    /* 回车即下载 */
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') goBtn.click(); });

    /* 任务行内按钮（取消/重试/移除/复制） */
    view.querySelector('#wb-recent').addEventListener('click', onTaskAction);
  }

  function titleFor(url, type) {
    const map = {
      video: '单个作品下载', note: '图文作品下载', collection: '合集下载',
      music: '音乐下载', live: '直播录制', user: '作者主页批量', short: '短链解析',
      unknown: '链接下载',
    };
    return map[type] || '链接下载';
  }

  function modeLabel(opt) {
    if (window.DYRouter.current === 'youtube') {
      const f = window.DYConfig.YT_FORMAT.find((x) => x.id === opt.ytFormat);
      return f ? f.label : '视频 MP4';
    }
    return opt.modes.join('+') || 'post';
  }

  /* —— 本次设置表单 —— */
  function renderOptions(view, platformId, opt) {
    const cfg = window.DYConfig;
    const P = cfg.PLATFORMS[platformId];
    const box = view.querySelector('#wb-options');
    let html = '';

    if (P.options.includes('mode')) {
      html +=
        '<div class="form-item"><div class="form-item__label">下载模式（主页链接生效）</div><div class="chips" id="opt-modes">' +
        cfg.MODES.map((m) =>
          '<button class="chip' + (opt.modes.includes(m.id) ? ' is-on' : '') + '" data-mode="' + esc(m.id) +
          '" title="' + esc(m.desc) + '">' + esc(m.label) + '</button>'
        ).join('') +
        '</div><div class="form-item__desc" id="opt-mode-desc"></div></div>';
    }
    if (P.options.includes('ytFormat')) {
      html +=
        '<div class="form-item"><div class="form-item__label">下载格式</div><div class="chips" id="opt-ytfmt">' +
        cfg.YT_FORMAT.map((f) =>
          '<button class="chip' + (opt.ytFormat === f.id ? ' is-on' : '') + '" data-ytfmt="' + esc(f.id) + '">' +
          esc(f.label) + '</button>'
        ).join('') + '</div></div>';
    }
    html += '<div class="grid grid--2">';
    if (P.options.includes('number')) {
      html +=
        '<div class="form-item" style="margin:0"><div class="form-item__label">数量上限' +
        '<span class="form-item__desc" style="margin:0">0 = 全部</span></div>' +
        '<input class="input" type="number" id="opt-number" min="0" max="9999" step="1" value="' + opt.number + '"></div>';
    }
    if (P.options.includes('quality')) {
      html +=
        '<div class="form-item" style="margin:0"><div class="form-item__label">视频画质</div>' +
        '<select class="input" id="opt-quality">' +
        cfg.QUALITY.map((q) => '<option value="' + esc(q.id) + '"' + (opt.quality === q.id ? ' selected' : '') + '>' +
          esc(q.label) + '</option>').join('') +
        '</select></div>';
    }
    html += '</div>';
    if (P.options.includes('increase')) {
      html +=
        '<div class="flex mt-4"><div><div class="small" style="font-weight:600">增量下载</div>' +
        '<div class="small faint">跳过已下载过的作品，只抓新增</div></div>' +
        '<span class="right">' + C.switchHtml(opt.increase, 'id="opt-increase"') + '</span></div>';
    }
    box.innerHTML = html;

    /* chips 交互 */
    const modeDesc = () => {
      const el = box.querySelector('#opt-mode-desc');
      if (!el) return;
      const sel = cfg.MODES.filter((m) => opt.modes.includes(m.id));
      el.textContent = sel.length ? sel.map((m) => m.label + '：' + m.desc).join('；')
        : '至少选择一种模式';
    };
    modeDesc();

    box.querySelectorAll('[data-mode]').forEach((chip) => {
      chip.addEventListener('click', () => {
        const id = chip.dataset.mode;
        const solo = id === 'collect' || id === 'collectmix';
        if (solo) {
          // 收藏夹/收藏合集必须单独使用（与后端规则一致）
          opt.modes = opt.modes.includes(id) ? [] : [id];
        } else {
          opt.modes = opt.modes.filter((m) => m !== 'collect' && m !== 'collectmix');
          opt.modes = opt.modes.includes(id) ? opt.modes.filter((m) => m !== id) : opt.modes.concat(id);
        }
        if (!opt.modes.length) opt.modes = ['post'];
        box.querySelectorAll('[data-mode]').forEach((c) =>
          c.classList.toggle('is-on', opt.modes.includes(c.dataset.mode)));
        modeDesc();
      });
    });
    box.querySelectorAll('[data-ytfmt]').forEach((chip) => {
      chip.addEventListener('click', () => {
        opt.ytFormat = chip.dataset.ytfmt;
        box.querySelectorAll('[data-ytfmt]').forEach((c) =>
          c.classList.toggle('is-on', c.dataset.ytfmt === opt.ytFormat));
      });
    });
    const numInput = box.querySelector('#opt-number');
    if (numInput) numInput.addEventListener('change', () => {
      opt.number = Math.max(0, Math.min(9999, parseInt(numInput.value, 10) || 0));
      numInput.value = opt.number;
    });
    const qSel = box.querySelector('#opt-quality');
    if (qSel) qSel.addEventListener('change', () => { opt.quality = qSel.value; });
    const incSw = box.querySelector('#opt-increase');
    if (incSw) incSw.addEventListener('click', () => {
      opt.increase = opt.increase !== true;
      incSw.setAttribute('aria-checked', String(opt.increase));
    });
  }

  /* —— 最近任务（bridge：真实历史 + 实时任务；mock：本地任务） —— */
  async function renderRecent(view, platformId) {
    const box = view.querySelector('#wb-recent');
    if (!box) return;
    if (window.DYApi.isBridge()) {
      try {
        const feed = await window.DYApi.taskFeed();
        const items = feed.live.concat(feed.history).slice(0, 4);
        box.innerHTML = items.length
          ? '<div class="grid" style="gap:12px">' + items.map((t) => C.taskRow(t)).join('') + '</div>'
          : C.empty(platformId, 'download');
        return;
      } catch (e) {
        box.innerHTML = C.empty(platformId, 'download');
        return;
      }
    }
    const tasks = window.DYStore.state.tasks.filter((t) => t.platform === platformId).slice(0, 4);
    box.innerHTML = tasks.length
      ? '<div class="grid" style="gap:12px">' + tasks.map((t) => C.taskRow(t)).join('') + '</div>'
      : C.empty(platformId, 'download');
  }

  /* —— 任务行操作（与任务中心共用一套行为） —— */
  function onTaskAction(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    const store = window.DYStore;
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
    const view = document.getElementById('page-view');
    renderRecent(view, view.dataset.platform);
    window.DYApp.updateTaskBadge();
  }

  /* —— 注册三个平台路由 —— */
  ['douyin', 'tiktok', 'youtube'].forEach((pid) => {
    window.DYRouter.register(pid, (view) => {
      render(view, pid);
      window.DYApp.updatePlatformUI(pid);
      window.DYUtils.bus.emit('platform:changed', pid);
    });
  });
})();
