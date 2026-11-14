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
    cover(work, cls, attrs) {
      // 配色种子：mock 数据用 cover 序号；真实数据（快手台账没有封面图）用 id 的哈希
      // —— 否则一千多张卡会全部落在同一个色块上，列表看起来像没渲染出来。
      let seed;
      if (work.cover !== undefined && work.cover !== null) seed = work.cover;
      else {
        const s0 = String(work.id || work.awemeId || '');
        let h = 0;
        for (let i = 0; i < s0.length; i++) h = (h * 31 + s0.charCodeAt(i)) | 0;
        seed = Math.abs(h);
      }
      const idx = seed % 6;
      let thumb;
      if (work.coverPath) thumb = work.coverPath;                      // bridge: 真实 CDN 封面
      else if (work.cover !== undefined && work.cover !== null) {
        thumb = 'assets/img/thumbs/thumb-' + ((work.cover || 0) % 12) + '.jpg';  // mock: 本地缩略图
      } else thumb = '';
      const inner = thumb
        ? '<img src="' + esc(thumb) + '" alt="" loading="lazy" ' +
          'onerror="this.parentElement.classList.add(\'cover--ph\');this.remove()">'
        : '';
      // 本地有文件的作品：盖一层播放/图集入口 —— 让「点封面就能看」这件事看得出来，
      // 否则封面只是一张图，用户不会想到它可以点。（mock 数据没有 localKind，不会出现。）
      const kind = work.localKind;
      const tip = kind === 'video' ? '本地视频 · 点击播放'
        : kind === 'image' ? '本地图集 · 点击查看' : '';
      return (
        '<div class="cover' + (cls ? ' ' + cls : '') + (thumb ? '' : ' cover--ph') + '"' +
        (attrs || '') + ' data-c="' + idx + '"' + (tip ? ' title="' + esc(tip) + '"' : '') + '>' +
        inner +
        (kind
          ? '<span class="cover__play cover__play--' + kind + '">' +
            I.icon(kind === 'video' ? 'play' : 'image', kind === 'video' ? 24 : 20) +
            (kind === 'image' && work.localImages > 1
              ? '<span class="cover__count">' + work.localImages + ' 图</span>' : '') +
            '</span>'
          : '') +
        (work.type === 'note' ? '<span class="cover__type">' + C.badge('图文', 'pink') + '</span>' : '') +
        (work.durationSec ? '<span class="cover__dur">' + U.duration(work.durationSec) + '</span>' : '') +
        '</div>'
      );
    },

    /* —— 空状态 —— */
    empty(sceneKey, iconName, override) {
      // override 可选：同一个场景在 bridge / mock 两种模式下语义可能不同
      //（例如「收藏与喜欢」在 mock 下是账号收藏、在 bridge 下是本地收藏夹），
      // 空态文案必须跟着换，否则会指一条走不通的路。
      const cfg = Object.assign({}, window.DYConfig.EMPTY[sceneKey] || { title: '暂无内容', desc: '' },
                                override || {});
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
      // 弹窗里可能有多个 data-modal-close（头部 × 图标 + footer 的「关闭」文字按钮），
      // 而且有些弹窗的内容是**异步渲染**的（查找重复视频：扫描完成后才往
      // footer 里插入「关闭」）。逐元素绑定在打开那一刻做，之后被 innerHTML
      // 替换出来的新按钮就没有监听了 —— 表现为「关闭点了没反应」（2026-11-07
      // 实测踩坑）。改挂 mask 级事件委托：无论按钮何时插入，点它都能关。
      mask.addEventListener('click', (e) => {
        const t = e.target && e.target.closest ? e.target.closest('[data-modal-close]') : null;
        if (t) this.close();
      });
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

    /* —— 重试任务 ——
     * 三个页面（任务中心 / 工作台 / 工作台首页）都有一模一样的「重试」入口，
     * 结果提示也一模一样，所以收在这里一份。真正的重试在 api.retryTask 里：
     * bridge 模式会 POST /api/bridge/task/{id}/retry 重新起进程，
     * 直链批量只补失败的那几条（成功的几个 GB 不再下一遍）。 */
    retryTask(id) {
      const C = this;
      return window.DYApi.retryTask(id).then((d) => {
        C.toast(d && d.onlyFailed
          ? ('已重新排队：只补 ' + d.retried + ' 个失败的链接，成功的不会重下')
          : '已重新排队，到任务中心看进度', 'ok');
      }).catch((e) => {
        C.toast('重试失败：' + String((e && e.message) || e), 'err');
      });
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

    /* —— 任务计数文案 ——
     * ⚠️ 以前是 `total > 0 ? 统计 : '等待开始'`：真实桥接任务的 total 一直是 0，
     *    于是**下载完成后**仍显示「等待开始」，用户会读成「下载失败」。
     *    现在只要有任何一项计数就出统计；真没有时才按状态给一句能解释现状的话。 */
    taskCounts(t) {
      const bits = [];
      if (t.total > 0 || t.success > 0 || t.failed > 0 || t.skipped > 0 || t.paid > 0) {
        bits.push('成功 <b class="num">' + (t.success || 0) + '</b>');
        if (t.failed) bits.push('失败 <b class="num" style="color:var(--red)">' + (t.failed || 0) + '</b>');
        if (t.skipped) bits.push('跳过 <b class="num">' + (t.skipped || 0) + '</b>');
        // 付费/会员内容：抖音只给 CENC 密文，本地解不开，属预期内下不来。
        // 单列出来，避免用户把「注定下不了」读成「工具坏了」。
        if (t.paid) {
          bits.push('付费跳过 <b class="num" style="color:var(--bb-warning)" ' +
            'title="付费/会员内容，抖音下发的是加密流，本地无法播放">' + (t.paid || 0) + '</b>');
        }
        if (t.total) bits.push('共 <b class="num">' + t.total + '</b>');
        return bits.join(' · ');
      }
      if (t.status === 'braked') return t.brake_reason || '守卫主动刹车，未产出文件';
      if (t.status === 'success' || t.status === 'canceled') return '已结束，无文件产出';
      if (t.status === 'failed') return '未产出文件';
      if (t.status === 'pending') return '排队中，等待空闲槽位';
      return '正在准备…';
    },

    /* —— 不确定态进度条（总量未知时用，不给假百分比） —— */
    progressIndeterminate() {
      return '<div class="progress progress--indet" role="progressbar" aria-label="进行中，总量未定">' +
        '<div class="progress__bar progress__bar--cyan"></div></div>';
    },

    /* —— 任务进度条 ——
     * ⚠️ `progress === null` = 总量未知（还在收集清单、或工具没打进度行），
     *    此时渲染不确定态 + 阶段文字 + 已运行时长，**绝不能**拿常数冒充百分比 ——
     *    以前就写死 50%，于是「进度条一直 50% 不动」。 */
    taskProgress(t) {
      if (t.status !== 'running' && t.status !== 'pending') return '';
      const U2 = window.DYUtils;
      const p = (t.progress === null || t.progress === undefined)
        ? null : Math.max(0, Math.min(100, Math.round(t.progress)));
      const dur = t.elapsed ? ' · 已运行 ' + U2.duration(t.elapsed) : '';
      const label = p === null
        ? (t.stage || (t.status === 'pending' ? '排队中' : '进行中')) + dur
        : p + '%' + (t.stage ? ' · ' + t.stage : '') + dur;
      return '<div class="flex mt-2" style="gap:10px">' +
        (p === null ? C.progressIndeterminate() : C.progress(p, 'cyan')) +
        '<span class="num small muted" style="white-space:nowrap">' + esc(label) + '</span></div>';
    },

    /* —— 任务日志尾巴（「下载过程中什么都看不到」靠它解决） —— */
    taskLog(t) {
      if (!t.tail || !t.tail.length) return '';
      const U2 = window.DYUtils;
      return '<pre class="task-log" aria-label="最近日志">' +
        t.tail.map((l) => esc(U2.truncate(l, 110))).join('\n') + '</pre>';
    },

    /* —— 任务行（工作台最近任务 / 任务中心共用） —— */
    taskRow(t, opts) {
      const o = opts || {};
      const st = window.DYConfig.TASK_STATUS[t.status] || window.DYConfig.TASK_STATUS.pending;
      const U = window.DYUtils;
      const live = t.status === 'running' || t.status === 'pending';
      const actions = [];
      if (live) {
        actions.push('<button class="btn btn--ghost btn--sm" data-act="cancel" data-id="' + esc(t.id) + '">' + I.icon('stop', 14) + '取消</button>');
      }
      if (t.source === 'bridge-live') {
        actions.push('<button class="btn btn--ghost btn--sm" data-act="log" data-id="' + esc(t.id) + '">' + I.icon('file', 14) + '日志</button>');
      }
      if (t.status === 'failed' || t.status === 'canceled') {
        actions.push('<button class="btn btn--ghost btn--sm" data-act="retry" data-id="' + esc(t.id) + '">' + I.icon('restart', 14) + '重试</button>');
      }
      // 「复制」复制的是**真正能下的那条链接**。直链任务当初粘进去的常是
      // 取件码式的页面地址（/download/<id>），服务端扫描任务一过期就只剩
      // "scan task not found or expired"；后端已把 clawfetch 解析出的真实
      // 直链放在 resolved_urls 里，这里优先用它。
      const copyUrl = (t.resolved_urls && t.resolved_urls.length)
        ? t.resolved_urls.join('\n')
        : (t.url || '');
      const copyTip = (t.resolved_urls && t.resolved_urls.length)
        ? '复制真实下载链接（已解析中转页）' : '复制链接';
      /* 输出目录：本任务产物将落到/已落在的工具目录。名称 = 目录名；
       * 点文件夹图标直接打开（与工程下载历史的「打开目录」同源）。 */
      if (t.outputDir) {
        actions.unshift(
          '<span class="small faint" title="' + esc(t.outputDir) + '">' +
          I.icon('openFolder', 13) + ' ' + esc(t.outputName || '') + '</span>' +
          '<button class="btn btn--ghost btn--sm" data-act="opendir" data-openpath="' + esc(t.outputDir) + '" ' +
            'title="打开输出目录：' + esc(t.outputDir) + '">' +
            I.icon('openFolder', 14) + '打开目录</button>'
        );
      }
      actions.push(
        '<button class="btn btn--ghost btn--sm" data-act="copy" data-id="' + esc(t.id) +
          '" data-copy="' + esc(copyUrl) + '" title="' + esc(copyTip) + '" aria-label="' + esc(copyTip) + '">' +
          I.icon('copy', 14) + '</button>' +
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
              '<span>' + C.taskCounts(t) + '</span></div>' +
            (t.error ? '<div class="row-item__meta" style="color:var(--red)">' + I.icon('alert', 13) + ' ' + esc(t.error) + '</div>' : '') +
            C.taskProgress(t) +
            // 「已结束但一个文件都没下下来」最容易被读成失败（实际常是台账去重），
            // 这种行也把日志尾巴留下，让「无需下载：清单共 N，已下载 N」这类原文自己解释。
            // 守卫刹车同理 —— 而且更需要日志，因为原因（额度/租约/冷却）只有日志里才有。
            (live || (t.source === 'bridge-live'
                      && (t.status === 'braked' || (t.status === 'success' && !t.success)))
              ? C.taskLog(t) : '') +
          '</div>' +
          '<div class="row-item__actions">' + actions.join('') + '</div>' +
        '</div>'
      );
    },

    /* —— 作品卡（作品档案 / 收藏页共用） —— */
    workCard(w) {
      // 本地确实有文件才给播放入口：文件已被删/移走的作品照样显示卡片（记录还在），
      // 但不该给一个点了报错的按钮 —— 那才是真正的"骗点击"。
      const kind = w.localKind;
      const playAttrs = kind ? ' data-act="play" data-id="' + esc(w.id) + '" role="button" tabindex="0"' : '';
      return (
        '<div class="work-card" data-work-card="' + esc(w.id) + '">' +
          C.cover(w, kind ? 'cover--clickable' : '', playAttrs) +
          '<div class="work-card__body">' +
            '<div class="work-card__title">' + C.ellipsisTitle(w.title, 42) + '</div>' +
            '<div class="work-card__meta">' +
              '<span class="ellipsis">' + esc(w.creatorName || '未知作者') + '</span>' +
              '<span>' + esc(w.downloadTime ? U.fmtDate(w.downloadTime) : U.fmtDate(w.createTime)) + '</span>' +
              (w.sizeMB != null ? '<span class="num">' + esc(w.sizeMB) + ' MB</span>' : '') +
              (w.quality ? '<span>' + esc(w.quality) + '</span>' : '') +
              (kind === 'image' ? '<span>' + esc(String(w.localImages || 1)) + ' 张图</span>' : '') +
            '</div>' +
          '</div>' +
          '<div class="work-card__actions">' +
            (kind ? '<button class="btn btn--primary btn--sm btn--icon" data-act="play" data-id="' + esc(w.id) + '" aria-label="' +
              (kind === 'video' ? '播放' : '查看') + '">' + I.icon(kind === 'video' ? 'play' : 'image', 15) + '</button>' : '') +
            // 收藏是抖音侧的能力（工程库 + 本地收藏夹）。快手台账没有收藏概念，
            // 给一个点了没用的按钮不如不给。
            (w.platform === 'kuaishou' ? '' :
              '<button class="btn btn--ghost btn--sm btn--icon" data-act="fav" data-id="' + esc(w.id) + '" aria-label="收藏">' +
              I.icon(w.fav ? 'heartFill' : 'heart', 15) + '</button>') +
            '<button class="btn btn--ghost btn--sm btn--icon" data-act="folder" data-id="' + esc(w.id) + '" aria-label="打开目录">' + I.icon('folder', 15) + '</button>' +
            '<button class="btn btn--ghost btn--sm btn--icon" data-act="path" data-id="' + esc(w.id) + '" aria-label="复制路径">' + I.icon('copy', 15) + '</button>' +
          '</div>' +
        '</div>'
      );
    },

    /* —— 本地作品播放器：视频直接播，图集可翻页 ——
       为什么不在卡片里直接内嵌 <video>：一页 60 张卡，内嵌 60 个 <video> 会同时
       拉 60 个文件流，页面直接卡死；弹窗一次只开一路，播完即释放。
       src 指向 /api/bridge/media，由服务端把本地文件读出来（浏览器读不到 E 盘）。 */
    mediaPlayer(w) {
      const kind = w && w.localKind;
      if (!kind) {
        this.toast('本地未找到该作品的文件（可能已删除或移动）', 'err');
        return;
      }
      // 两个平台的文件流走各自的接口（抖音 /api/bridge/media、快手 /api/bridge/ks/media）。
      // 这里只按 platform 选出口，播放器本体完全共用 —— 用户看到的播放体验是一致的。
      const isKs = w.platform === 'kuaishou';
      const aid = w.awemeId || String(w.id || '').replace(/^(aw|ks)-/, '');
      const mkUrl = (i2, k2) => (isKs ? window.DYApi.ksMediaUrl(aid)
                                      : window.DYApi.mediaUrl(aid, i2, k2));
      const url = mkUrl(0, kind === 'image' ? 'image' : undefined);
      const nImg = Math.max(1, w.localImages || 1);
      const meta = [w.creatorName || '未知作者',
                    w.downloadTime || w.createTime || '',
                    w.sizeMB != null ? w.sizeMB + ' MB' : '',
                    w.filePath || ''].filter(Boolean).join(' · ');
      let idx = 0;

      const body = kind === 'video'
        ? '<div class="media-stage"><video id="mp-video" controls autoplay playsinline ' +
          'preload="metadata" src="' + esc(url) + '"></video></div>'
        : '<div class="media-stage media-stage--img">' +
          '<img id="mp-img" src="' + esc(url) + '" alt="' + esc(w.title || '') + '">' +
          (nImg > 1
            ? '<button class="media-nav media-nav--prev" data-mp="prev" aria-label="上一张">' + I.icon('chevronLeft', 20) + '</button>' +
              '<button class="media-nav media-nav--next" data-mp="next" aria-label="下一张">' + I.icon('chevronRight', 20) + '</button>' +
              '<span class="media-count" id="mp-count">1 / ' + nImg + '</span>'
            : '') +
          '</div>';

      this.open(
        this.modalFrame(
          (kind === 'video' ? '播放' : '图文') + ' · ' + (w.title || '未命名作品'),
          body + '<div class="small faint media-meta">' + esc(meta) + '</div>',
          '<button class="btn btn--ghost btn--sm" id="mp-reveal">' + I.icon('folder', 14) + '在文件夹中打开</button>' +
          '<button class="btn btn--primary btn--sm" data-modal-close>关闭</button>'
        ),
        { wide: true }
      );

      const mask = document.querySelector('.modal-mask');
      if (!mask) return;
      const box = mask.querySelector('.modal__body');

      if (kind === 'video') {
        const v = box.querySelector('#mp-video');
        v.addEventListener('error', () => {
          box.insertAdjacentHTML('afterbegin',
            '<div class="error-box"><h3>无法播放</h3><p class="muted">' +
            '文件可能已被移动、删除，或格式不被浏览器支持。可点下方「在文件夹中打开」确认。</p></div>');
        });
      } else {
        box.addEventListener('click', (e) => {
          const b = e.target.closest('[data-mp]');
          if (!b) return;
          idx = (idx + (b.dataset.mp === 'next' ? 1 : nImg - 1)) % nImg;
          box.querySelector('#mp-img').src = mkUrl(idx, 'image');
          const c = box.querySelector('#mp-count');
          if (c) c.textContent = (idx + 1) + ' / ' + nImg;
        });
      }

      // ⚠️ 这个按钮在 `.modal__foot` 里，不在 `.modal__body` 里 ——
      //    用 box.querySelector 拿会是 null，绑定就抛
      //    "Cannot read properties of null (reading 'addEventListener')"。
      mask.querySelector('#mp-reveal').addEventListener('click', () => {
        (isKs ? window.DYApi.ksRevealMedia(aid) : window.DYApi.revealMedia(aid))
          .then(() => this.toast('已在资源管理器中定位该文件', 'ok'))
          .catch((err) => this.toast(err.message || '打开失败', 'err'));
      });
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

    /* —— 哔哩哔哩扫码登录 ——
     * 二维码由后端走官方 passport 接口生成、用工程自带 qr_svg 渲染成 SVG，
     * 本方法只负责「显示 + 轮询 + 成功后回调」。登录成功的 Cookie 已经由
     * 后端落盘，所以 onDone 里直接刷新状态即可，不用再传 Cookie 回来。
     * 设置页与工作台共用这一套，避免两处各写一份轮询逻辑走偏。 */
    biliLogin(onDone) {
      const C = this;
      let timer = null;
      let sid = null;

      const stop = () => {
        if (timer) { clearInterval(timer); timer = null; }
      };

      const body =
        '<div class="flex" style="gap:18px;align-items:flex-start">' +
          '<div id="bq-box" style="width:192px;height:192px;flex:0 0 192px;border:1px solid var(--line);' +
            'border-radius:10px;display:flex;align-items:center;justify-content:center;background:#fff;overflow:hidden">' +
            '<span class="small faint">二维码加载中…</span>' +
          '</div>' +
          '<div style="flex:1;min-width:190px">' +
            '<div class="small" style="font-weight:600">用「哔哩哔哩」App 扫码</div>' +
            '<ol class="small muted" style="margin:8px 0 0 18px;padding:0;line-height:2">' +
              '<li>打开哔哩哔哩 App</li>' +
              '<li>右上角「扫一扫」</li>' +
              '<li>手机上点「确认登录」</li>' +
            '</ol>' +
            '<div class="small" id="bq-state" aria-live="polite" style="margin-top:10px;min-height:22px">' +
              '正在获取二维码…</div>' +
            '<div class="small faint" style="margin-top:6px">登录后可下载 1080P / 大会员画质，' +
              '并显著降低主页批量被风控的概率。</div>' +
          '</div>' +
        '</div>';

      const foot =
        '<button class="btn btn--ghost btn--sm" id="bq-refresh">刷新二维码</button>' +
        '<button class="btn btn--primary btn--sm" data-modal-close>关闭</button>';

      C.open(C.modalFrame('哔哩哔哩扫码登录', body, foot));

      const box = document.getElementById('bq-box');
      const stateEl = document.getElementById('bq-state');
      const setState = (html) => { if (stateEl) stateEl.innerHTML = html; };

      const start = async () => {
        stop();
        if (box) box.innerHTML = '<span class="small faint">二维码加载中…</span>';
        setState('正在获取二维码…');
        try {
          const d = await window.DYApi.biliQrStart();
          sid = d.session;
          if (box) box.innerHTML = d.svg;
          setState('<span class="dot dot--amber"></span>等待扫码…');
          poll();
        } catch (e) {
          setState('<span style="color:var(--danger)">' + esc(e.message || e) + '</span>');
        }
      };

      const poll = () => {
        stop();
        timer = setInterval(async () => {
          // 弹窗被关掉（Esc / 点遮罩 / 关闭按钮）就停，别再打后端
          if (!stateEl || !document.body.contains(stateEl)) { stop(); return; }
          try {
            const d = await window.DYApi.biliQrPoll(sid);
            if (d.state === 'scanned') {
              setState('<span class="dot dot--amber"></span>已扫码，请在手机上点「确认登录」…');
            } else if (d.state === 'expired') {
              stop();
              setState('<span class="dot dot--faint"></span>二维码已过期，点「刷新二维码」重来');
            } else if (d.state === 'failed') {
              stop();
              setState('<span style="color:var(--danger)">' + esc(d.message || '登录未生效') + '</span>');
            } else if (d.state === 'confirmed') {
              stop();
              setState('<span class="dot dot--green"></span>登录成功' +
                (d.uname ? '：' + esc(d.uname) : '') + '，Cookie 已保存');
              C.toast('哔哩哔哩已登录' + (d.uname ? '：' + d.uname : ''), 'ok');
              setTimeout(() => {
                C.close();
                if (onDone) onDone(d);
              }, 800);
            }
          } catch (e) { /* 网络抖动忽略，下一轮再问 */ }
        }, 2000);
      };

      const btn = document.getElementById('bq-refresh');
      if (btn) btn.addEventListener('click', () => {
        if (sid) window.DYApi.biliQrCancel(sid).catch(() => {});
        start();
      });
      start();
    },
  };

  window.DYComponents = C;
})();
