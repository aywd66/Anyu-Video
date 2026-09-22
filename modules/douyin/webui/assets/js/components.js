/* ============================================================
 * components.js — 可复用 UI 组件（纯函数渲染，返回 HTML 字符串）
 * ------------------------------------------------------------
 * ★ 页面一律用这里的方法拼 UI，不要手写重复结构。
 *   所有动态文本必须经过 esc() 转义后再进 HTML。
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const esc = U.esc;

  const C = {
    esc,

    /* —— 品牌 Logo（几何猫 + 字标） —— */
    brand() {
      return (
        '<div class="brand">' +
        '<div class="brand__mark">' + I.icon('cat', 24) + '</div>' +
        '<div class="brand__word"><span class="brand__env">' + esc(window.DYConfig.APP.envLabel) +
        '</span>' + esc(window.DYConfig.APP.name) + '</div>' +
        '</div>'
      );
    },

    /* —— 徽章 —— */
    badge(text, tone) {
      return '<span class="badge' + (tone ? ' badge--' + tone : '') + '">' + esc(text) + '</span>';
    },

    dot(tone, pulse) {
      return '<span class="dot dot--' + (tone || 'faint') + (pulse ? ' dot--pulse' : '') + '"></span>';
    },

    /* —— 任务状态徽章 —— */
    taskStatus(status) {
      const map = window.DYConfig.TASK_STATUS;
      const s = map[status] || map.pending;
      return C.badge(s.label, s.tone === 'muted' || s.tone === 'faint' ? '' : s.tone);
    },

    /* —— 媒体类型徽章 —— */
    mediaType(type) {
      const map = { video: ['视频', 'cyan'], note: ['图文', 'pink'], collection: ['合集', 'amber'], music: ['音乐', 'green'], live: ['直播', 'red'] };
      const m = map[type] || [type || '未知', ''];
      return C.badge(m[0], m[1]);
    },

    /* —— 作者头像（首字色块） —— */
    avatar(name, colorIndex, size) {
      const ch = (String(name || '?').replace(/[\u{1F000}-\u{1FAFF}\u2600-\u27BF\uFE0F\u200D]/gu, '').trim()[0] || '?').toUpperCase();
      return (
        '<span class="avatar' + (size ? ' avatar--' + size : '') + '" data-c="' + ((colorIndex || 0) % 6) +
        '" aria-hidden="true">' + esc(ch) + '</span>'
      );
    },

    /* —— 作品封面（真实封面 URL / 本地缩略图 / 加载失败自动降级 duotone） —— */
    cover(work, cls) {
      const idx = (work.cover || 0) % 6;
      let thumb;
      if (work.coverPath) thumb = work.coverPath;                      // bridge: 真实 CDN 封面
      else if (work.cover !== undefined && work.cover !== null) {
        thumb = 'assets/img/thumbs/thumb-' + ((work.cover || 0) % 12) + '.jpg';  // mock: 本地缩略图
      } else thumb = '';
      const inner = thumb
        ? '<img src="' + esc(thumb) + '" alt="" loading="lazy" ' +
          'onerror="this.parentElement.classList.add(\'cover--ph\');this.remove()">'
        : '';
      return (
        '<div class="cover' + (cls ? ' ' + cls : '') + (thumb ? '' : ' cover--ph') + '" data-c="' + idx + '">' +
        inner +
        (work.type === 'note' ? '<span class="cover__type">' + C.badge('图文', 'pink') + '</span>' : '') +
        (work.durationSec ? '<span class="cover__dur">' + U.duration(work.durationSec) + '</span>' : '') +
        '</div>'
      );
    },

    /* —— 空状态 —— */
    empty(sceneKey, iconName) {
      const cfg = window.DYConfig.EMPTY[sceneKey] || { title: '暂无内容', desc: '' };
      return (
        '<div class="empty">' +
        '<div class="empty__icon">' + I.icon(iconName || 'info', 34) + '</div>' +
        '<div class="empty__title">' + esc(cfg.title) + '</div>' +
        '<div class="empty__desc">' + esc(cfg.desc) + '</div>' +
        '</div>'
      );
    },

    /* —— 加载骨架（n 行） —— */
    skeletonRows(n) {
      let out = '';
      for (let i = 0; i < (n || 3); i++) out += '<div class="skeleton skeleton--row"></div>';
      return '<div aria-busy="true">' + out + '</div>';
    },

    /* —— 进度条 —— */
    progress(pct, tone) {
      const p = Math.max(0, Math.min(100, pct || 0));
      return (
        '<div class="progress" role="progressbar" aria-valuenow="' + p + '" aria-valuemin="0" aria-valuemax="100">' +
        '<div class="progress__bar' + (tone ? ' progress__bar--' + tone : '') + '" style="width:' + p + '%"></div>' +
        '</div>'
      );
    },

    /* —— 开关 —— */
    switchHtml(checked, attrs) {
      return (
        '<button type="button" class="switch" role="switch" aria-checked="' + (checked ? 'true' : 'false') +
        '" ' + (attrs || '') + '></button>'
      );
    },

    /* —— 弹窗：open(html, {wide}) 返回关闭函数 —— */
    open(innerHtml, opts) {
      this.close();
      const mask = document.createElement('div');
      mask.className = 'modal-mask';
      mask.innerHTML =
        '<div class="modal' + (opts && opts.wide ? ' modal--wide' : '') + '" role="dialog" aria-modal="true">' +
        innerHtml + '</div>';
      mask.addEventListener('mousedown', (e) => {
        if (e.target === mask) this.close();
      });
      document.body.appendChild(mask);
      const escBtn = mask.querySelector('[data-modal-close]');
      if (escBtn) escBtn.addEventListener('click', () => this.close());
      this._onKey = (e) => { if (e.key === 'Escape') this.close(); };
      document.addEventListener('keydown', this._onKey);
      return () => this.close();
    },

    close() {
      const m = document.querySelector('.modal-mask');
      if (m) m.remove();
      if (this._onKey) document.removeEventListener('keydown', this._onKey);
    },

    modalFrame(title, bodyHtml, footHtml, wide) {
      return (
        '<div class="modal__head"><div class="modal__title">' + esc(title) + '</div>' +
        '<button class="btn btn--ghost btn--icon" data-modal-close aria-label="关闭">' + I.icon('close', 18) + '</button></div>' +
        '<div class="modal__body">' + bodyHtml + '</div>' +
        (footHtml ? '<div class="modal__foot">' + footHtml + '</div>' : '')
      ).replace('"modal' + (wide ? ' modal--wide' : '') + '"', '"modal' + (wide ? ' modal--wide' : '') + '"');
    },

    /* —— 提示条 —— */
    toast(msg, type) {
      let wrap = document.querySelector('.toast-wrap');
      if (!wrap) {
        wrap = document.createElement('div');
        wrap.className = 'toast-wrap';
        document.body.appendChild(wrap);
      }
      const t = document.createElement('div');
      t.className = 'toast' + (type === 'err' ? ' toast--err' : type === 'ok' ? ' toast--ok' : '');
      t.innerHTML = I.icon(type === 'err' ? 'circleX' : type === 'ok' ? 'circleCheck' : 'info', 16) + esc(msg);
      wrap.appendChild(t);
      setTimeout(() => {
        t.style.transition = 'opacity .3s';
        t.style.opacity = '0';
        setTimeout(() => t.remove(), 320);
      }, 2400);
    },

    /* —— 统计块 —— */
    stats(items) {
      return (
        '<div class="stats">' +
        items.map((it) =>
          '<div class="stat"><div class="stat__label">' + esc(it.label) + '</div>' +
          '<div class="stat__value' + (it.color ? '" style="color:var(--' + it.color + ')' : '') + '">' +
          esc(it.value) + '</div>' +
          (it.hint ? '<div class="stat__hint">' + esc(it.hint) + '</div>' : '') +
          '</div>'
        ).join('') +
        '</div>'
      );
    },

    /* —— Tab 组 —— */
    tabs(items, activeId, attrs) {
      return (
        '<div class="tabs" role="tablist" ' + (attrs || '') + '>' +
        items.map((t) =>
          '<button class="tab' + (t.id === activeId ? ' is-on' : '') + '" role="tab" ' +
          'aria-selected="' + (t.id === activeId) + '" data-tab="' + esc(t.id) + '">' +
          esc(t.label) + (t.count !== undefined ? '&nbsp;<span class="num">' + esc(t.count) + '</span>' : '') +
          '</button>'
        ).join('') +
        '</div>'
      );
    },

    /* —— 长文本安全展示（超长标题截断 + title 提示） —— */
    ellipsisTitle(text, max) {
      const t = String(text || '');
      const shown = U.truncate(t, max || 60);
      return '<span class="ellipsis" title="' + esc(t) + '">' + esc(shown) + '</span>';
    },

    /* —— 任务行（工作台最近任务 / 任务中心共用） —— */
    taskRow(t, opts) {
      const o = opts || {};
      const st = window.DYConfig.TASK_STATUS[t.status] || window.DYConfig.TASK_STATUS.pending;
      const U = window.DYUtils;
      const counts = t.total > 0
        ? '成功 <b class="num">' + t.success + '</b>' + (t.failed ? ' · 失败 <b class="num" style="color:var(--red)">' + t.failed + '</b>' : '') + (t.skipped ? ' · 跳过 <b class="num">' + t.skipped + '</b>' : '')
        : '等待开始';
      const actions = [];
      if (t.status === 'running' || t.status === 'pending') {
        actions.push('<button class="btn btn--ghost btn--sm" data-act="cancel" data-id="' + esc(t.id) + '">' + I.icon('stop', 14) + '取消</button>');
      }
      if (t.status === 'failed' || t.status === 'canceled') {
        actions.push('<button class="btn btn--ghost btn--sm" data-act="retry" data-id="' + esc(t.id) + '">' + I.icon('restart', 14) + '重试</button>');
      }
      actions.push(
        '<button class="btn btn--ghost btn--sm" data-act="copy" data-id="' + esc(t.id) + '">' + I.icon('copy', 14) + '</button>' +
        '<button class="btn btn--ghost btn--sm btn--icon" data-act="remove" data-id="' + esc(t.id) + '" aria-label="移除任务">' + I.icon('trash', 14) + '</button>'
      );
      return (
        '<div class="row-item" data-task-row="' + esc(t.id) + '">' +
          '<div class="row-item__main">' +
            '<div class="flex" style="gap:10px">' + C.taskStatus(t.status) +
              '<span class="row-item__title grow">' + C.ellipsisTitle(t.title, 46) + '</span></div>' +
            '<div class="row-item__meta">' + C.mediaType(t.type) +
              '<span>' + esc(t.mode || '-') + '</span>' +
              '<span class="mono">' + esc(U.truncate(t.url, 52)) + '</span>' +
              '<span>' + esc(U.fromNow(t.createdAt)) + '</span>' +
              '<span>' + counts + '</span></div>' +
            (t.error ? '<div class="row-item__meta" style="color:var(--red)">' + I.icon('alert', 13) + ' ' + esc(t.error) + '</div>' : '') +
            (t.status === 'running' ? '<div class="flex mt-2" style="gap:10px">' + C.progress(t.progress, 'cyan') + '<span class="num small muted">' + t.progress + '%</span></div>' : '') +
          '</div>' +
          '<div class="row-item__actions">' + actions.join('') + '</div>' +
        '</div>'
      );
    },

    /* —— 作品卡（作品档案 / 收藏页共用） —— */
    workCard(w) {
      return (
        '<div class="work-card" data-work-card="' + esc(w.id) + '">' +
          C.cover(w) +
          '<div class="work-card__body">' +
            '<div class="work-card__title">' + C.ellipsisTitle(w.title, 42) + '</div>' +
            '<div class="work-card__meta">' +
              '<span class="ellipsis">' + esc(w.creatorName || '未知作者') + '</span>' +
              '<span>' + esc(w.downloadTime ? U.fmtDate(w.downloadTime) : U.fmtDate(w.createTime)) + '</span>' +
              (w.sizeMB != null ? '<span class="num">' + esc(w.sizeMB) + ' MB</span>' : '') +
              (w.quality ? '<span>' + esc(w.quality) + '</span>' : '') +
            '</div>' +
          '</div>' +
          '<div class="work-card__actions">' +
            '<button class="btn btn--ghost btn--sm btn--icon" data-act="fav" data-id="' + esc(w.id) + '" aria-label="收藏">' +
              I.icon(w.fav ? 'heartFill' : 'heart', 15) + '</button>' +
            '<button class="btn btn--ghost btn--sm btn--icon" data-act="folder" data-id="' + esc(w.id) + '" aria-label="打开目录">' + I.icon('folder', 15) + '</button>' +
            '<button class="btn btn--ghost btn--sm btn--icon" data-act="path" data-id="' + esc(w.id) + '" aria-label="复制路径">' + I.icon('copy', 15) + '</button>' +
          '</div>' +
        '</div>'
      );
    },

    /* —— 确认弹窗 —— */
    confirm(title, desc, onOk, okLabel, danger) {
      const closeFn = this.open(
        this.modalFrame(
          title,
          '<p class="muted" style="margin:0">' + esc(desc) + '</p>',
          '<button class="btn btn--ghost" data-modal-close>取消</button>' +
          '<button class="btn ' + (danger ? 'btn--danger' : 'btn--primary') + '" id="__confirm_ok">' +
          esc(okLabel || '确认') + '</button>'
        )
      );
      document.getElementById('__confirm_ok').addEventListener('click', () => {
        closeFn();
        onOk && onOk();
      });
    },
  };

  window.DYComponents = C;
})();
