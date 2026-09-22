/* ============================================================
 * pages/settings.js — 设置
 * ------------------------------------------------------------
 * schema 驱动：SETTINGS_SCHEMA 里加字段，本页自动渲染。
 * 分组卡片 + 开关/输入/下拉 + 恢复默认 + 数据台账卡片
 * ============================================================ */
(function () {
  'use strict';

  const U = window.DYUtils;
  const I = window.DYIcons;
  const C = window.DYComponents;
  const esc = U.esc;

  function render(view) {
    view.className = 'content';
    delete view.dataset.platform;
    const cfg = window.DYConfig;
    const s = window.DYStore.state.settings;

    view.innerHTML =
      '<div class="page-head page-head--compact">' +
        '<div class="page-head__row">' +
          '<div><div class="crumb"><span class="crumb__dot"></span>系统 · 设置</div>' +
          '<h1 class="page-title">设置</h1></div>' +
          '<button class="btn btn--ghost btn--sm" id="st-reset">' + I.icon('restart', 14) + '全部恢复默认</button>' +
        '</div>' +
      '</div>' +
      '<p class="small muted mb-4">以下对应工程 <code>config.yml</code> 的核心字段；' +
      '网页演示环境保存在浏览器本地，接入后端后可改为写入真实配置。</p>' +
      '<div class="grid" style="gap:var(--sp-4);align-items:start" id="st-groups">' +
        '<div>' + cfg.SETTINGS_SCHEMA.map(groupCard).join('') + '</div>' +
        '<div>' + aboutCard() + ledgerCard() + '</div>' +
      '</div>';

    if (window.DYApi.isBridge()) {
      renderBridgeConfig(view);
      return;
    }
    bind(view);
  }

  /* —— bridge 模式：展示工程真实 config.yml（已脱敏，只读） —— */
  async function renderBridgeConfig(view) {
    const groups = document.getElementById('st-groups');
    if (!groups) return;
    groups.innerHTML = '<div class="card">' + C.skeletonRows(2) + '</div>';
    try {
      const d = await window.DYApi.bridgeConfig();
      if (!d.available) throw new Error(d.reason || 'config.yml 不可读');
      const cfgObj = d.config || {};
      const entries = Object.keys(cfgObj).filter((k) => typeof cfgObj[k] !== 'object');
      const nested = Object.keys(cfgObj).filter((k) => typeof cfgObj[k] === 'object' && cfgObj[k]);
      const row = (k, v) => '<tr><td style="font-weight:600;white-space:nowrap">' + esc(k) + '</td>' +
        '<td><code class="mono small" style="word-break:break-all">' + esc(String(v)) + '</code></td></tr>';
      groups.innerHTML =
        '<div>' +
          '<section class="card mb-4"><div class="card__title mb-3">工程配置 config.yml（实时读取 · 敏感项已脱敏）</div>' +
            '<div class="table-wrap"><table class="table"><tbody>' +
            entries.map((k) => row(k, cfgObj[k])).join('') +
            nested.map((k) => row(k, JSON.stringify(cfgObj[k]))).join('') +
            '</tbody></table></div>' +
            '<p class="small faint mt-3">网页端不直接改写 config.yml；修改配置请编辑工程根目录的 config.yml 后重启桥接服务即可生效。</p>' +
          '</section>' +
        '</div>' +
        '<div>' + ledgerCard() + '</div>';
      // 台账操作仍可用
      const ex = document.getElementById('st-export');
      if (ex) ex.addEventListener('click', exportData);
      const rd = document.getElementById('st-resetdemo');
      if (rd) rd.addEventListener('click', () => {
        C.confirm('重置演示数据', '将清空本地演示记录并恢复为内置演示数据；工程数据库不受影响。', () => {
          window.DYStore.resetDemo();
          C.toast('已重置本地演示数据', 'ok');
        }, '重置', true);
      });
    } catch (e) {
      groups.innerHTML = '<div class="error-box"><h3>读取工程配置失败</h3><p class="muted">' +
        esc(String(e.message || e)) + '</p></div>';
    }
  }

  function groupCard(g) {
    return (
      '<section class="card mb-4">' +
        '<div class="card__title mb-4">' + esc(g.group) + '</div>' +
        g.fields.map(field).join('') +
      '</section>'
    );
  }

  function field(fd) {
    const s = window.DYStore.state.settings;
    const desc = fd.desc ? '<div class="form-item__desc">' + esc(fd.desc) + '</div>' : '';
    if (fd.type === 'switch') {
      return (
        '<div class="form-item flex" style="justify-content:space-between">' +
          '<div><div class="form-item__label" style="margin:0">' + esc(fd.label) + '</div>' + desc + '</div>' +
          C.switchHtml(!!s[fd.key], 'data-set="' + esc(fd.key) + '"') +
        '</div>'
      );
    }
    if (fd.type === 'select') {
      const options = window.DYConfig[fd.options] || [];
      return (
        '<div class="form-item"><div class="form-item__label">' + esc(fd.label) + '</div>' +
        '<select class="input" data-set="' + esc(fd.key) + '">' +
        options.map((o) => '<option value="' + esc(o.id) + '"' + (s[fd.key] === o.id ? ' selected' : '') + '>' +
          esc(o.label) + '</option>').join('') +
        '</select>' + desc + '</div>'
      );
    }
    return (
      '<div class="form-item"><div class="form-item__label">' + esc(fd.label) + '</div>' +
      '<input class="input" data-set="' + esc(fd.key) + '" type="' + (fd.type === 'number' ? 'number' : 'text') + '" ' +
      (fd.min !== undefined ? 'min="' + fd.min + '" max="' + fd.max + '"' : '') +
      ' value="' + esc(s[fd.key] === undefined ? '' : s[fd.key]) + '"' +
      (fd.mono ? ' style="font-family:var(--font-mono)"' : ' style="font-family:var(--font-sans)"') +
      (fd.placeholder ? ' placeholder="' + esc(fd.placeholder) + '"' : '') + '>' + desc + '</div>'
    );
  }

  function aboutCard() {
    const cfg = window.DYConfig;
    return (
      '<section class="card mb-4">' +
        '<div class="card__title mb-3">关于</div>' +
        '<div class="row-item__meta" style="flex-direction:column;align-items:flex-start;gap:8px">' +
          '<span>' + esc(cfg.APP.name) + ' ' + esc(cfg.APP.version) + ' · ' + esc(cfg.APP.fullName) + ' Web UI</span>' +
          '<span class="faint">以 E:\\录屏\\dysp\\dyxz 工程的官方桌面端为参考实现的网页版界面</span>' +
          '<span class="faint">数据模式：' + (window.DYApi.mode === 'mock' ? 'mock 演示数据' : '真实后端') + '</span>' +
        '</div>' +
      '</section>'
    );
  }

  function ledgerCard() {
    const rows = window.DYStore.ledger();
    const total = rows.reduce((a, b) => a + b.bytes, 0);
    return (
      '<section class="card">' +
        '<div class="card__title mb-3">数据台账</div>' +
        '<p class="small muted mb-3">全部交互数据统一存于 localStorage 的 <code>dyui.*</code> 键，' +
        '可导出、可重置，满足"数据有台账"要求。</p>' +
        (rows.length
          ? '<div class="table-wrap mb-3"><table class="table"><tbody>' +
            rows.map((r) => '<tr><td>' + esc(r.label) + '</td><td class="num">' +
              esc(U.bytes(r.bytes)) + '</td></tr>').join('') +
            '<tr><td><b>合计</b></td><td class="num"><b>' + esc(U.bytes(total)) + '</b></td></tr>' +
            '</tbody></table></div>'
          : '<p class="small faint mb-3">暂无记录。</p>') +
        '<div class="flex">' +
          '<button class="btn btn--ghost btn--sm" id="st-export">' + I.icon('file', 13) + '导出 JSON</button>' +
          '<button class="btn btn--danger btn--sm" id="st-resetdemo">' + I.icon('trash', 13) + '重置演示数据</button>' +
        '</div>' +
      '</section>'
    );
  }

  function bind(view) {
    /* 开关 */
    view.querySelectorAll('.switch[data-set]').forEach((sw) => {
      sw.addEventListener('click', () => {
        const key = sw.dataset.set;
        const cur = !!window.DYStore.state.settings[key];
        window.DYStore.updateSettings({ [key]: !cur });
        sw.setAttribute('aria-checked', String(!cur));
        C.toast('已保存：' + key, 'ok');
      });
    });
    /* 文本/数字（失焦保存） */
    view.querySelectorAll('input[data-set]').forEach((inp) => {
      inp.addEventListener('change', () => {
        const key = inp.dataset.set;
        let v = inp.value;
        if (inp.type === 'number') {
          v = parseInt(v, 10);
          if (isNaN(v)) v = window.DYConfig.DEFAULT_SETTINGS[key] || 0;
          inp.value = v;
        }
        window.DYStore.updateSettings({ [key]: v });
        C.toast('已保存', 'ok');
      });
    });
    /* 下拉 */
    view.querySelectorAll('select[data-set]').forEach((sel) => {
      sel.addEventListener('change', () => {
        window.DYStore.updateSettings({ [sel.dataset.set]: sel.value });
        C.toast('已保存', 'ok');
      });
    });
    /* 恢复默认 */
    view.querySelector('#st-reset').addEventListener('click', () => {
      C.confirm('恢复默认设置', '所有设置项将恢复为工程默认值（对应 config.example.yml），此操作会覆盖你的修改。', () => {
        window.DYStore.resetSettings();
        render(view);
        C.toast('已恢复默认设置', 'ok');
      }, '恢复', true);
    });
    /* 台账操作 */
    view.querySelector('#st-export').addEventListener('click', exportData);
    view.querySelector('#st-resetdemo').addEventListener('click', () => {
      C.confirm('重置演示数据', '将清空全部本地记录并恢复为内置演示数据，此操作不可撤销。', () => {
        window.DYStore.resetDemo();
        render(view);
        C.toast('已重置为演示数据', 'ok');
      }, '重置', true);
    });
  }

  function exportData() {
    const blob = new Blob([JSON.stringify(window.DYStore.exportAll(), null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'dyui-data-' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
    C.toast('数据已导出', 'ok');
  }

  window.DYRouter.register('settings', render);
})();
