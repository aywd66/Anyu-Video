/* ============================================================
 * icons.js — 内联 SVG 图标库（单一图标来源，全站禁用 emoji 做界面图形）
 * ------------------------------------------------------------
 * 规范：24×24 视窗、stroke=currentColor、线宽 1.7、几何基元构成
 * （瑞士网格风格：只用直线/圆/矩形/折线，不用曲线涂鸦）。
 * 新增图标：在 ICONS 里加一条 HTML 字符串即可，键名小写下划线。
 * 用法：icon('download') → 返回 <svg> 字符串；icon('download', 18)
 * ============================================================ */
(function () {
  'use strict';

  const P = {
    /* —— 导航与平台 —— */
    download: '<path d="M12 4v10.5"/><path d="M7.5 10.5 12 15l4.5-4.5"/><path d="M4.5 19.5h15"/>',
    music: '<circle cx="6.5" cy="17.5" r="2.7"/><circle cx="17.5" cy="15.5" r="2.7"/><path d="M9.2 17.5V6.8L20.2 4.5v11"/>',
    tiktok: '<path d="M15.5 3.5c.4 2.3 1.9 3.9 4.2 4.2v3.2c-1.6.1-3.1-.4-4.2-1.3v6.2a5.8 5.8 0 1 1-5.8-5.8c.4 0 .7 0 1 .1v3.3a2.6 2.6 0 1 0 1.8 2.4V3.5h3z"/>',
    youtube: '<rect x="2.8" y="6" width="18.4" height="12" rx="3.2"/><path d="M10.6 9.6 14.8 12l-4.2 2.4z"/>',
    users: '<circle cx="9" cy="8.2" r="3.1"/><path d="M3.6 19.4c.5-3 2.7-4.9 5.4-4.9s4.9 1.9 5.4 4.9"/><circle cx="16.8" cy="9.4" r="2.4"/><path d="M17.6 14.6c2 .4 3.3 2 3.7 4.2"/>',
    heart: '<path d="M12 20.1C7.4 16.2 4 13.1 4 9.6 4 7.1 6 5 8.5 5c1.4 0 2.7.7 3.5 1.8C12.8 5.7 14.1 5 15.5 5 18 5 20 7.1 20 9.6c0 3.5-3.4 6.6-8 10.5z"/>',
    heartFill: '<path fill="currentColor" stroke="none" d="M12 20.1C7.4 16.2 4 13.1 4 9.6 4 7.1 6 5 8.5 5c1.4 0 2.7.7 3.5 1.8C12.8 5.7 14.1 5 15.5 5 18 5 20 7.1 20 9.6c0 3.5-3.4 6.6-8 10.5z"/>',
    tasks: '<path d="M4 6.2h8"/><path d="M4 12h8"/><path d="M4 17.8h8"/><path d="m15 5.4 1.6 1.6 3-3"/><path d="m15 11.2 1.6 1.6 3-3"/><path d="m15 17 1.6 1.6 3-3"/>',
    settings: '<path d="M4 7.2h9"/><circle cx="16.2" cy="7.2" r="2.2"/><path d="M20 7.2h-.6"/><path d="M4 16.8h3"/><circle cx="10.2" cy="16.8" r="2.2"/><path d="M14 16.8h6"/>',
    star: '<path d="m12 4 2.4 5 5.4.7-4 3.8 1 5.4-4.8-2.6-4.8 2.6 1-5.4-4-3.8 5.4-.7z"/>',

    /* —— 操作 —— */
    search: '<circle cx="11" cy="11" r="6.2"/><path d="m20 20-4.4-4.4"/>',
    plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.9-6.2"/><path d="M20.5 3.5V8H16"/>',
    trash: '<path d="M4.5 6.8h15"/><path d="M9.5 6.8V4.6h5v2.2"/><path d="m6.5 6.8.9 12.6h9.2l.9-12.6"/><path d="M10 10.5v6"/><path d="M14 10.5v6"/>',
    folder: '<path d="M3.5 6.8c0-.9.7-1.6 1.6-1.6h4.1l2 2.4h7.7c.9 0 1.6.7 1.6 1.6v8.6c0 .9-.7 1.6-1.6 1.6H5.1c-.9 0-1.6-.7-1.6-1.6z"/>',
    external: '<path d="M14 4.5h5.5V10"/><path d="M19.5 4.5 11 13"/><path d="M19.5 13.8v4.7c0 .9-.7 1.6-1.6 1.6H5.6c-.9 0-1.6-.7-1.6-1.6V6.1c0-.9.7-1.6 1.6-1.6h4.7"/>',
    close: '<path d="m6 6 12 12"/><path d="M18 6 6 18"/>',
    check: '<path d="m5 13 4.2 4.2L19 7"/>',
    alert: '<path d="M12 3.8 2.8 19.8h18.4z"/><path d="M12 9.6v4.6"/><path d="M12 17.1v.2"/>',
    info: '<circle cx="12" cy="12" r="8.4"/><path d="M12 11.2v5"/><path d="M12 7.8v.2"/>',
    clock: '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.2V12l3.1 1.9"/>',
    play: '<path d="M8.2 6.2v11.6L17.6 12z"/>',
    pause: '<path d="M8.6 6v12"/><path d="M15.4 6v12"/>',
    copy: '<rect x="9" y="9" width="10.5" height="10.5" rx="1.6"/><path d="M6.2 15H5.6c-.9 0-1.6-.7-1.6-1.6V5.6c0-.9.7-1.6 1.6-1.6h7.8c.9 0 1.6.7 1.6 1.6v.6"/>',
    link: '<path d="m9.2 14.8 5.6-5.6"/><path d="m8.4 12.2-2.2 2.2a3.7 3.7 0 1 0 5.2 5.2l2.2-2.2"/><path d="m15.6 11.8 2.2-2.2a3.7 3.7 0 1 0-5.2-5.2l-2.2 2.2"/>',
    chevronDown: '<path d="m7 10 5 5 5-5"/>',
    chevronRight: '<path d="m10 7 5 5-5 5"/>',
    chevronLeft: '<path d="m14 7-5 5 5 5"/>',
    chevronUp: '<path d="m7 14 5-5 5 5"/>',
    dots: '<circle cx="5.4" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="18.6" cy="12" r="1.3"/>',
    filter: '<path d="M4.5 6.5h15"/><path d="M7.5 12h9"/><path d="M10.5 17.5h3"/>',
    upload: '<path d="M12 15.5V5"/><path d="M7.5 9.5 12 5l4.5 4.5"/><path d="M4.5 19.5h15"/>',
    openFolder: '<path d="M3.5 6.8c0-.9.7-1.6 1.6-1.6h4.1l2 2.4h5.7c.9 0 1.6.7 1.6 1.6v1.2"/><path d="M3.5 12.4 5.4 18c.2.7.8 1.2 1.6 1.2h11.2c.7 0 1.3-.4 1.5-1.1l1.8-5.7c.3-.9-.4-1.9-1.4-1.9H5.1c-.9 0-1.6.7-1.6 1.9z"/>',

    /* —— 媒体与数据 —— */
    image: '<rect x="3.5" y="5" width="17" height="14" rx="1.6"/><circle cx="8.6" cy="9.4" r="1.7"/><path d="m6 17.2 4.4-4.4 3 3 2.6-2.6 4 4"/>',
    video: '<rect x="3" y="6.4" width="13" height="11.2" rx="1.6"/><path d="m16 10.2 5-2.8v9.2l-5-2.8z"/>',
    eye: '<path d="M2.6 12S6.1 5.6 12 5.6 21.4 12 21.4 12 17.9 18.4 12 18.4 2.6 12 2.6 12z"/><circle cx="12" cy="12" r="2.8"/>',
    calendar: '<rect x="4" y="5.6" width="16" height="14.4" rx="1.6"/><path d="M4 10.2h16"/><path d="M8.2 3.4v4"/><path d="M15.8 3.4v4"/>',
    database: '<ellipse cx="12" cy="5.6" rx="7.2" ry="2.6"/><path d="M4.8 5.6v12.8c0 1.4 3.2 2.6 7.2 2.6s7.2-1.2 7.2-2.6V5.6"/><path d="M4.8 12c0 1.4 3.2 2.6 7.2 2.6s7.2-1.2 7.2-2.6"/>',
    globe: '<circle cx="12" cy="12" r="8.4"/><path d="M3.6 12h16.8"/><path d="M12 3.6c2.3 2.3 3.4 5.1 3.4 8.4s-1.1 6.1-3.4 8.4c-2.3-2.3-3.4-5.1-3.4-8.4s1.1-6.1 3.4-8.4z"/>',
    bolt: '<path d="M13.2 3.4 5.4 13.6h5l-1.6 7 7.8-10.2h-5z"/>',
    file: '<path d="M6.5 3.8h7.2L18.5 9v9.4c0 .9-.7 1.6-1.6 1.6H6.5c-.9 0-1.6-.7-1.6-1.6V5.4c0-.9.7-1.6 1.6-1.6z"/><path d="M13.4 3.8V9.3h5.1"/>',
    terminal: '<rect x="3.2" y="4.8" width="17.6" height="14.4" rx="1.6"/><path d="m7 9.4 3 2.6-3 2.6"/><path d="M12.6 14.8h4.2"/>',
    bookmark: '<path d="M7 4.4h10c.6 0 1 .4 1 1V20l-6-3.8L6 20V5.4c0-.6.4-1 1-1z"/>',
    pin: '<path d="M9.4 3.6h5.2v5l2.6 2.8v2H6.8v-2l2.6-2.8z"/><path d="M12 13.4v7"/>',
    history: '<path d="M4.6 5.4 3.4 6.6"/><path d="M3.4 6.6 6 9.2"/><path d="M5.2 13.2a7.4 7.4 0 1 0 2-6.6L3.4 9.9"/><path d="M12 8v4.4l3 1.8"/>',

    /* —— 状态 —— */
    circleCheck: '<circle cx="12" cy="12" r="8.4"/><path d="m8.4 12.2 2.4 2.4 4.8-4.8"/>',
    circleX: '<circle cx="12" cy="12" r="8.4"/><path d="m9.2 9.2 5.6 5.6"/><path d="m14.8 9.2-5.6 5.6"/>',
    circleDot: '<circle cx="12" cy="12" r="8.4"/><circle cx="12" cy="12" r="2.6"/>',
    loader: '<circle cx="12" cy="12" r="8.4" stroke-dasharray="40 13" stroke-dashoffset="8"/>',
    stop: '<rect x="6.8" y="6.8" width="10.4" height="10.4" rx="1.4"/>',
    sort: '<path d="M8 5.5v13"/><path d="m4.8 15.4 3.2 3.1 3.2-3.1"/><path d="M16 18.5v-13"/><path d="m12.8 8.6 3.2-3.1 3.2 3.1"/>',
    shield: '<path d="M12 3.4 19 6.2v5.6c0 4.6-2.9 7.5-7 9.3-4.1-1.8-7-4.7-7-9.3V6.2z"/><path d="m9 11.8 2.2 2.2 3.8-3.8"/>',
    restart: '<path d="M5 12a7 7 0 1 1 1.8 4.7"/><path d="M4.6 12.6 5 17l4.3-.8"/>',
    activity: '<path d="M3.5 12h4l2.2-5.4 3.6 10.8 2.2-5.4h5"/>',
    key: '<circle cx="8" cy="15.5" r="3.6"/><path d="m10.7 12.8 8.8-8.8"/><path d="m15.5 8 2.6 2.6"/><path d="m18 5.5 2 2"/>',
    layers: '<rect x="4" y="4.5" width="16" height="6.2" rx="1.4"/><rect x="4" y="13.3" width="16" height="6.2" rx="1.4"/>',
    queueAdd: '<path d="M4 6.5h9"/><path d="M4 12h9"/><path d="M4 17.5h6"/><path d="M17 10v8"/><path d="M13 14h8"/>',
    cat: '<path d="M7 4.5 8.8 8a5.4 5.4 0 0 1 6.4 0L16.9 4.5l1.2 4.9c.6 1 .9 2.1.9 3.2 0 3.6-3.1 6.4-7 6.4s-7-2.8-7-6.4c0-1.1.3-2.2.9-3.2z"/><path d="M9.6 11.4v.2"/><path d="M14.4 11.4v.2"/><path d="M10.6 14.6c.4.4.9.6 1.4.6s1-.2 1.4-.6"/>',
  };

  const SVG_CACHE = {};

  window.DYIcons = {
    /**
     * 取一个图标的 SVG 字符串。
     * @param {string} name 图标键名
     * @param {number} [size=20] 渲染尺寸（正方形）
     * @param {string} [cls] 附加 class
     */
    icon(name, size, cls) {
      const body = P[name] || P.info;
      const s = size || 20;
      const key = name + '|' + s + '|' + (cls || '');
      if (!SVG_CACHE[key]) {
        SVG_CACHE[key] =
          '<svg class="i' + (cls ? ' ' + cls : '') + '" width="' + s + '" height="' + s +
          '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
          'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
      }
      return SVG_CACHE[key];
    },
    has(name) { return Object.prototype.hasOwnProperty.call(P, name); },
  };
})();
