/* ============================================================
 * pages/queue.js — 多链排队
 * ------------------------------------------------------------
 * 粘贴多行链接 → 解析表格（类型识别/去重）→ 一键加入队列
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  let parsed = [];

  function render(view) {
    view.className = 'content';
    delete view.dataset.platform;
    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div><div class="crumb"><span class="crumb__dot"></span>下载与管理 · 多链排队</div>' +
        '<h1 class="page-title">多链排队</h1></div>' +
      '</div>' +
      '<p class="small muted mb-4">一次粘贴多条链接（每行一条，或直接粘贴整段分享文本），' +
      '解析去重后按顺序加入下载队列。</p>' +

      '<section class="section section--flush">' +
        '<textarea class="input" id="qu-input" rows="7" spellcheck="false" ' +
        'placeholder="https://www.douyin.com/video/…&#10;https://v.douyin.com/…&#10;https://www.douyin.com/user/…&#10;（每行一条，支持混合平台）" ' +
        'aria-label="批量链接输入"></textarea>' +
        '<div class="flex mt-4">' +
          '<button class="btn btn--primary" id="qu-parse">' + I.icon('bolt', 15) + '解析链接</button>' +
          '<button class="btn btn--ghost" id="qu-clear">' + esc(window.DYConfig.TEXT.clearInput) + '</button>' +
          '<span class="small faint right" id="qu-hint"></span>' +
        '</div>' +
      '</section>' +

      '<div id="qu-result"></div>';

    view.querySelector('#qu-parse').addEventListener('click', doParse);
    view.querySelector('#qu-clear').addEventListener('click', () => {
      view.querySelector('#qu-input').value = '';
      parsed = [];
      renderResult();
    });
    view.querySelector('#qu-result').addEventListener('click', onResultAction);
  }

  function doParse() {
    const raw = document.getElementById('qu-input').value;
    const urls = U.extractUrls(raw);
    const seen = {};
    parsed = [];
    urls.forEach((u) => {
      const key = u.replace(/\/$/, '');
      if (seen[key]) return;
      seen[key] = true;
      const exists = window.DYStore.state.tasks.some((t) => t.url.replace(/\/$/, '') === key);
      parsed.push({ url: u, type: U.guessUrlType(u), dup: exists });
    });
    renderResult();
    if (!urls.length) C.toast('未识别到任何链接，请检查内容', 'err');
  }

  function renderResult() {
    const box = document.getElementById('qu-result');
    if (!box) return;
    if (!parsed.length) {
      box.innerHTML = C.empty('queue', 'layers');
      return;
    }
    const bad = parsed.filter((p) => p.type === 'unknown').length;
    const dups = parsed.filter((p) => p.dup).length;
    box.innerHTML =
      '<div class="flex mb-3">' +
        '<span class="small muted">共解析 <b class="num">' + parsed.length + '</b> 条' +
        (dups ? ' · <span style="color:var(--amber)">' + dups + ' 条已在任务中</span>' : '') +
        (bad ? ' · <span style="color:var(--red)">' + bad + ' 条类型未知</span>' : '') +
        '</span>' +
        '<button class="btn btn--primary btn--sm right" id="qu-enqueue"' + (dups === parsed.length ? ' disabled' : '') + '>' +
        I.icon('queueAdd', 14) + '加入队列（' + (parsed.length - dups) + '）</button>' +
      '</div>' +
      '<div class="table-wrap"><table class="table"><thead><tr>' +
      '<th style="width:44px">#</th><th>链接</th><th>识别类型</th><th>状态</th><th style="width:70px"></th>' +
      '</tr></thead><tbody>' +
      parsed.map((p, i) => {
        const hint = window.DYConfig.URL_TYPE_HINT[p.type] || window.DYConfig.URL_TYPE_HINT.unknown;
        return '<tr>' +
          '<td class="num faint">' + (i + 1) + '</td>' +
          '<td><code class="mono small ellipsis" style="max-width:420px;display:inline-block" title="' + esc(p.url) + '">' +
          esc(U.truncate(p.url, 64)) + '</code></td>' +
          '<td>' + esc(hint.label) + '</td>' +
          '<td>' + (p.dup
            ? '<span class="badge badge--amber">已在队列</span>'
            : p.type === 'unknown'
              ? '<span class="badge badge--red">类型未知</span>'
              : '<span class="badge badge--green">可添加</span>') + '</td>' +
          '<td><button class="btn btn--ghost btn--sm btn--icon" data-act="del" data-i="' + i + '" aria-label="移除该条">' +
          I.icon('close', 13) + '</button></td>' +
          '</tr>';
      }).join('') +
      '</tbody></table></div>';

    document.getElementById('qu-enqueue').addEventListener('click', () => {
      const addable = parsed.filter((p) => !p.dup);
      if (!addable.length) return;
      addable.forEach((p, i) => window.DYStore.createTask({
        url: p.url, platform: platformOf(p.url), type: p.type,
        title: '多链排队 #' + (i + 1), mode: 'queue',
      }));
      C.toast('已加入队列 ' + addable.length + ' 条任务', 'ok');
      parsed = [];
      document.getElementById('qu-input').value = '';
      renderResult();
      window.DYApp.updateTaskBadge();
    });
  }

  function platformOf(url) {
    if (/tiktok\.com/i.test(url)) return 'tiktok';
    if (/youtube\.com|youtu\.be/i.test(url)) return 'youtube';
    return 'douyin';
  }

  function onResultAction(e) {
    const btn = e.target.closest('[data-act="del"]');
    if (!btn) return;
    parsed.splice(parseInt(btn.dataset.i, 10), 1);
    renderResult();
  }

  window.DYRouter.register('queue', render);
})();
