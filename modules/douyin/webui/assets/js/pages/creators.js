/* ============================================================
 * pages/creators.js — 关注管理
 * ------------------------------------------------------------
 * 对照参考截图 004：同步状态条 → 搜索/排序/视图/多选工具行
 * → 筛选 chips → 统计行 → 作者卡片列表（网格/列表双视图）
 * 卡片：圆形头像 + 昵称(认证徽章) + 抖音号 + 简介 + 数据行 + 标签行
 *       + 右侧「下载」主按钮 + 「⋯」更多
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  let ui = { q: '', sort: 'recent', view: 'list', chips: {}, grid: {} };
  let bridgeAuthors = null;
  const NOTES_KEY = 'dyui.creatorNotes';

  function localNotes() { return window.DYUtils.ls.get(NOTES_KEY, {}); }
  function setLocalNote(nickname, note) {
    const m = localNotes();
    if (note) m[nickname] = note; else delete m[nickname];
    window.DYUtils.ls.set(NOTES_KEY, m);
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

      /* 同步状态条 */
      '<div class="row-item mb-4" id="cr-syncbar"></div>' +

      /* 工具行 */
      '<div class="flex flex-wrap mb-3" style="gap:12px">' +
        '<div style="position:relative;flex:2;min-width:220px">' +
          '<input class="input" id="cr-search" placeholder="搜索昵称 / 抖音号 / 备注" aria-label="搜索作者" ' +
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
      '<div id="cr-list"></div>';

    if (window.DYApi.isBridge()) {
      const addBtn = view.querySelector('#cr-add');
      if (addBtn) addBtn.style.display = 'none';
      const chips = view.querySelector('#cr-chips');
      if (chips) chips.style.display = 'none';
    }

    renderSyncBar();
    renderList();

    view.querySelector('#cr-search').addEventListener('input', U.debounce((e) => {
      ui.q = e.target.value.trim().toLowerCase();
      renderList();
    }, 150));
    view.querySelector('#cr-sort').addEventListener('change', (e) => { ui.sort = e.target.value; renderList(); });
    view.querySelector('#cr-add').addEventListener('click', openAdd);
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
    });

    U.bus.on('creator:synced', () => renderSyncBar());
    U.bus.on('store:reset', () => { renderSyncBar(); renderList(); });
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
        if (!bridgeAuthors) bridgeAuthors = await window.DYApi.bridgeAuthors();
        const totalWorks = bridgeAuthors.reduce((a, c) => a + c.works, 0);
        el.innerHTML =
          '<div class="side-card__icon">' + I.icon('database', 17) + '</div>' +
          '<div class="row-item__main">' +
            '<div class="row-item__meta" style="margin:0">' +
              '<span>来自工程数据库（只读）</span>' +
              '<span>作者 <b class="num">' + bridgeAuthors.length + '</b> 位</span>' +
              '<span>作品 <b class="num">' + window.DYUtils.group(totalWorks) + '</b> 条</span>' +
              '<span style="color:var(--green)">' + I.icon('circleCheck', 13) + ' 已连接</span>' +
            '</div>' +
          '</div>';
        return;
      } catch (e) {
        el.innerHTML = '<div class="row-item__main"><div class="row-item__meta" style="margin:0;color:var(--red)">' +
          '读取数据库失败：' + esc(String(e.message || e)) + '</div></div>';
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
      let cs = (bridgeAuthors || []).slice();
      if (ui.q) cs = cs.filter((c) =>
        c.nickname.toLowerCase().includes(ui.q) ||
        (c.douyinId || '').toLowerCase().includes(ui.q));
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
    const tags = [];
    if (window.DYApi.isBridge()) {
      tags.push('<span class="badge badge--cyan">最近作品 ' + esc(c.lastDownload) + '</span>');
      if (note) tags.push('<span class="badge badge--pink" title="' + esc(note) + '">备注: ' + esc(U.truncate(note, 12)) + '</span>');
    } else {
      if (c.newWorks > 0) tags.push('<span class="badge badge--amber">新作品 +' + c.newWorks + '</span>');
      if (c.mutual) tags.push('<span class="badge badge--cyan">互关</span>');
      if (note) tags.push('<span class="badge badge--pink" title="' + esc(note) + '">备注: ' + esc(U.truncate(note, 12)) + '</span>');
      tags.push('<span class="badge">' + esc(c.lastDownload || '未下载') + '</span>');
    }
    return (
      '<div class="row-item" data-creator="' + esc(c.id) + '">' +
        C.avatar(c.nickname, c.colorIndex, 'lg') +
        '<div class="row-item__main">' +
          '<div class="flex" style="gap:8px">' +
            '<span class="row-item__title" style="flex:none;max-width:60%">' + C.ellipsisTitle(c.nickname, 22) + '</span>' +
            (c.verified ? '<span class="badge badge--cyan" style="gap:3px">' + I.icon('check', 11) + '已认证</span>' : '') +
            '<span class="mono small faint">' + esc(c.douyinId) + '</span>' +
          '</div>' +
          (c.bio ? '<div class="row-item__meta"><span class="ellipsis" style="max-width:52ch">' + esc(c.bio) + '</span></div>' : '') +
          '<div class="row-item__meta"><span>作品 <b class="num">' + U.group(c.works) + '</b></span>' +
            (c.fans != null ? '<span>粉丝 <b class="num">' + U.compact(c.fans) + '</b></span>' : '') +
            (c.modes && !window.DYApi.isBridge() ? '<span>模式 <b>' + esc(c.modes.join('+')) + '</b></span>' : '') +
          '</div>' +
          '<div class="row-item__meta">' + tags.join('') + '</div>' +
        '</div>' +
        '<div class="row-item__actions">' +
          '<button class="btn btn--primary btn--sm" data-act="download" data-id="' + esc(c.id) + '">' +
          I.icon('download', 14) + '下载</button>' +
          '<button class="btn btn--ghost btn--sm btn--icon" data-act="more" data-id="' + esc(c.id) + '" aria-label="更多操作">' +
          I.icon('dots', 15) + '</button>' +
        '</div>' +
      '</div>'
    );
  }

  /* —— 网格模式小卡 —— */
  function creatorTile(c) {
    return (
      '<div class="card" data-creator="' + esc(c.id) + '" style="text-align:center;padding:var(--sp-4)">' +
        '<div class="flex" style="justify-content:center">' + C.avatar(c.nickname, c.colorIndex, 'lg') + '</div>' +
        '<div class="small mt-3" style="font-weight:700">' + esc(U.truncate(c.nickname, 12)) + '</div>' +
        '<div class="small faint mono">' + esc(c.douyinId) + '</div>' +
        '<div class="small muted mt-2">作品 <b class="num">' + U.group(c.works) + '</b></div>' +
        (c.newWorks ? '<div class="mt-2"><span class="badge badge--amber">新 +' + c.newWorks + '</span></div>' : '') +
        '<div class="mt-3"><button class="btn btn--primary btn--sm" data-act="download" data-id="' + esc(c.id) + '">' +
        I.icon('download', 13) + '下载</button></div>' +
      '</div>'
    );
  }

  async function renderList() {
    const box = document.getElementById('cr-list');
    if (!box) return;
    if (window.DYApi.isBridge()) {
      if (!bridgeAuthors) {
        box.innerHTML = C.skeletonRows(3);
        try { bridgeAuthors = await window.DYApi.bridgeAuthors(); }
        catch (e) {
          box.innerHTML = '<div class="error-box"><h3>读取作者库失败</h3><p class="muted">' +
            esc(String(e.message || e)) + '</p></div>';
          return;
        }
      }
      const cs = filtered();
      const line = document.getElementById('cr-statline');
      if (line) line.innerHTML = '显示 ' + cs.length + ' / ' + bridgeAuthors.length + ' 位作者（工程数据库）';
      box.innerHTML = cs.length
        ? (ui.view === 'list'
          ? '<div class="grid" style="gap:12px">' + cs.map(creatorCard).join('') + '</div>'
          : '<div class="grid grid--4">' + cs.map(creatorTile).join('') + '</div>')
        : (ui.q ? C.empty('search', 'search') : C.empty('creators', 'users'));
      await renderSyncBar();
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

  /* —— 卡片操作 —— */
  function onCardAction(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;

    if (window.DYApi.isBridge()) {
      const c = (bridgeAuthors || []).find((x) => x.id === id);
      if (!c) return;
      if (act === 'download') {
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

  /* —— bridge 作者更多操作 —— */
  function openMoreBridge(c) {
    const note = localNotes()[c.nickname] || '';
    C.open(C.modalFrame(c.nickname,
      '<div class="flex mb-4">' + C.avatar(c.nickname, c.colorIndex, 'lg') +
      '<div class="grow"><div class="row-item__title">' + esc(c.nickname) + '</div>' +
      '<div class="small muted mono">' + esc(c.douyinId) + '</div>' +
      '<div class="small faint mt-2 ellipsis">' + esc(c.url || '（数据库中无 sec_uid，无法构造主页链接）') + '</div></div></div>' +
      '<div class="grid" style="gap:10px">' +
      moreRow('br-copy', 'link', '复制作者主页链接', c.url || '不可用') +
      moreRow('br-download', 'download', '去下载该作者', '预填到抖音工作台') +
      moreRow('br-note', 'bookmark', '编辑本地备注', '当前：' + (note || '无')) +
      '</div>'));
    document.querySelectorAll('[data-more-act]').forEach((b) => {
      b.addEventListener('click', () => {
        const act = b.dataset.moreAct;
        C.close();
        if (act === 'br-copy') {
          if (!c.url) { C.toast('该作者缺少 sec_uid，无法复制链接', 'err'); return; }
          U.copyText(c.url).then((ok) => C.toast(ok ? '链接已复制' : '复制失败', ok ? 'ok' : 'err'));
        } else if (act === 'br-download') {
          const fake = { target: document.createElement('button') };
          fake.target.dataset.act = 'download';
          fake.target.dataset.id = c.id;
          onCardAction(fake);
        } else if (act === 'br-note') {
          openBridgeNote(c);
        }
      });
    });
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
      (danger ? 'color:var(--red);border-color:rgba(224,83,107,.4)' : '') + '">' +
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
