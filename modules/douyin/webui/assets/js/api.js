/* ============================================================
 * api.js — 数据访问适配层（bridge ⇄ mock 自动切换）
 * ------------------------------------------------------------
 * 三种模式：
 *   bridge : 由工程 webui/bridge_server.py 托管时自动启用。
 *            读取真实 dy_downloader.db（只读），下载调用工程 CLI run.py。
 *   mock   : 直接双击 index.html 或纯静态托管时的演示模式。
 *   rest   : 预留（工程自带 FastAPI 服务），手动切换。
 *
 * ★ 页面只调 DYApi，不关心当前模式。
 * ★ 之前 mock 阶段留下的本地数据（dyui.* 键）始终保留、不覆盖。
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;

  const DYApi = {
    mode: 'mock',                        // 'bridge' | 'mock' | 'rest'（自动探测前两者）
    REST_BASE: 'http://127.0.0.1:8000',
    bridgeInfo: null,                    // /api/bridge/health 的返回
    _timers: {},                         // mock 模式进度计时器
    _favKey: 'dyui.favAweme',            // 真实作品收藏（本地持久化的用户操作）

    /* ---------- 模式探测 ---------- */
    async detectMode() {
      if (this.mode === 'rest') return this.mode;
      if (location.protocol.indexOf('http') === 0) {
        try {
          const r = await fetch('/api/bridge/health', { cache: 'no-store' });
          if (r.ok) {
            const d = await r.json();
            if (d && d.mode === 'bridge') {
              this.mode = 'bridge';
              this.bridgeInfo = d;
              return this.mode;
            }
          }
        } catch (e) { /* 非 bridge 托管环境 */ }
      }
      this.mode = 'mock';
      return this.mode;
    },

    isBridge() { return this.mode === 'bridge'; },

    /* =====================================================
     * BRIDGE 模式（真实工程数据）
     * ===================================================== */

    async bridgeHealth() {
      const r = await fetch('/api/bridge/health', { cache: 'no-store' });
      if (!r.ok) throw new Error('桥接服务异常 ' + r.status);
      return r.json();
    },

    /** 真实作者库（按作品数排序） */
    async bridgeAuthors() {
      const r = await fetch('/api/bridge/authors', { cache: 'no-store' });
      const d = await r.json();
      return (d.authors || []).map((a, i) => ({
        id: 'br-' + (a.author_id || i),
        nickname: a.author_name || '未知作者',
        douyinId: a.author_id ? '@' + a.author_id : '-',
        secUid: a.sec_uid || '',
        url: a.sec_uid ? 'https://www.douyin.com/user/' + a.sec_uid : '',
        works: a.works || 0,
        fans: null,                     // 数据库不含粉丝数，如实不显示
        verified: false,
        mutual: null,
        lastWorkTime: a.last_time,      // 最近一条作品的发布时间
        lastDownload: a.last_time ? U.fmtDate(a.last_time) : '未知',
        source: 'bridge',
        colorIndex: i % 6,
      }));
    },

    /** 真实作品库（分页 + 筛选） */
    async bridgeWorks(opt) {
      const o = opt || {};
      const qs = new URLSearchParams();
      if (o.author) qs.set('author', o.author);
      if (o.type && o.type !== 'all') qs.set('type', o.type);
      if (o.q) qs.set('q', o.q);
      qs.set('limit', String(o.limit || 60));
      qs.set('offset', String(o.offset || 0));
      const r = await fetch('/api/bridge/aweme?' + qs.toString(), { cache: 'no-store' });
      const d = await r.json();
      const favs = this.favSet();
      return {
        total: d.total || 0,
        items: (d.items || []).map((w) => ({
          id: 'aw-' + w.aweme_id,
          awemeId: w.aweme_id,
          type: w.aweme_type === 'video' ? 'video' : (String(w.aweme_type || '').indexOf('note') >= 0 ? 'note' : 'video'),
          title: w.title && w.title !== 'no_title' ? w.title : '无标题作品',
          creatorId: null,
          creatorName: w.author_name || '未知作者',
          createTime: U.fmtDateTime(w.create_time ? Number(w.create_time) * 1000 : null),
          downloadTime: U.fmtDateTime(w.download_time ? Number(w.download_time) * 1000 : null),
          sizeMB: null,                 // 数据库不存体积，如实不显示
          durationSec: 0,
          quality: null,
          coverPath: w.cover || null,   // 抖音 CDN 封面（过期时 onerror 自动降级程序化封面）
          filePath: w.file_path || '',
          fav: favs.indexOf(String(w.aweme_id)) >= 0,
          source: 'bridge',
        })),
      };
    },

    /** 真实作品收藏开关（本地持久化，不动工程数据） */
    favSet() { return U.ls.get(this._favKey, []); },
    toggleBridgeFav(awemeId) {
      const set = this.favSet();
      const i = set.indexOf(String(awemeId));
      if (i >= 0) set.splice(i, 1); else set.push(String(awemeId));
      U.ls.set(this._favKey, set);
      return i < 0;
    },

    /** 真实下载历史（download_history 表）→ 任务行形状 */
    async bridgeHistory(limit) {
      const r = await fetch('/api/bridge/history?limit=' + (limit || 30), { cache: 'no-store' });
      const d = await r.json();
      return (d.history || []).map((h) => ({
        id: 'bh-' + h.id,
        url: h.url,
        platform: 'douyin',
        type: h.url_type || 'unknown',
        title: '工程历史 #' + h.id + ' · ' + (h.url_type || '链接'),
        status: Number(h.success_count) > 0 ? 'success' : 'muted-done',
        total: Number(h.total_count) || 0,
        success: Number(h.success_count) || 0,
        failed: 0,
        skipped: (Number(h.total_count) || 0) - (Number(h.success_count) || 0),
        progress: 100,
        mode: '-',
        createdAt: U.fmtDateTime(h.download_time ? Number(h.download_time) * 1000 : null),
        source: 'bridge-history',
      }));
    },

    /** 桥接实时任务（本次浏览器会话发起的下载） */
    async bridgeTasks() {
      const r = await fetch('/api/bridge/tasks', { cache: 'no-store' });
      const d = await r.json();
      return (d.tasks || []).map((t) => ({
        id: t.id,
        url: t.url,
        platform: 'douyin',
        type: 'bridge',
        title: (t.status === 'running' ? '正在下载：' : '下载任务：') + U.truncate(t.url, 40),
        status: t.status === 'success' ? 'success' : t.status === 'failed' ? 'failed' : 'running',
        total: 0, success: 0, failed: 0, skipped: 0,
        progress: t.status === 'running' ? 50 : 100,
        mode: 'CLI run.py',
        createdAt: t.created_at,
        error: t.status === 'failed' ? '进程退出码 ' + t.exit_code + '，查看日志了解详情' : null,
        exitCode: t.exit_code,
        source: 'bridge-live',
      }));
    },

    async bridgeTaskLog(id) {
      const r = await fetch('/api/bridge/task/' + encodeURIComponent(id), { cache: 'no-store' });
      if (!r.ok) throw new Error('获取日志失败');
      return r.json();
    },

    /** 发起真实下载：URL + 本次选项（桥接生成临时配置，跑完自动删） */
    async bridgeSubmit(url, options) {
      const r = await fetch('/api/bridge/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, options: options || {} }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '提交失败 ' + r.status);
      return d.task;
    },

    async bridgeCancel(id) {
      const r = await fetch('/api/bridge/cancel/' + encodeURIComponent(id), { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '取消失败');
      return d;
    },

    async bridgeConfig() {
      const r = await fetch('/api/bridge/config', { cache: 'no-store' });
      return r.json();
    },

    /* =====================================================
     * 统一入口（页面只调这些）
     * ===================================================== */

    async health() {
      if (this.mode === 'bridge') return this.bridgeHealth();
      if (this.mode === 'rest') {
        const r = await fetch(this.REST_BASE + '/api/v1/health');
        return r.json();
      }
      await new Promise((r2) => setTimeout(r2, 120));
      return { status: 'ok', mode: 'mock' };
    },

    /** 任务中心数据源：bridge = 实时 + 工程历史 + 本地旧记录；mock = 本地 */
    async taskFeed() {
      if (!this.isBridge()) {
        return { live: [], history: [], local: window.DYStore.state.tasks };
      }
      const [live, history] = await Promise.all([this.bridgeTasks(), this.bridgeHistory(30)]);
      return { live, history, local: window.DYStore.state.tasks };
    },

    /** 提交下载：bridge 走真实 CLI，mock 走模拟 */
    async submitDownload({ url, platform, type, title, mode, total, options }) {
      const store = window.DYStore;
      if (this.isBridge()) {
        return this.bridgeSubmit(url, options);
      }
      if (this.mode === 'rest') {
        const r = await fetch(this.REST_BASE + '/api/v1/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url }),
        });
        if (!r.ok) throw new Error('后端返回 ' + r.status);
        const data = await r.json();
        return store.createTask({ url, platform, type, mode, title: title || data.job_id });
      }
      // mock 模拟
      const t = store.createTask({ url, platform, type, title, mode });
      t.total = total !== undefined ? total : this._guessTotal(type);
      store.updateTask(t.id, { total: t.total });
      setTimeout(() => {
        const cur = store.state.tasks.find((x) => x.id === t.id);
        if (!cur || cur.status !== 'pending') return;
        store.updateTask(t.id, { status: 'running' });
        this._simulateProgress(t.id, t.total);
      }, 900);
      return t;
    },

    _guessTotal(type) {
      if (type === 'video' || type === 'note' || type === 'music') return 1;
      return 6 + Math.floor(Math.random() * 60);
    },

    _simulateProgress(id, total) {
      const store = window.DYStore;
      clearInterval(this._timers[id]);
      let done = 0;
      this._timers[id] = setInterval(() => {
        const t = store.state.tasks.find((x) => x.id === id);
        if (!t || t.status !== 'running') { clearInterval(this._timers[id]); return; }
        const step = 1 + Math.floor(Math.random() * Math.min(3, Math.max(1, total - done)));
        done = Math.min(total, done + step);
        const failed = done === total && Math.random() < 0.12 ? 1 : 0;
        const finished = done >= total;
        store.updateTask(id, {
          success: done - failed,
          failed,
          progress: Math.round((done / Math.max(1, total)) * 100),
          status: finished ? (failed ? 'failed' : 'success') : 'running',
          finishedAt: finished ? new Date().toISOString() : null,
          error: finished && failed
            ? 'DownloadError: 第 ' + done + ' 个作品下载超时（演示错误，用于查看失败态）'
            : null,
        });
        if (finished) {
          clearInterval(this._timers[id]);
          const task = store.state.tasks.find((x) => x.id === id);
          if (task && task.status === 'success' && task.total <= 3) {
            store.archiveWorkFromTask(task);
          }
        }
      }, 700);
    },

    cancelTask(id) {
      if (this.isBridge()) return this.bridgeCancel(id);
      clearInterval(this._timers[id]);
      return window.DYStore.updateTask(id, {
        status: 'canceled', finishedAt: new Date().toISOString(),
      });
    },

    retryTask(id) {
      const t = window.DYStore.updateTask(id, {
        status: 'pending', progress: 0, success: 0, failed: 0,
        skipped: 0, error: null, finishedAt: null,
        createdAt: new Date().toISOString(),
      });
      if (t && t.total > 0) {
        setTimeout(() => {
          const cur = window.DYStore.state.tasks.find((x) => x.id === id);
          if (cur && cur.status === 'pending') {
            window.DYStore.updateTask(id, { status: 'running' });
            this._simulateProgress(id, cur.total);
          }
        }, 700);
      }
      return t;
    },

    async syncAccount() {
      await new Promise((r) => setTimeout(r, 1100));
      if (Math.random() < 0.15) {
        throw new Error('Cookie 已过期，请重新登录抖音（演示错误，用于查看失败态）');
      }
      return { collections: 3, likes: 6, syncedAt: new Date().toISOString() };
    },
  };

  window.DYApi = DYApi;
})();
