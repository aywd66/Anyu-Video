/* ============================================================
 * router.js — Hash 路由
 * ------------------------------------------------------------
 * 用 hash（#/xxx）做前端路由，file:// 直接打开也能用。
 * 路由表在 config.js 的 NAV 里定义；新增页面 = 新建一个
 * pages/xxx.js + 在 NAV 里加一条，本文件不用动。
 * ============================================================ */
(function () {
  'use strict';

  const Router = {
    routes: {},        // id -> render 函数（由各页面注册）
    current: null,

    /** 页面模块调用：注册自己的渲染函数 */
    register(id, renderFn) {
      this.routes[id] = renderFn;
    },

    /** 当前 hash 里的页面 id（非法时回默认页） */
    parse() {
      const raw = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
      const def = window.DYConfig.NAV.platforms.find((n) => n.default).id;
      const id = raw || def;
      return this.routes[id] ? id : def;
    },

    /** 主动跳转 */
    go(id) {
      if (('#/' + id) === location.hash) {
        this.render();
      } else {
        location.hash = '#/' + id;
      }
    },

    /** 渲染当前页面到 #page-view，并同步侧边栏选中态 */
    render() {
      const id = this.parse();
      const view = document.getElementById('page-view');
      const nav = window.DYConfig.NAV.platforms.find((n) => n.id === id) ||
        window.DYConfig.NAV.menu.find((n) => n.id === id);
      this.current = id;
      if (!view) return;

      // 页面切换：清掉上一页可能遗留的轮询计时器
      if (window.__DY_PAGE_TIMER) {
        clearInterval(window.__DY_PAGE_TIMER);
        window.__DY_PAGE_TIMER = null;
      }

      // 页面切换统一动画入口：先清空再渲染，避免旧事件残留
      view.innerHTML = '';
      view.scrollTop = 0;
      const t0 = performance.now();
      try {
        this.routes[id](view);
      } catch (err) {
        console.error('[router] 页面渲染失败:', id, err);
        view.innerHTML =
          '<div class="error-box"><div class="error-box__icon">' + window.DYIcons.icon('alert', 28) +
          '</div><h3>页面加载失败</h3><p>' + window.DYUtils.esc(String(err && err.message || err)) +
          '</p><button class="btn" onclick="DYRouter.render()">重试</button></div>';
      }
      const dt = Math.round(performance.now() - t0);

      // 侧边栏选中态 + 顶栏面包屑
      document.querySelectorAll('.nav__item').forEach((el) => {
        el.classList.toggle('is-active', el.dataset.navId === id);
        if (el.dataset.navId === id) el.setAttribute('aria-current', 'page');
        else el.removeAttribute('aria-current');
      });
      const crumb = document.getElementById('crumb');
      if (crumb) {
        // 面包屑文案：优先用平台工作台的专属 crumb，否则用菜单标签
        const plat = window.DYConfig.PLATFORMS[id];
        crumb.textContent = plat ? plat.crumb : (nav ? nav.label : id);
      }
      const crumbIcon = document.getElementById('crumb-icon');
      if (crumbIcon) crumbIcon.innerHTML = nav ? window.DYIcons.icon(nav.icon, 16) : '';

      // 提供性能观测点（自检脚本用）
      window.__DY_LAST_RENDER = { id, ms: dt, at: new Date().toISOString() };
    },

    /** 初始化：监听 hash 变化 */
    start() {
      window.addEventListener('hashchange', () => this.render());
      this.render();
    },
  };

  window.DYRouter = Router;
})();
