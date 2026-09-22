/* ============================================================
 * pages/favorites.js — 收藏与喜欢（当前登录账号）
 * ------------------------------------------------------------
 * Tab：收藏视频 / 收藏合集 / 喜欢；同步账号按钮（含失败态演示）
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  let tab = 'video';
  let syncing = false;

  function render(view) {
    view.className = 'content';
    delete view.dataset.platform;
    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div class="page-head__row">' +
          '<div><div class="crumb"><span class="crumb__dot"></span>账号内容 · 收藏与喜欢</div>' +
          '<h1 class="page-title">收藏与喜欢</h1></div>' +
          '<button class="btn btn--ghost btn--sm" id="fv-sync">' + I.icon('refresh', 14) +
          esc(syncing ? '同步中…' : '缓存与同步') + '</button>' +
        '</div>' +
      '</div>' +
      '<p class="small muted mb-4">展示当前登录抖音账号的收藏视频、收藏合集与喜欢列表；' +
      '网页演示环境使用内置数据，接入后端后由 Cookie 拉取真实数据。</p>' +
      '<div class="mb-4" id="fv-tabs"></div>' +
      '<div id="fv-body"></div>';

    if (window.DYApi.isBridge()) {
      // 桥接模式：收藏/喜欢需要登录态接口，桥接层暂未提供，如实告知
      const syncBtn = view.querySelector('#fv-sync');
      if (syncBtn) syncBtn.style.display = 'none';
      document.getElementById('fv-tabs').innerHTML = '';
      document.getElementById('fv-body').innerHTML =
        '<div class="empty"><div class="empty__icon">' + I.icon('bookmark', 32) + '</div>' +
        '<div class="empty__title">桥接模式暂不提供收藏同步</div>' +
        '<div class="empty__desc">收藏夹 / 喜欢列表需要登录态接口，当前桥接服务只读下载数据库；' +
        '请使用官方桌面端同步，或断开桥接后查看演示数据。</div></div>';
      return;
    }

    renderTabs();
    renderBody();

    view.querySelector('#fv-sync').addEventListener('click', onSync);
    view.querySelector('#fv-tabs').addEventListener('click', (e) => {
      const t = e.target.closest('[data-tab]');
      if (!t) return;
      tab = t.dataset.tab;
      renderTabs();
      renderBody();
    });
    view.querySelector('#fv-body').addEventListener('click', onBodyAction);
  }

  function renderTabs() {
    const f = window.DYStore.favData();
    document.getElementById('fv-tabs').innerHTML = C.tabs([
      { id: 'video', label: '我的收藏（视频）', count: f.likes.filter((l) => l.kind === 'video').length },
      { id: 'collection', label: '我收藏的合集', count: f.collections.length },
      { id: 'like', label: '我的喜欢', count: f.likes.length },
    ], tab);
  }

  function renderBody() {
    const box = document.getElementById('fv-body');
    const f = window.DYStore.favData();
    const works = window.DYStore.state.works;

    if (tab === 'collection') {
      box.innerHTML = f.collections.length
        ? '<div class="grid grid--3">' + f.collections.map((c) =>
            '<div class="work-card">' +
            '<div class="cover cover--ph" data-c="' + (c.cover || 0) % 6 + '">' + I.icon('bookmark', 26) + '</div>' +
            '<div class="work-card__body">' +
              '<div class="work-card__title">' + esc(c.title) + '</div>' +
              '<div class="work-card__meta"><span>创建者：' + esc(c.creator) + '</span>' +
              '<span class="num">共 ' + c.count + ' 项</span>' +
              '<span>' + esc(c.syncedAt ? '上次同步 ' + U.fromNow(c.syncedAt) : '未同步') + '</span></div>' +
            '</div>' +
            '<div class="work-card__actions">' +
              '<button class="btn btn--primary btn--sm grow" data-act="dl-collection" data-id="' + esc(c.id) + '">' +
              I.icon('download', 13) + '下载整个合集</button>' +
              '<button class="btn btn--ghost btn--sm btn--icon" data-act="rm-collection" data-id="' + esc(c.id) + '" aria-label="移除收藏">' +
              I.icon('trash', 14) + '</button>' +
            '</div>' +
            '</div>').join('') + '</div>'
        : C.empty('favorites', 'bookmark');
      return;
    }

    const list = f.likes
      .map((l) => works.find((w) => w.id === l.work))
      .filter(Boolean)
      .filter((w) => (tab === 'video' ? l_kind(w, f) : true));

    function l_kind() { return true; }

    box.innerHTML = list.length
      ? '<div class="grid grid--4">' + list.map((w) => C.workCard(w)).join('') + '</div>'
      : C.empty('favorites', 'heart');
  }

  function onSync(btn) {
    if (syncing) return;
    syncing = true;
    const b = document.getElementById('fv-sync');
    if (b) { b.disabled = true; b.innerHTML = I.icon('loader', 14) + '同步中…'; }
    window.DYApi.syncAccount()
      .then(() => C.toast('同步完成：3 个合集 · 6 条喜欢', 'ok'))
      .catch((e) => C.toast(e.message, 'err'))
      .finally(() => {
        syncing = false;
        const nb = document.getElementById('fv-sync');
        if (nb) { nb.disabled = false; nb.innerHTML = I.icon('refresh', 14) + '同步账号'; }
        renderBody();
        renderTabs();
      });
  }

  function onBodyAction(e) {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    if (btn.dataset.act === 'rm-collection') {
      C.confirm('移除收藏合集', '仅从「收藏与喜欢」列表移除，不影响本地已下载文件。', () => {
        window.DYStore.removeFavCollection(id);
        renderTabs();
        renderBody();
        C.toast('已移除', 'ok');
      }, '移除', true);
    } else if (btn.dataset.act === 'dl-collection') {
      C.toast('已创建合集下载任务（演示）', 'ok');
      window.DYRouter.go('douyin');
    } else if (btn.dataset.act === 'fav') {
      window.DYStore.toggleWorkFav(id);
      renderBody();
    } else if (btn.dataset.act === 'path') {
      const w = window.DYStore.state.works.find((x) => x.id === id);
      if (w) U.copyText(w.filePath).then((ok) => C.toast(ok ? '路径已复制' : '复制失败', ok ? 'ok' : 'err'));
    }
  }

  window.DYRouter.register('favorites', render);
})();
