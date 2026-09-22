/* ============================================================
 * utils.js — 通用工具函数
 * ------------------------------------------------------------
 * 只放与业务无关的纯函数。格式化规则如果以后要变（比如数字
 * 单位、日期格式），只改这里，所有页面自动生效。
 * ============================================================ */
(function () {
  'use strict';

  const pad = (n) => (n < 10 ? '0' + n : '' + n);

  const Utils = {
    /** 生成稳定 id（模拟后端 job_id） */
    uid(prefix) {
      return (prefix || 'id') + '-' + Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4);
    },

    /** 防抖 */
    debounce(fn, ms) {
      let t = null;
      return function (...args) {
        clearTimeout(t);
        t = setTimeout(() => fn.apply(this, args), ms || 200);
      };
    },

    /** 12345 -> "1.2万"；用于播放量/粉丝数等大数字 */
    compact(n) {
      if (n === null || n === undefined || isNaN(n)) return '-';
      n = Number(n);
      if (Math.abs(n) >= 100000000) return (n / 100000000).toFixed(1).replace(/\.0$/, '') + '亿';
      if (Math.abs(n) >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + '万';
      return String(n);
    },

    /** 千分位，用于文件大小/计数等精确数字 */
    group(n) {
      if (n === null || n === undefined || isNaN(n)) return '-';
      return Number(n).toLocaleString('zh-CN');
    },

    /** 字节 -> 可读大小 */
    bytes(b) {
      if (b === null || b === undefined || isNaN(b) || b < 0) return '-';
      if (b < 1024) return b + ' B';
      if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
      if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + ' MB';
      return (b / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    },

    /** 秒 -> "03:45" 或 "1:02:03" */
    duration(sec) {
      sec = Math.max(0, Math.round(Number(sec) || 0));
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      return h > 0 ? h + ':' + pad(m) + ':' + pad(s) : pad(m) + ':' + pad(s);
    },

    /** ISO/时间戳 -> "2026-08-14 21:30" */
    fmtDateTime(v) {
      if (!v) return '-';
      const d = new Date(v);
      if (isNaN(d.getTime())) return String(v);
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
        ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    },

    /** ISO/时间戳 -> "2026-08-14" */
    fmtDate(v) {
      if (!v) return '-';
      const d = new Date(v);
      if (isNaN(d.getTime())) return String(v);
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    },

    /** 相对时间："3 分钟前" */
    fromNow(v) {
      if (!v) return '-';
      const t = new Date(v).getTime();
      if (isNaN(t)) return String(v);
      const diff = Date.now() - t;
      const abs = Math.abs(diff);
      const suffix = diff >= 0 ? '前' : '后';
      if (abs < 60000) return '刚刚';
      if (abs < 3600000) return Math.floor(abs / 60000) + ' 分钟' + suffix;
      if (abs < 86400000) return Math.floor(abs / 3600000) + ' 小时' + suffix;
      if (abs < 86400000 * 30) return Math.floor(abs / 86400000) + ' 天' + suffix;
      return Utils.fmtDate(v);
    },

    /** 截断过长文本（标题超长时用） */
    truncate(str, n) {
      if (!str) return '';
      str = String(str);
      return str.length > n ? str.slice(0, n - 1) + '…' : str;
    },

    /** HTML 转义（所有动态文案进 DOM 前必须过这里，防注入） */
    esc(str) {
      return String(str === null || str === undefined ? '' : str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    /** 从分享文本里提取 http(s) 链接（与桌面端 launcher 同规则） */
    extractUrls(text) {
      const re = /https?:\/\/[^\s"'，。、）)】>]+/gi;
      const out = [];
      const m = String(text || '').match(re);
      if (m) out.push(...m);
      return out;
    },

    /** 猜测链接类型（仅用于前端即时提示，真实解析以后端为准） */
    guessUrlType(url) {
      const u = String(url || '');
      if (/v\.douyin\.com|v\.iesdouyin\.com/i.test(u)) return 'short';
      if (/\/video\//i.test(u)) return 'video';
      if (/\/note\//i.test(u) || /\/gallery\//i.test(u)) return 'note';
      if (/live\.douyin\.com/i.test(u)) return 'live';
      if (/\/collection\/|\/mix\//i.test(u)) return 'collection';
      if (/\/music\//i.test(u)) return 'music';
      if (/\/user\//i.test(u)) return 'user';
      return 'unknown';
    },

    /** 简单事件总线：页面之间解耦通信用 */
    bus: {
      _h: {},
      on(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); },
      off(ev, fn) { this._h[ev] = (this._h[ev] || []).filter((f) => f !== fn); },
      emit(ev, data) { (this._h[ev] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error('[bus]', ev, e); } }); },
    },

    /** 复制文本到剪贴板（file:// 下尽量兼容） */
    async copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (e) {
        try {
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.style.cssText = 'position:fixed;opacity:0';
          document.body.appendChild(ta);
          ta.select();
          const ok = document.execCommand('copy');
          ta.remove();
          return ok;
        } catch (e2) { return false; }
      }
    },

    /** localStorage 安全读写（隐私模式/被禁时不崩） */
    ls: {
      get(key, fallback) {
        try {
          const raw = localStorage.getItem(key);
          return raw === null ? fallback : JSON.parse(raw);
        } catch (e) { return fallback; }
      },
      set(key, val) {
        try { localStorage.setItem(key, JSON.stringify(val)); return true; }
        catch (e) { return false; }
      },
      del(key) {
        try { localStorage.removeItem(key); } catch (e) { /* ignore */ }
      },
      /** 全站占用统计（设置页“数据台账”展示用） */
      usage() {
        const out = [];
        try {
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (k && k.indexOf('dyui.') === 0) {
              out.push({ key: k, bytes: (localStorage.getItem(k) || '').length });
            }
          }
        } catch (e) { /* ignore */ }
        return out;
      },
    },
  };

  window.DYUtils = Utils;
})();
