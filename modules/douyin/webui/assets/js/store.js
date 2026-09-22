/* ============================================================
 * store.js — 单一数据层（全站唯一的状态与持久化出口）
 * ------------------------------------------------------------
 * ★ 任何页面想读写数据，都必须走 DYStore，禁止自己碰 localStorage。
 *    - 所有键都在 dyui. 命名空间下（设置页“数据台账”可见、可导出）
 *    - 每次变更自动持久化，并通过事件总线广播（页面间解耦）
 *    - 首次运行用 mock-data.js 灌入演示数据
 * ============================================================ */
(function () {
  'use strict';

  const NS = 'dyui.';
  const U = window.DYUtils;

  const KEY = {
    creators: NS + 'creators',
    works: NS + 'works',
    tasks: NS + 'tasks',
    fav: NS + 'favorites',
    settings: NS + 'settings',
    meta: NS + 'meta',
  };

  const Store = {
    state: null,

    /* ---------- 初始化 ---------- */
    init() {
      const cfg = window.DYConfig;
      this.state = {
        creators: U.ls.get(KEY.creators, null),
        works: U.ls.get(KEY.works, null),
        tasks: U.ls.get(KEY.tasks, null),
        fav: U.ls.get(KEY.fav, null),
        settings: U.ls.get(KEY.settings, null),
        meta: U.ls.get(KEY.meta, null),
      };
      // 任一数据缺失 → 视为首次运行，整体灌入演示数据
      if (!this.state.creators || !this.state.works || !this.state.tasks) {
        this.resetDemo();
      }
      if (!this.state.fav) this.state.fav = window.DYMock.FAV_COLLECTIONS.reduce((acc, c) => {
        acc.collections.push({ ...c });
        return acc;
      }, { collections: [], likes: window.DYMock.FAV_VIDEOS.map((v) => ({ ...v })) }) && null || this.state.fav;
      if (!this.state.settings) {
        this.state.settings = { ...cfg.DEFAULT_SETTINGS };
        this.persist('settings');
      }
      if (!this.state.meta) {
        this.state.meta = { seededAt: window.DYMock.generatedAt, lastSyncCheck: null };
        this.persist('meta');
      }
    },

    /* ---------- 持久化与广播 ---------- */
    persist(what) {
      const map = {
        creators: KEY.creators, works: KEY.works, tasks: KEY.tasks,
        fav: KEY.fav, settings: KEY.settings, meta: KEY.meta,
      };
      if (map[what]) U.ls.set(map[what], this.state[what]);
      U.bus.emit('store:change', { what });
    },

    /* ---------- 任务（下载任务队列） ---------- */
    createTask({ url, platform, type, title, mode }) {
      const t = {
        id: U.uid('j'),
        url, platform, type,
        title: title || url,
        status: 'pending',
        total: 0, success: 0, failed: 0, skipped: 0,
        progress: 0,
        mode: mode || '-',
        createdAt: new Date().toISOString(),
        finishedAt: null,
        error: null,
      };
      this.state.tasks.unshift(t);
      this.persist('tasks');
      U.bus.emit('task:created', t);
      return t;
    },

    updateTask(id, patch) {
      const t = this.state.tasks.find((x) => x.id === id);
      if (!t) return null;
      Object.assign(t, patch);
      this.persist('tasks');
      U.bus.emit('task:updated', t);
      return t;
    },

    removeTask(id) {
      const i = this.state.tasks.findIndex((x) => x.id === id);
      if (i >= 0) {
        const [t] = this.state.tasks.splice(i, 1);
        this.persist('tasks');
        U.bus.emit('task:removed', t);
      }
    },

    clearFinishedTasks() {
      this.state.tasks = this.state.tasks.filter(
        (t) => t.status === 'pending' || t.status === 'running'
      );
      this.persist('tasks');
      U.bus.emit('task:bulk-changed', {});
    },

    /* ---------- 作者（关注管理） ---------- */
    addCreator(data) {
      const palette = window.DYStore && 6;
      const c = {
        id: U.uid('c'),
        nickname: data.nickname || '未命名作者',
        secUid: data.secUid || '-',
        url: data.url || '',
        note: data.note || '',
        group: data.group || '未分组',
        works: data.works || 0,
        newWorks: 0,
        lastSync: null,
        modes: data.modes || ['post'],
        autoSync: false,
        colorIndex: (this.state.creators.length) % 6,
      };
      this.state.creators.unshift(c);
      this.persist('creators');
      U.bus.emit('creator:added', c);
      return c;
    },

    updateCreator(id, patch) {
      const c = this.state.creators.find((x) => x.id === id);
      if (!c) return null;
      Object.assign(c, patch);
      this.persist('creators');
      U.bus.emit('creator:updated', c);
      return c;
    },

    removeCreator(id) {
      const i = this.state.creators.findIndex((x) => x.id === id);
      if (i >= 0) {
        const [c] = this.state.creators.splice(i, 1);
        this.persist('creators');
        U.bus.emit('creator:removed', c);
      }
    },

    /** 模拟一次“同步作者主页”：成功返回新增数，失败抛错 */
    async syncCreator(id) {
      const c = this.state.creators.find((x) => x.id === id);
      if (!c) throw new Error('作者不存在');
      await new Promise((r) => setTimeout(r, 900 + Math.random() * 700));
      // 演示规则：80% 概率同步成功
      if (Math.random() < 0.2) {
        throw new Error('同步失败：分页请求被风控拦截（演示错误，用于查看失败态）');
      }
      const added = Math.floor(Math.random() * 5);
      c.works += added;
      c.newWorks = added;
      c.lastSync = new Date().toISOString();
      this.persist('creators');
      U.bus.emit('creator:synced', c);
      return { added, creator: c };
    },

    /** 同步后清除“新作品”角标 */
    markCreatorSeen(id) {
      const c = this.state.creators.find((x) => x.id === id);
      if (c && c.newWorks) {
        c.newWorks = 0;
        this.persist('creators');
      }
    },

    /* ---------- 作品库（本地归档） ---------- */
    removeWork(id) {
      const i = this.state.works.findIndex((x) => x.id === id);
      if (i >= 0) {
        const [w] = this.state.works.splice(i, 1);
        this.persist('works');
        U.bus.emit('work:removed', w);
      }
    },

    toggleWorkFav(id) {
      const w = this.state.works.find((x) => x.id === id);
      if (!w) return null;
      w.fav = !w.fav;
      this.persist('works');
      U.bus.emit('work:fav', w);
      return w;
    },

    /** 新任务完成时归档一条作品（演示环境随机生成） */
    archiveWorkFromTask(task) {
      const w = {
        id: U.uid('w'),
        awemeId: String(Math.floor(7660000000000000000 + Math.random() * 1e12)),
        type: task.type === 'note' ? 'note' : 'video',
        title: task.title,
        creatorId: null,
        creatorName: task.title.split(' ')[0] || '未知作者',
        createTime: new Date().toISOString(),
        downloadTime: new Date().toISOString(),
        sizeMB: Math.round((2 + Math.random() * 40) * 10) / 10,
        durationSec: 10 + Math.floor(Math.random() * 300),
        quality: this.state.settings.video_quality || 'highest',
        cover: Math.floor(Math.random() * 12),
        filePath: (this.state.settings.path || './Downloaded/') + task.title + '/' + U.uid('aweme'),
        fav: false,
      };
      this.state.works.unshift(w);
      this.persist('works');
      return w;
    },

    /* ---------- 收藏与喜欢 ---------- */
    favData() {
      if (!this.state.fav) {
        this.state.fav = {
          collections: window.DYMock.FAV_COLLECTIONS.map((c) => ({ ...c })),
          likes: window.DYMock.FAV_VIDEOS.map((v) => ({ ...v })),
        };
        this.persist('fav');
      }
      return this.state.fav;
    },

    removeFavCollection(id) {
      const f = this.favData();
      f.collections = f.collections.filter((c) => c.id !== id);
      this.persist('fav');
    },

    /* ---------- 设置 ---------- */
    updateSettings(patch) {
      Object.assign(this.state.settings, patch);
      this.persist('settings');
      U.bus.emit('settings:changed', this.state.settings);
      return this.state.settings;
    },

    resetSettings() {
      this.state.settings = { ...window.DYConfig.DEFAULT_SETTINGS };
      this.persist('settings');
      U.bus.emit('settings:changed', this.state.settings);
      return this.state.settings;
    },

    /* ---------- 数据台账（导出 / 重置 / 占用统计） ---------- */
    exportAll() {
      return {
        exportedAt: new Date().toISOString(),
        seededAt: this.state.meta && this.state.meta.seededAt,
        creators: this.state.creators,
        works: this.state.works,
        tasks: this.state.tasks,
        fav: this.state.fav,
        settings: this.state.settings,
      };
    },

    resetDemo() {
      const m = window.DYMock;
      this.state = {
        creators: m.CREATORS.map((c) => ({ ...c })),
        works: m.WORKS.map((w) => ({ ...w })),
        tasks: m.TASKS.map((t) => ({ ...t })),
        fav: {
          collections: m.FAV_COLLECTIONS.map((c) => ({ ...c })),
          likes: m.FAV_VIDEOS.map((v) => ({ ...v })),
        },
        settings: { ...window.DYConfig.DEFAULT_SETTINGS },
        meta: { seededAt: m.generatedAt, lastSyncCheck: null },
      };
      Object.keys(KEY).forEach((k) => this.persist(k));
      U.bus.emit('store:reset', {});
    },

    /** 台账：列出本应用在 localStorage 的全部键与占用 */
    ledger() {
      return U.ls.usage().map((e) => ({
        ...e,
        label: ({
          'dyui.creators': '作者库',
          'dyui.works': '本地作品库',
          'dyui.tasks': '下载任务',
          'dyui.favorites': '收藏与喜欢',
          'dyui.settings': '设置',
          'dyui.meta': '元信息',
        }[e.key] || e.key),
      }));
    },
  };

  window.DYStore = Store;
})();
