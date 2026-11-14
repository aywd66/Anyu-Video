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

  /* —— bridge 模式：工程真实 config.yml（**可改**）+ 存储位置 + 只读全表 ——
   *
   * ⚠️⚠️ 2026-10-03 修的是一个「能看不能改」的坑：
   *   原来这里在 bridge 模式下（= 实际运行时）**整块换成了只读表格** ——
   *   schema 驱动的表单被丢弃，`bind()` 也不会被调用，页面上一个输入框都没有。
   *   原因是没有写入通道：只有 `GET /api/bridge/config`。
   *   结果：「一键更新每人取最新」「一键更新专用画质」这些设置在真实环境里
   *   **根本没法改**，只能去手工编辑 YAML。
   *
   * 现在的做法：**表单保留并可编辑**，值从真实 config.yml 读出来回填，
   *   改动经 `POST /api/bridge/config` 写回（后端白名单 + 逐键最小 diff + 先备份）。
   *   只读的全量表挪到下面，作为「看看还有哪些字段」的参考 —— 它本来就不是编辑器。
   */
  async function renderBridgeConfig(view) {
    const groups = document.getElementById('st-groups');
    if (!groups) return;
    groups.innerHTML = '<div class="section section--flush">' + C.skeletonRows(2) + '</div>';
    try {
      const [d, w] = await Promise.all([
        window.DYApi.bridgeConfig(),
        window.DYApi.bridgeWebuiSettings().catch(() => ({ settings: {} })),
      ]);
      if (!d.available) throw new Error(d.reason || 'config.yml 不可读');
      const cfgObj = d.config || {};

      /* 用真实配置回填表单（schema 的键名与 config.yml 的键名一致；
       * `increase.post` 这种嵌套的用点号路径取）。回填完再 bind，
       * 这样用户看到的就是工程里的真实值，而不是 DEFAULT_SETTINGS。 */
      const s = window.DYStore.state.settings;
      /* ⚠️ `cfg` 必须在这里取：`render()` 里的 `const cfg` 只在那个函数作用域内，
       *   renderBridgeConfig 是另一个函数 —— 直接用 `cfg` 会 ReferenceError
       *   （症状：设置页整块变成「读取工程配置失败：cfg is not defined」，
       *    一个输入框都没有。`node --check` 查不出这种跨作用域引用错误，
       *    是真浏览器跑出来的）。 */
      const cfg = window.DYConfig;
      const hydrate = (obj) => cfg.SETTINGS_SCHEMA.forEach((g) => g.fields.forEach((fd) => {
        if (fd.key.indexOf('.') > 0) {
          const [a, b] = fd.key.split('.');
          if (obj[a] && typeof obj[a] === 'object' && obj[a][b] !== undefined) s[fd.key] = obj[a][b];
        } else if (obj[fd.key] !== undefined && obj[fd.key] !== null
                   && typeof obj[fd.key] !== 'object') {
          s[fd.key] = obj[fd.key];
        }
      }));
      hydrate(cfgObj);
      /* 网页端专属键（不在 config.yml 里的，如 creators_update_*） */
      hydrate(w.settings || {});

      /* 重画表单分组（值已回填），使其可编辑 */
      groups.innerHTML =
        '<div>' +
          '<div class="card__title mb-3">可修改的设置</div>' +
          '<div class="small muted mb-3">改动会写回工程的 <code>config.yml</code> ' +
          '（写入前自动备份为 <code>config.yml.bak</code>）。' +
          '灰底只读表在下方，仅供查看其它字段。</div>' +
          cfg.SETTINGS_SCHEMA.map(groupCard).join('') +
        '</div>' +
        '<div>' + storagePanel() + readOnlyTable(cfgObj) + ledgerCard() + biliCookieCard() + '</div>';

      bind(view);              // ← 关键：bridge 模式也要绑事件，否则改了没反应
      bindStorage(view);
      bindLedger(view);
      bindBiliCookie(view);
    } catch (e) {
      groups.innerHTML = '<div class="error-box"><h3>读取工程配置失败</h3><p class="muted">' +
        esc(String(e.message || e)) + '</p></div>';
    }
  }

  /* 只读全表：让用户看清 config.yml 里还有哪些字段（不提供编辑 —— 编辑走上面的表单）。 */
  function readOnlyTable(cfgObj) {
    const entries = Object.keys(cfgObj).filter((k) => typeof cfgObj[k] !== 'object');
    const nested = Object.keys(cfgObj).filter((k) => typeof cfgObj[k] === 'object' && cfgObj[k]);
    const row = (k, v) => '<tr><td style="font-weight:600;white-space:nowrap">' + esc(k) + '</td>' +
      '<td><code class="mono small" style="word-break:break-all">' + esc(String(v)) + '</code></td></tr>';
    return (
      '<section class="section mt-4"><div class="card__title mb-3">config.yml 全量字段（只读 · 敏感项已脱敏）</div>' +
        '<div class="table-wrap"><table class="table"><tbody>' +
        entries.map((k) => row(k, cfgObj[k])).join('') +
        nested.map((k) => row(k, JSON.stringify(cfgObj[k]))).join('') +
        '</tbody></table></div>' +
      '</section>'
    );
  }

  /* 台账按钮（bridge 与 mock 两种模式都要能点）。 */
  function bindLedger(view) {
    const ex = view.querySelector('#st-export');
    if (ex) ex.addEventListener('click', exportData);
    const rd = view.querySelector('#st-resetdemo');
    if (rd) rd.addEventListener('click', () => {
      C.confirm('重置演示数据', '将清空本地演示记录并恢复为内置演示数据；工程数据库不受影响。', () => {
        window.DYStore.resetDemo();
        C.toast('已重置本地演示数据', 'ok');
      }, '重置', true);
    });
  }


  /* ================= 存储位置面板 =================
   * 六段按操作顺序排：路径 → 检查 → 占用 → 迁移选项 → 未保存条 → 迁移进度。
   * 「搬完校验」默认关：同盘迁移只改目录项、根本没拷数据，开着纯属浪费时间；
   * 跨盘才建议打开（文案直接写明，不做默认替用户做主）。 */
  const sto = { baseline: '', polling: null };

  function storagePanel() {
    return (
      '<section class="section section--flush" id="sto-panel">' +
        '<div class="card__title">存储位置</div>' +
        '<p class="small muted" style="margin:6px 0 0">下载产物的根目录，对应 <code>config.yml</code> 的 <code>path</code>。' +
        '相对路径基于工程根目录；填绝对路径（如 <code>D:\\Media</code>）可把产物单独挪到另一块盘。</p>' +

        '<div class="sto-row">' +
          '<input class="input" id="sto-path" spellcheck="false" autocomplete="off" ' +
            'style="font-family:var(--font-mono)" placeholder="例如 downloads/douyin 或 D:\\Media\\douyin">' +
          '<button class="btn btn--ghost" id="sto-check">检查</button>' +
        '</div>' +
        '<div id="sto-checkout" class="sto-checkout" aria-live="polite"></div>' +

        '<div class="sto-sec">' +
          '<div class="sto-label">当前占用 <span class="faint num" id="sto-usagenow"></span></div>' +
          '<div class="sto-bar" id="sto-bar" hidden></div>' +
          '<div class="sto-legend" id="sto-legend"></div>' +
        '</div>' +

        '<div class="sto-sec" id="sto-migrate-opts">' +
          '<div class="sto-label">更改路径时</div>' +
          '<label class="sto-opt"><input type="checkbox" id="sto-migrate" checked>' +
            '<span>搬移已有产物到新位置（不搬则旧文件留在原地）</span></label>' +
          '<label class="sto-opt"><input type="checkbox" id="sto-verify">' +
            '<span>搬完逐文件校验大小 <span class="faint">跨盘建议打开；同盘只改目录索引、没有拷贝过程，开了没用</span></span></label>' +
        '</div>' +

        '<div class="sto-dirty" id="sto-dirty" hidden>' +
          '<svg class="ico" width="14" height="14"><use href="#i-info"/></svg>' +
          '<span>路径已修改，尚未生效。保存后新任务写入新位置；进行中的任务不受影响。</span>' +
          '<span class="sto-dirty__act">' +
            '<button class="btn btn--sm" id="sto-discard">丢弃</button>' +
            '<button class="btn btn--primary btn--sm" id="sto-apply">保存并生效</button>' +
          '</span>' +
        '</div>' +

        '<div class="sto-mig" id="sto-mig" hidden>' +
          '<div class="sto-label">正在搬移产物… <span class="num" id="sto-migpct"></span></div>' +
          '<div class="sto-bar"><div class="sto-bar__fill" id="sto-migfill" style="width:0%"></div></div>' +
          '<div class="sto-legend"><span class="mono small" id="sto-migfile"></span></div>' +
          '<button class="btn btn--sm" id="sto-cancel">取消迁移</button>' +
        '</div>' +
      '</section>'
    );
  }

  function bindStorage(view) {
    const $ = (id) => document.getElementById(id);
    if (!$('sto-panel')) return;
    window.DYApi.storage().then((d) => {
      if (!d.available) { $('sto-path').disabled = true; return; }
      sto.baseline = d.raw;
      $('sto-path').value = d.raw;
      renderUsage(d.usage);
      refreshDirty();
      pollMigration(true);
    }).catch(() => {});

    $('sto-path').addEventListener('input', refreshDirty);
    $('sto-discard').addEventListener('click', () => {
      $('sto-path').value = sto.baseline;
      refreshDirty();
      $('sto-checkout').innerHTML = '';
    });
    $('sto-check').addEventListener('click', async () => {
      const out = $('sto-checkout');
      out.innerHTML = '<span class="faint small">检查中…</span>';
      const r = await window.DYApi.storageCheck($('sto-path').value.trim());
      if (!r.ok) {
        out.innerHTML = '<span class="sto-badge sto-badge--bad">不可用</span> ' +
          '<span class="small">' + esc(r.error || '未知原因') + '</span>';
        return;
      }
      out.innerHTML =
        '<span class="sto-badge sto-badge--ok">可用</span>' +
        '<span class="small"> ' + esc(r.abs) + '</span>' +
        (r.free_gb != null ? '<span class="small faint"> · 剩余 ' + esc(r.free_gb) + ' GB</span>' : '') +
        '<span class="small ' + (r.same_disk ? 'faint' : '') + '" style="margin-left:8px">' +
        (r.same_disk ? '同盘迁移：只改目录索引，瞬间完成' : '<b>跨盘迁移：将真实拷贝数据</b>') + '</span>' +
        (r.note ? '<div class="small faint">' + esc(r.note) + '</div>' : '');
      refreshDirty();
    });
    $('sto-apply').addEventListener('click', async () => {
      const btn = $('sto-apply');
      btn.disabled = true;
      try {
        const r = await window.DYApi.storageApply(
          $('sto-path').value.trim(), $('sto-migrate').checked, $('sto-verify').checked);
        sto.baseline = $('sto-path').value.trim();
        refreshDirty();
        if (r.migrating) {
          C.toast('已保存，开始搬移产物', 'ok');
          pollMigration(false);
        } else {
          C.toast(r.same_path ? '路径未变化' : '已保存并生效', 'ok');
        }
      } catch (e) {
        C.toast(e.message, 'err');
      } finally {
        btn.disabled = false;
      }
    });
    $('sto-cancel').addEventListener('click', async () => {
      try { await window.DYApi.storageCancel(); C.toast('已请求取消', 'ok'); }
      catch (e) { C.toast(e.message, 'err'); }
    });
  }

  function refreshDirty() {
    const $ = (id) => document.getElementById(id);
    if (!$('sto-dirty')) return;
    const changed = $('sto-path').value.trim() !== sto.baseline;
    $('sto-dirty').hidden = !changed;
    // 没改路径时迁移选项没有意义，收起来免得误导
    $('sto-migrate-opts').hidden = !changed;
  }

  function renderUsage(usage) {
    const $ = (id) => document.getElementById(id);
    if (!$('sto-bar')) return;
    const total = usage && usage.total || 0;
    $('sto-usagenow').textContent = total ? '· ' + U.bytes(total) +
      (usage.partial ? '（部分统计）' : '') : '· 目录为空';
    if (!total) { $('sto-bar').hidden = true; $('sto-legend').innerHTML = ''; return; }
    const COLORS = ['var(--brand)', '#4E9DA0', '#7FB8BA', '#A8CED0', '#CDE2E3', '#E4EEEF', 'var(--line)'];
    $('sto-bar').hidden = false;
    $('sto-bar').innerHTML = (usage.groups || []).map((g, i) =>
      '<div class="sto-bar__seg" style="width:' +
      Math.max(1, (g.bytes / total) * 100).toFixed(2) + '%;background:' + COLORS[i % COLORS.length] + '"' +
      ' title="' + esc(g.name || '（根目录散件）') + '"></div>').join('');
    $('sto-legend').innerHTML = (usage.groups || []).map((g, i) =>
      '<span class="sto-key"><i style="background:' + COLORS[i % COLORS.length] + '"></i>' +
      esc(g.name || '（根目录散件）') + ' <span class="num faint">' + U.bytes(g.bytes) + '</span></span>').join('');
  }

  function pollMigration(initial) {
    const $ = (id) => document.getElementById(id);
    if (!window.DYApi.isBridge()) return;
    const tick = async () => {
      // 页面已切走时（路由重绘 innerHTML）立刻放弃，防止空引用；
      // 轮询本身留给下一次进入设置页时复用。
      if (!document.getElementById('sto-mig')) return;
      let m;
      try { m = await window.DYApi.storageMigrate(); } catch (e) { return; }
      const running = m && m.status === 'running';
      $('sto-mig').hidden = !running && !(m && m.status && m.status !== 'idle' && m.error);
      if (!running && sto.polling) { clearInterval(sto.polling); sto.polling = null; }
      if (!m || m.status === 'idle' || (!running && !m.total)) { $('sto-mig').hidden = true; return; }
      const pct = m.total ? Math.round((m.done / m.total) * 100) : 0;
      $('sto-migpct').textContent = m.done + '/' + m.total + '（' + pct + '%）';
      $('sto-migfill').style.width = pct + '%';
      $('sto-migfile').textContent = m.current || '';
      if (m.status === 'done') {
        $('sto-migpct').textContent = '完成';
        $('sto-migfill').style.width = '100%';
        $('sto-migfile').textContent = '';
        if (sto.polling) { clearInterval(sto.polling); sto.polling = null; }
        C.toast('产物迁移完成', 'ok');
      } else if (m.status === 'failed' || m.status === 'canceled') {
        $('sto-migfile').innerHTML = '<span style="color:var(--danger)">' + esc(m.error || '') + '</span>';
        if (sto.polling) { clearInterval(sto.polling); sto.polling = null; }
      }
    };
    if (initial) { tick(); }   // 打开页面时若已有迁移在跑，立即接上进度
    if (!sto.polling) sto.polling = setInterval(tick, 700);
  }

  function groupCard(g) {
    return (
      '<section class="section">' +
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
      /* ⚠️ `blankLabel`（2026-10-03 加）：给「留空 = 走默认逻辑」这类语义一个可选项。
       *    没有它的话，浏览器会把 select 显示成**第一项**，用户以为选了它、其实
       *    值已经变了 —— 典型表现是「留空用原画」悄悄变成了「用最高清」。
       *    值存空串，`updateQuality()` 那边正好按 falsy 处理。 */
      const blank = fd.blankLabel
        ? '<option value=""' + (s[fd.key] ? '' : ' selected') + '>' + esc(fd.blankLabel) + '</option>'
        : '';
      return (
        '<div class="form-item"><div class="form-item__label">' + esc(fd.label) + '</div>' +
        '<select class="input" data-set="' + esc(fd.key) + '">' + blank +
        options.map((o) => '<option value="' + esc(o.id) + '"' + (s[fd.key] === o.id ? ' selected' : '') + '>' +
          esc(o.label) + '</option>').join('') +
        '</select>' + desc + '</div>'
      );
    }
    return (
      '<div class="form-item"><div class="form-item__label">' + esc(fd.label) + '</div>' +
      '<input class="input" data-set="' + esc(fd.key) + '" type="' + (fd.type === 'number' ? 'number' : 'text') + '" ' +
      (fd.min !== undefined ? 'min="' + fd.min + '" max="' + fd.max + '"' : '') +
      /* ⚠️ `readOnly`（2026-10-03）：有些字段**只在这里展示**，改动要走别处。
       *   `path` 就是这样 —— 它有专门的「存储位置」面板（改它会搬移已有产物，
       *   后端刻意不放在普通设置的写白名单里）。若这里还让它可编辑，用户改了会被
       *   后端拒绝，界面白提示一次、之后每次都失败 = 死路。 */
      (fd.readOnly ? ' readonly title="这一项请在下方「存储位置」面板里改"' : '') +
      (fd.readOnly ? ' style="opacity:.75"' : '') +
      ' value="' + esc(s[fd.key] === undefined ? '' : s[fd.key]) + '"' +
      (fd.mono ? ' style="font-family:var(--font-mono)' + (fd.readOnly ? ';opacity:.75' : '') + '"'
               : ' style="font-family:var(--font-sans)' + (fd.readOnly ? ';opacity:.75' : '') + '"') +
      (fd.placeholder ? ' placeholder="' + esc(fd.placeholder) + '"' : '') + '>' + desc + '</div>'
    );
  }

  function aboutCard() {
    const cfg = window.DYConfig;
    return (
      '<section class="section">' +
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
      '<section class="section">' +
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

  /* 改一项设置。bridge 模式下要**写回工程 config.yml**（否则改了只存在
   * 浏览器 localStorage 里，重开工具箱就丢，而且下载器根本不读它）。
   * 失败时把界面回滚成改之前的值 —— 不能「界面显示已保存、实际没落盘」。 */
  async function saveOne(view, key, value, prev, applyLocal) {
    if (!window.DYApi.isBridge()) {          // mock 模式：只存浏览器本地
      applyLocal();
      C.toast('已保存（仅本机浏览器）', 'ok');
      return;
    }
    try {
      const r = await window.DYApi.bridgeSaveConfig({ [key]: value });
      applyLocal();                            // 后端确认后才更新本地状态
      if (r.missed && r.missed.length) {
        C.toast('已保存，但 config.yml 里没找到「' + r.missed.join(',') + '」对应行，未写入', 'err');
      } else {
        C.toast('已写入 config.yml：' + key, 'ok');
      }
    } catch (e) {
      /* 回滚界面：把控件恢复成改之前的值 */
      view.querySelectorAll('[data-set="' + key + '"]').forEach((el) => {
        if (el.classList.contains('switch')) el.setAttribute('aria-checked', String(!!prev));
        else el.value = prev === undefined || prev === null ? '' : String(prev);
      });
      C.toast('保存失败：' + String((e && e.message) || e), 'err');
    }
  }

  function bind(view) {
    /* 开关 */
    view.querySelectorAll('.switch[data-set]').forEach((sw) => {
      sw.addEventListener('click', () => {
        const key = sw.dataset.set;
        const cur = !!window.DYStore.state.settings[key];
        const next = !cur;
        saveOne(view, key, next, cur,
                () => window.DYStore.updateSettings({ [key]: next }));
        sw.setAttribute('aria-checked', String(next));
      });
    });
    /* 文本/数字（失焦保存） */
    view.querySelectorAll('input[data-set]').forEach((inp) => {
      inp.addEventListener('change', () => {
        const key = inp.dataset.set;
        const prev = window.DYStore.state.settings[key];
        let v = inp.value;
        if (inp.type === 'number') {
          v = parseInt(v, 10);
          if (isNaN(v)) v = window.DYConfig.DEFAULT_SETTINGS[key] || 0;
          inp.value = v;
        }
        saveOne(view, key, v, prev,
                () => window.DYStore.updateSettings({ [key]: v }));
      });
    });
    /* 下拉 */
    view.querySelectorAll('select[data-set]').forEach((sel) => {
      sel.addEventListener('change', () => {
        const key = sel.dataset.set;
        const prev = window.DYStore.state.settings[key];
        const v = sel.value;
        saveOne(view, key, v, prev,
                () => window.DYStore.updateSettings({ [key]: v }));
      });
    });
    /* 恢复默认 */
    const rst = view.querySelector('#st-reset');
    if (rst) rst.addEventListener('click', () => {
      if (window.DYApi.isBridge()) {
        /* bridge 模式（2026-11-08 起）：POST /api/bridge/config/reset 把
         * 全部可写键写回工程默认值（default_config.py），写前自动备份。 */
        C.confirm('恢复默认设置',
          '所有设置项将恢复为工程默认值（default_config.py），你当前的修改会被覆盖：\n\n' +
          '· config.yml 会先自动备份为 config.yml.bak\n' +
          '· 「存储位置」与「数据库路径」不在重置范围（环境相关键，防误伤真实数据）\n' +
          '· 数据库路径恢复出厂值后需重启下载器才按新路径读取',
          async () => {
            rst.disabled = true;
            try {
              await window.DYApi.bridgeResetConfig();
              C.toast('已恢复默认设置，正在刷新…', 'ok');
              /* 立即整页刷新：所有控件直接反映默认配置（用户要求） */
              setTimeout(() => window.location.reload(), 700);
            } catch (e) {
              rst.disabled = false;
              C.toast(e.message || '恢复失败', 'err');
            }
          }, '恢复默认', true);
        return;
      }
      C.confirm('恢复默认设置', '所有设置项将恢复为工程默认值（对应 config.example.yml），此操作会覆盖你的修改。', () => {
        window.DYStore.resetSettings();
        render(view);
        C.toast('已恢复默认设置', 'ok');
      }, '恢复', true);
    });
    /* 台账操作 */
    const ex = view.querySelector('#st-export');
    if (ex) ex.addEventListener('click', exportData);
    const rd = view.querySelector('#st-resetdemo');
    if (rd) rd.addEventListener('click', () => {
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

  /* ================= 哔哩哔哩 Cookie 管理 =================
   * 扫码获取（推荐）/ 粘贴 / 清除。扫码走官方 passport 接口，成功后由后端直接
   * 落盘到 modules/bilibili/.cookies.json，bilibili_headless 每次任务启动时读取；
   * 环境变量 BILI_COOKIE 优先于文件。 */
  function biliCookieCard() {
    return (
      '<section class="section section--flush" id="bkc-panel">' +
        '<div class="card__title">哔哩哔哩 Cookie</div>' +
        '<p class="small muted">登录后可下载 1080P / 大会员画质，并显著降低主页批量下载被风控拦截的概率。' +
        '点「扫码获取」用手机 App 扫一下即可，不用再手动抄 Cookie。</p>' +
        '<div class="small muted mb-2" id="bkc-status">读取中 …</div>' +
        '<textarea class="input" id="bkc-input" rows="3" spellcheck="false" ' +
          'placeholder="也可以手动粘贴整段 Cookie（SESSDATA=...; buvid3=...; ...）" style="font-family:var(--mono,monospace);font-size:12px"></textarea>' +
        '<div class="flex mt-3" style="gap:8px">' +
          '<button class="btn btn--primary btn--sm" id="bkc-qr">' + I.icon('key', 13) + '扫码获取</button>' +
          '<button class="btn btn--ghost btn--sm" id="bkc-save">' + I.icon('check', 13) + '保存粘贴内容</button>' +
          '<button class="btn btn--ghost btn--sm" id="bkc-clear">' + I.icon('trash', 13) + '清除</button>' +
        '</div>' +
      '</section>'
    );
  }

  function bindBiliCookie(view) {
    const status = view.querySelector('#bkc-status');
    const input = view.querySelector('#bkc-input');
    if (!status || !input) return;

    const refresh = async () => {
      try {
        const d = await window.DYApi.biliCookieGet();
        if (d.has_cookie) {
          status.innerHTML = '<span class="dot dot--green"></span>已登录' +
            (d.uname ? '（' + esc(d.uname) + '）' : '') +
            '，更新于 ' + esc(d.updated_at || '时间未知') +
            '：<code class="mono">' + esc(d.cookie_masked || '') + '</code>';
        } else {
          status.innerHTML = '<span class="dot dot--faint"></span>未配置 —— 当前按游客身份下载（最高 720P）';
        }
      } catch (e) {
        status.innerHTML = '<span class="dot dot--amber"></span>读取失败：' + esc(String(e.message || e));
      }
    };

    // 扫码获取：二维码弹窗 + 轮询，成功后后端已落盘，这里只需刷新状态
    view.querySelector('#bkc-qr').addEventListener('click', () => {
      C.biliLogin(() => { refresh(); });
    });

    view.querySelector('#bkc-save').addEventListener('click', async () => {
      const v = input.value.trim();
      if (!v) { C.toast('请先粘贴 Cookie 内容，或直接用「扫码获取」', 'err'); return; }
      try {
        await window.DYApi.biliCookieSet(v);
        input.value = '';
        await refresh();
        C.toast('哔哩哔哩 Cookie 已保存，下一个任务生效', 'ok');
      } catch (e) {
        C.toast('保存失败：' + (e.message || e), 'err');
      }
    });
    view.querySelector('#bkc-clear').addEventListener('click', () => {
      C.confirm('清除 Cookie', '将删除已保存的哔哩哔哩 Cookie，恢复游客身份下载。', async () => {
        try {
          await window.DYApi.biliCookieSet('');
          await refresh();
          C.toast('已清除', 'ok');
        } catch (e) {
          C.toast('清除失败：' + (e.message || e), 'err');
        }
      }, '清除', true);
    });
    refresh();
  }

  window.DYRouter.register('settings', render);
})();
