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

    /**
     * 环境体检：ffmpeg / 运行时 / Cookie / 磁盘。
     * mock 模式（双击打开 index.html）拿不到本机事实，
     * 返回 null，顶栏状态灯会如实显示「未知」而不是假装正常。
     */
    async env() {
      if (!this.isBridge()) return null;
      const r = await fetch('/api/bridge/env', { cache: 'no-store' });
      if (!r.ok) throw new Error('环境体检异常 ' + r.status);
      return r.json();
    },

    /* ================= 存储位置（读 / 校验 / 应用 + 迁移进度） ================= */

    async storage() {
      const r = await fetch('/api/bridge/storage', { cache: 'no-store' });
      if (!r.ok) throw new Error('存储信息读取失败 ' + r.status);
      return r.json();
    },

    async storageCheck(path) {
      const r = await fetch('/api/bridge/storage/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      return r.json();
    },

    /** 应用新路径；migrate=true 时同时启动旧产物迁移。失败时后端返回 {error}。 */
    async storageApply(path, migrate, verify) {
      const r = await fetch('/api/bridge/storage/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, migrate: !!migrate, verify: !!verify }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '保存失败 ' + r.status);
      return d;
    },

    async storageMigrate() {
      const r = await fetch('/api/bridge/storage/migrate', { cache: 'no-store' });
      return r.json();
    },

    async storageCancel() {
      const r = await fetch('/api/bridge/storage/cancel', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '取消失败');
      return d;
    },

    /* ================= 直播录制（桌面模块 4 的状态总览 + 唤起入口） ================= */

    /** 直播录制模块状态；mock 模式返回 null（如实，不假装有录制能力）。 */
    async live() {
      if (!this.isBridge()) return null;
      const r = await fetch('/api/bridge/live', { cache: 'no-store' });
      if (!r.ok) throw new Error('直播录制状态读取失败 ' + r.status);
      return r.json();
    },

    /** 唤起桌面 LiveRecorder GUI。失败抛 Error(error)。 */
    async liveLaunch() {
      const r = await fetch('/api/bridge/live/launch', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '唤起失败 ' + r.status);
      return d;
    },

    /** 打开录像目录。dir 为空打开录像根目录；传房间行的 output_dir 打开该房间的录制文件夹。 */
    async liveOpenDir(dir) {
      const r = await fetch('/api/bridge/live/open-dir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dir: dir || '' }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '打开失败 ' + r.status);
      return d;
    },

    /** 把直播链接加入桌面 LiveRecorder 监控清单（写 config.json，静默不弹 GUI；录制器运行中会自动并入）。 */
    async liveAddRoom(url, remark) {
      const r = await fetch('/api/bridge/live/add-room', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: (url || '').trim(), remark: remark || '' }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '添加失败 ' + r.status);
      return d;
    },

    /* ================= 录制内核（无界面守护进程，网页全权控制） =================
     *
     * 这几个是「把录制窗口搬进网页」的通道。守护进程只 bind 在回环地址的
     * 8519 上，网页不能直连，全部经桥接的 /api/bridge/live/daemon/* 代理。
     *
     * 约定：守护进程没跑时后端如实回 503 + {daemon:false, hint}，
     * 调用方据此把按钮置灰并提示「启动录制内核」，**不要**当成加载失败。
     */

    /** 确保录制内核在线（进入页面时调一次）。autostart=false 时只查不拉。 */
    async liveKernelEnsure(autostart = true) {
      const r = await fetch('/api/bridge/live/daemon/ensure', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ autostart: !!autostart }),
      });
      const d = await r.json().catch(() => ({}));
      // 503 = 内核没跑（或没拉起来）。这是正常状态，交给调用方渲染，不抛。
      if (!r.ok && r.status !== 503) throw new Error(d.error || '内核检查失败 ' + r.status);
      return d;
    },

    /** 录制内核完整状态：房间实时状态 + 设置 + 登录态 + 扫码会话。 */
    async liveKernelStatus() {
      const r = await fetch('/api/bridge/live/daemon/status', { cache: 'no-store' });
      const d = await r.json().catch(() => ({}));
      if (r.status === 503) return { ...d, daemon: false };
      if (!r.ok) throw new Error(d.error || '状态读取失败 ' + r.status);
      return d;
    },

    /** 录制内核增量日志。since 为空/0 时拉全部（受环形缓冲上限约束）。 */
    async liveKernelLogs(since = 0) {
      const r = await fetch('/api/bridge/live/daemon/logs?since=' + (since || 0), { cache: 'no-store' });
      const d = await r.json().catch(() => ({}));
      if (r.status === 503) return { seq: since, items: [], daemon: false };
      if (!r.ok) throw new Error(d.error || '日志读取失败 ' + r.status);

      /* 字段契约归一化：内核环形缓冲返回的是 {seq, level, text}（text 已含
       * 「HH:MM:SS logger: 消息」前缀），而页面模板按 {time, message} 渲染。
       * 早先两边没对齐，结果每行只剩一对空方括号「[]」，看起来像日志为空。
       * 这里统一拆出 time / message，同时兼容内核未来直接给这两个字段。 */
      const items = (Array.isArray(d.items) ? d.items : []).map((it) => {
        if (!it || typeof it !== 'object') return it;
        if (it.message) return it;
        const text = String(it.text || '');
        const m = text.match(/^(\d{1,2}:\d{2}:\d{2})\s+([\s\S]*)$/);
        return Object.assign({}, it, {
          time: it.time || (m ? m[1] : ''),
          message: m ? m[2] : text,
        });
      });
      return Object.assign({}, d, { items });
    },

    /** 通用动作转发：path 形如 'monitor/start'、'rooms/toggle'。 */
    async liveKernelAction(path, body) {
      const r = await fetch('/api/bridge/live/daemon/' + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        const e = new Error(d.error || '操作失败 ' + r.status);
        e.status = r.status;
        e.offline = !!d.daemon === false || r.status === 503;
        throw e;
      }
      return d;
    },

    /** 开始 / 停止监控。 */
    liveKernelStart() { return this.liveKernelAction('monitor/start'); },
    liveKernelStop() { return this.liveKernelAction('monitor/stop'); },
    liveKernelCheck() { return this.liveKernelAction('monitor/check'); },

    /** 监控清单增删改。 */
    liveKernelAddRoom(url, remark) {
      return this.liveKernelAction('rooms/add', { url: (url || '').trim(), remark: remark || '' });
    },
    liveKernelRemoveRoom(key) { return this.liveKernelAction('rooms/remove', { key }); },
    liveKernelToggleRoom(key, enabled) {
      return this.liveKernelAction('rooms/toggle', { key, enabled: !!enabled });
    },
    /** 让某个房间「停止当前录制」（不影响监控清单里的启用状态）。 */
    liveKernelStopRoom(key) { return this.liveKernelAction('rooms/stop', { key }); },

    /** 更新录制设置（后端白名单，未列字段会被拒绝）。 */
    liveKernelSettings(patch) { return this.liveKernelAction('settings', patch || {}); },

    /** 停止录制内核（会先收尾合并已录分片，可能耗时）。 */
    liveKernelShutdown() { return this.liveKernelAction('shutdown'); },

    /* ---- 网页内扫码登录 ---- */

    /** 扫码会话状态（含 base64 二维码与登录态）。内核没跑时 state='unavailable'。 */
    async liveKernelLoginStatus() {
      const r = await fetch('/api/bridge/live/daemon/login/status', { cache: 'no-store' });
      const d = await r.json().catch(() => ({}));
      if (r.status === 503) return { state: 'unavailable', daemon: false };
      if (!r.ok) throw new Error(d.error || '登录状态读取失败 ' + r.status);
      return d;
    },

    /** 开始扫码登录（platform: 'kuaishou' | 'douyin'）。 */
    liveKernelLoginStart(platform) {
      return this.liveKernelAction('login/start', { platform });
    },
    /** 刷新二维码。 */
    liveKernelLoginRefresh() { return this.liveKernelAction('login/refresh'); },
    /** 取消扫码会话。 */
    liveKernelLoginCancel() { return this.liveKernelAction('login/cancel'); },

    /** 录像文件下载地址（供 <a download> 或 window.open 用）。 */
    liveKernelDownloadUrl(relPath) {
      return '/api/bridge/live/daemon/download?path=' + encodeURIComponent(relPath || '');
    },

    /* ================= 快手 / 直链（模块 2 / 3 页面内直接操作） ================= */

    /** 唤起桌面快手下载器，并自动填入链接开始下载（无需手动粘贴）。 */
    async kuaishouLaunch(url) {
      const r = await fetch('/api/bridge/kuaishou/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: (url || '').trim() }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '唤起失败 ' + r.status);
      return d;
    },

    /** 快手登录态 + 模块就绪状态（只读）。 */
    async kuaishouStatus() {
      const r = await fetch('/api/bridge/kuaishou/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('状态读取失败 ' + r.status);
      return r.json();
    },

    /** 唤起快手扫码登录窗口（登录态落盘，之后即可后台下载）。 */
    async kuaishouLogin() {
      const r = await fetch('/api/bridge/kuaishou/login', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '唤起失败 ' + r.status);
      return d;
    },

    /** 快手后台下载：纳入统一任务队列，进度进任务中心。mode: 'all' | '1'。 */
    async tyDownload(url, mode) {
      const r = await fetch('/api/bridge/ty/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: (url || '').trim(), mode: mode || 'all' }),
      });
      const d = await r.json();
      if (!r.ok) {
        const e = new Error(d.error || '提交失败 ' + r.status);
        e.needLogin = !!d.need_login;
        throw e;
      }
      return d;
    },

    /** yt-dlp 万能下载：提交一个 URL 到统一任务队列（opts 见 bridge 的白名单）。 */
    async ytdlpDownload(url, opts) {
      const r = await fetch('/api/bridge/ytdlp/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: (url || '').trim(), opts: opts || {} }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '提交失败 ' + r.status);
      return d;
    },

    /** yt-dlp 万能下载：下载前预览（标题/作者/时长/播放列表条目/可选画质）。 */
    async ytdlpPreview(url) {
      const r = await fetch('/api/bridge/ytdlp/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: (url || '').trim() }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '预览失败 ' + r.status);
      return d;
    },

    /** yt-dlp 高级配置（原生 conf）：读取。 */
    async ytdlpConfGet() {
      const r = await fetch('/api/bridge/ytdlp/conf', { cache: 'no-store' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '读取失败 ' + r.status);
      return d;
    },

    /** yt-dlp 高级配置：保存（服务端自动留 .bak）。 */
    async ytdlpConfSave(content) {
      const r = await fetch('/api/bridge/ytdlp/conf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: String(content || '') }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '保存失败 ' + r.status);
      return d;
    },

    /** yt-dlp 一键升级（kuaishou-env 内 pip install -U yt-dlp）。 */
    async ytdlpUpdate() {
      const r = await fetch('/api/bridge/ytdlp/update', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '升级失败 ' + r.status);
      return d;
    },

    /** 快手封面：触发后台 ffmpeg 抽帧（幂等；已在跑则返回当前进度）。 */
    async ksCoversEnsure(pids) {
      const r = await fetch('/api/bridge/ks/covers/ensure', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pids: pids || [] }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || '封面生成触发失败 ' + r.status);
      return d;
    },

    /** 快手封面生成进度（running/done/total/failed）。 */
    async ksCoversStatus() {
      const r = await fetch('/api/bridge/ks/covers/status', { cache: 'no-store' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || '封面进度读取失败 ' + r.status);
      return d;
    },

    async kuaishouDownload(url, mode) {
      const r = await fetch('/api/bridge/kuaishou/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: (url || '').trim(), mode: mode || 'all' }),
      });
      const d = await r.json();
      if (!r.ok) {
        const e = new Error(d.error || '提交失败 ' + r.status);
        e.needLogin = !!d.need_login;
        throw e;
      }
      return d.task;
    },

    /** Stripchat 直播录制（模块 6）：长驻进程监护，不走统一任务队列。
     * start 在已在录时抛错（409）；status 返回 {running, info, log, outdir}。 */
    async scStart(url) {
      const r = await fetch('/api/bridge/stripchat/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: (url || '').trim() }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '启动失败 ' + r.status);
      return d;
    },
    async scStop() {
      const r = await fetch('/api/bridge/stripchat/stop', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '停止失败 ' + r.status);
      return d;
    },
    async scStatus() {
      const r = await fetch('/api/bridge/stripchat/status');
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '查询失败 ' + r.status);
      return d;
    },
    async scOpenDir() {
      const r = await fetch('/api/bridge/stripchat/open-dir', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '打开失败 ' + r.status);
      return d;
    },

    /** 直链下载：后台跑 clawfetch，纳入统一任务队列。返回 task。
     *
     * url 可以是单个链接，也可以是多行文本 —— 后端会抽出全部链接并合并成
     * **一个批量任务**（clawfetch 原生批量：异域并行、同域自动串行）。
     * 也可以直接传字符串数组 urls，走同一条路。
     *
     * opts.segments = 1 表示「兼容弱源站」：直接让 clawfetch 用单连接起跑，
     * 跳过"先开 8 个、撞 502 再往下退"那段白等。不传 = auto 自适应（默认）。
     * opts.jobs = 1..4：多链接时的并发路数；不传 = auto（同时 2 个）。 */
    async directDownload(url, opts) {
      const body = Array.isArray(url)
        ? { urls: url }
        : { url: (url || '').trim() };
      if (opts && opts.segments === 1) body.segments = 1;
      if (opts && opts.jobs >= 1 && opts.jobs <= 4) body.jobs = opts.jobs;
      const r = await fetch('/api/bridge/direct/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '提交失败 ' + r.status);
      return d.task;
    },

    /* ================= 统一启动台（五个 .bat 入口的共享后端） ================= */

    /** 启动台总览：各工具的模块/运行时/产物目录就绪态。mock 模式返回 null。 */
    async launcher() {
      if (!this.isBridge()) return null;
      const r = await fetch('/api/bridge/launcher', { cache: 'no-store' });
      if (!r.ok) throw new Error('启动台状态读取失败 ' + r.status);
      return r.json();
    },

    /** 唤起某个工具的桌面 GUI（当前仅 live / kuaishou 有独立窗口）。 */
    async toolLaunch(kind) {
      const r = await fetch('/api/bridge/tool/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: kind }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '唤起失败 ' + r.status);
      return d;
    },

    /** 打开某个工具的产物目录（不存在时后端报 404，不悄悄创建）。 */
    async toolOpenDir(kind) {
      const r = await fetch('/api/bridge/tool/open-dir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: kind }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '打开失败 ' + r.status);
      return d;
    },

    /** 改某个工具的产物目录（写进该工具自己的配置；path 为空 = 恢复默认）。 */
    async toolSetDir(kind, path) {
      const r = await fetch('/api/bridge/tool/set-dir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: kind, path: (path || '').trim() }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '保存失败 ' + r.status);
      return d;
    },

    /**
     * 弹系统「选择文件夹」对话框。返回 {ok, path} 或 {cancelled: true}。
     * 对话框由桥接服务进程弹出；受限环境下会返回 error，调用方要提示改用手动输入。
     */
    async toolPickDir(start) {
      const r = await fetch('/api/bridge/tool/pick-dir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: start || '' }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '打开选择框失败 ' + r.status);
      // 取消是正常结果（200 + cancelled）；但「想弹却弹不出来」也是 200，
      // 只有 error 字段能区分 —— 这种情况必须把原因告诉用户，让他改手打路径。
      if (d && d.error) throw new Error(d.error);
      return d;
    },

    /** 工具环境自检（当前仅 live 有）。返回 {ok, code, output}。 */
    async toolSelftest(kind) {
      const r = await fetch('/api/bridge/tool/selftest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: kind || 'live' }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '自检失败 ' + r.status);
      return d;
    },

    /** 只解析直链不下载。bridge 模式下没有对应后台任务，如实报「需桌面端」。 */
    async directResolve(url) {
      if (!this.isBridge()) return { output: '' };
      // clawfetch 的 --resolve 是前台一次性输出，桥接服务未提供该端点；
      // 这里不伪造结果，明确告知走独立入口。
      throw new Error('「只解析不下载」请在桌面端使用：双击「启动程序下载.bat」并加 --resolve 参数');
    },

    /** 统一任务队列（供启动台最近任务用）。返回 {tasks, history}。 */
    async tasks() {
      if (this.isBridge()) {
        const r = await fetch('/api/bridge/tasks', { cache: 'no-store' });
        if (!r.ok) throw new Error('任务队列读取失败 ' + r.status);
        return r.json();
      }
      const local = (window.DYStore.state.tasks || []).map((t) => ({
        id: t.id, url: t.url, title: t.title, status: t.status,
        kind: 'douyin', type: t.type, created_at: t.createdAt,
        total: t.total, success: t.success, failed: t.failed, progress: t.progress,
      }));
      return { tasks: local, history: [] };
    },

    /* ================= 账号中心（登录账号的作品 / 喜欢 / 收藏 / 关注） =================
     * 数据由桥接服务的 account_service 用登录态页面通道拉取后缓存；
     * 前端只读缓存，点「同步」才触发后端刷新（喜欢/关注动辄几百条，不能自动刷）。
     */

    async accountStatus(platform) {
      const p = platform ? '?platform=' + encodeURIComponent(platform) : '';
      const r = await fetch('/api/bridge/account/status' + p, { cache: 'no-store' });
      if (!r.ok) throw new Error('账号状态读取失败 ' + r.status);
      return r.json();
    },

    async accountList(tab, limit, platform) {
      const q = '/api/bridge/account/list?tab=' + encodeURIComponent(tab || 'post') +
        (limit ? '&limit=' + limit : '') +
        (platform ? '&platform=' + encodeURIComponent(platform) : '');
      const r = await fetch(q, { cache: 'no-store' });
      if (!r.ok) throw new Error('账号内容读取失败 ' + r.status);
      return r.json();
    },

    async accountSync(tabs, platform) {
      const r = await fetch('/api/bridge/account/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tabs: tabs || null, platform: platform || 'douyin' }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '同步启动失败 ' + r.status);
      return d;
    },

    async accountLogin(platform) {
      const r = await fetch('/api/bridge/account/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform: platform || 'douyin' }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '登录启动失败 ' + r.status);
      return d;
    },

    async accountLoginStatus(platform) {
      const p = platform ? '?platform=' + encodeURIComponent(platform) : '';
      const r = await fetch('/api/bridge/account/login/status' + p, { cache: 'no-store' });
      if (!r.ok) throw new Error('登录状态读取失败 ' + r.status);
      return r.json();
    },

    /* ================= 收藏夹（用户自己标记「还想要的」，服务端持久化） ================= */

    async favFolders() {
      const r = await fetch('/api/bridge/favfolders', { cache: 'no-store' });
      if (!r.ok) throw new Error('收藏夹读取失败 ' + r.status);
      return r.json();
    },

    async favFolderAct(action, payload) {
      const r = await fetch('/api/bridge/favfolders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ action }, payload || {})),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '操作失败 ' + r.status);
      return d;
    },

    async favItems(folder) {
      const r = await fetch('/api/bridge/favitems?folder=' + encodeURIComponent(folder || 'all'),
        { cache: 'no-store' });
      if (!r.ok) throw new Error('收藏列表读取失败 ' + r.status);
      return r.json();
    },

    async favItemAct(action, payload) {
      const r = await fetch('/api/bridge/favitems', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ action }, payload || {})),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '操作失败 ' + r.status);
      return d;
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

    /**
     * 未计入「关注」的作者清单（抖音 / 快手同一形状）。
     *
     * 「关注」不是一张表，而是从作品派生 + 用户显式覆盖（详见后端 q_authors）：
     * 只有存在**主页来源**作品的作者才算关注。单作品链接下到的作品照样进
     * 「作品档案」，但作者不会自动出现在关注列表里 —— 那些人就在这里，
     * 由用户决定要不要「加入关注」。
     */
    async authorsUnfollowed(platform) {
      const p = platform === 'kuaishou' ? 'kuaishou' : 'douyin';
      const r = await fetch('/api/bridge/authors/unfollowed?platform=' + p, { cache: 'no-store' });
      const d = await r.json();
      return (d.authors || []).map((a) => ({
        nickname: a.nickname || a.author_name || '未知作者',
        works: a.works || 0,
        why: a.why || 'single',        // single=只下过单链接 / manual=用户取消关注
        lastTime: a.last_time || a.lastTime || null,
        platform: p,
      }));
    },

    /** 加入 / 取消关注。只改关注状态，不删任何作品记录。 */
    async authorFollow(platform, author, follow) {
      const r = await fetch('/api/bridge/author/follow', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          platform: platform === 'kuaishou' ? 'kuaishou' : 'douyin',
          author, follow: !!follow,
        }),
      });
      let d = {};
      try { d = await r.json(); } catch (e) { /* 非 JSON 按状态码报错 */ }
      if (!r.ok) throw new Error(d.error || ('操作失败 ' + r.status));
      return d;
    },

    /**
     * 删除作者信息：清掉工程库里该作者的全部作品记录（磁盘文件保留）。
     * 这是全站唯一的破坏性接口，后端会在删除前自动整库备份，并把备份路径回传。
     */
    async bridgeAuthorDelete(author) {
      const r = await fetch('/api/bridge/author/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ author }),
      });
      let d = {};
      try { d = await r.json(); } catch (e) { /* 非 JSON 响应按状态码报错 */ }
      if (!r.ok) throw new Error(d.error || ('删除失败 ' + r.status));
      return d;
    },

    /* ============ 快手（模块 2）：本地作品库 ============
     * 快手与抖音是两套**完全独立**的引擎，本地台账格式也不同：
     *   抖音 → SQLite（aweme 表）   快手 → JSON 数组（download_log.json）
     * 所以接口另起一组（/api/bridge/ks/*）。字段在这里就对齐成 workCard 的形状，
     * 页面侧才能复用同一张卡片、同一个播放器、同一套空状态。 */
    async ksWorks(opt) {
      const o = opt || {};
      const qs = new URLSearchParams();
      if (o.author) qs.set('author', o.author);
      if (o.q) qs.set('q', o.q);
      qs.set('limit', String(o.limit || 60));
      qs.set('offset', String(o.offset || 0));
      const r = await fetch('/api/bridge/ks/works?' + qs.toString(), { cache: 'no-store' });
      const d = await r.json();
      return {
        total: d.total || 0,
        items: (d.items || []).map((w) => ({
          id: 'ks-' + w.photoId,
          awemeId: w.photoId,        // 播放/定位都靠这个字段，mediaPlayer 认它
          photoId: w.photoId,
          type: 'video',             // 快手台账只记视频，没有图文作品
          title: w.title || '无标题作品',
          creatorId: null,
          creatorName: w.creatorName || '未知作者',
          createTime: null,          // 台账不记发布时间，如实留空
          downloadTime: w.downloadTime ? U.fmtDateTime(w.downloadTime) : null,
          sizeMB: w.sizeMB != null ? w.sizeMB : null,
          durationSec: w.durationSec || 0,
          quality: w.quality || null,
          coverPath: w.coverPath || null,   // 后端对有文件的作品用 ffmpeg 抽帧生成封面（.data/ks-covers/）；无文件回 null 走程序化占位
          filePath: w.filePath || '',
          local: !!w.localKind,
          localKind: w.localKind || null,
          localImages: 0,
          fav: false,                // 台账没有收藏概念 —— 卡片上不会出现收藏按钮
          platform: 'kuaishou',
          source: 'bridge',
        })),
      };
    },

    async ksAuthors() {
      const r = await fetch('/api/bridge/ks/authors', { cache: 'no-store' });
      const d = await r.json();
      return (d.authors || []).map((a, i) => ({
        id: a.id,
        nickname: a.nickname,
        // 没有主页 ID 时留空 —— 显示一个 "-" 会让人以为是数据显示错了
        douyinId: a.profileId ? '@' + a.profileId : '',
        secUid: '',
        url: a.profileUrl || '',
        // auto = 从下载时留下的作品清单推断出来的；manual = 用户自己贴的。
        // 都没有时「更新」按钮只能请用户先贴一次主页链接。
        profileSource: a.profileSource || null,
        works: a.works || 0,
        withFile: a.withFile || 0,
        sizeMB: a.sizeMB || 0,
        fans: null,
        verified: false,
        mutual: null,
        lastWorkTime: a.lastTime,
        lastDownload: a.lastTime ? U.fmtDate(a.lastTime) : '未知',
        platform: 'kuaishou',
        source: 'bridge',
        colorIndex: i % 6,
      }));
    },

    async ksStats() {
      const r = await fetch('/api/bridge/ks/stats', { cache: 'no-store' });
      return r.json();
    },

    ksMediaUrl(photoId) {
      return '/api/bridge/ks/media?id=' + encodeURIComponent(photoId);
    },

    async ksRevealMedia(photoId) {
      const r = await fetch('/api/bridge/ks/media/reveal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: photoId }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || '定位失败 ' + r.status);
      return d;
    },

    /** 从快手台账里移除一位作者（后端删前会整份备份台账，磁盘文件不动）。 */
    async ksAuthorDelete(author) {
      const r = await fetch('/api/bridge/ks/author/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ author }),
      });
      let d = {};
      try { d = await r.json(); } catch (e) { /* 非 JSON 按状态码报错 */ }
      if (!r.ok) throw new Error(d.error || ('删除失败 ' + r.status));
      return d;
    },

    /** 手动记下某位快手作者的主页链接（自动推断不出来时的兜底）。 */
    async ksAuthorProfile(author, url) {
      const r = await fetch('/api/bridge/ks/author/profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ author, url }),
      });
      let d = {};
      try { d = await r.json(); } catch (e) { /* 同上 */ }
      if (!r.ok) throw new Error(d.error || ('保存失败 ' + r.status));
      return d;
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
          // 本地是否真有文件（后端按 aweme_id 扫盘建索引后回报）：
          //   local_kind === 'video' → 封面可点，点了直接播；
          //   === 'image' → 图集，点了看大图；
          //   null → 文件已不在本地，卡片不给播放入口，点了如实说明。
          local: !!w.local,
          localKind: w.local_kind || null,
          localImages: w.local_images || 0,
          fav: favs.indexOf(String(w.aweme_id)) >= 0,
          source: 'bridge',
        })),
      };
    },

    /** 本地作品文件地址（<video>/<img> 直接用；服务端支持 Range，能拖动进度）。 */
    mediaUrl(awemeId, i, kind) {
      const qs = '?id=' + encodeURIComponent(awemeId);
      if (kind === 'image') return '/api/bridge/media' + qs + '&kind=image&i=' + (i || 0);
      return '/api/bridge/media' + qs;
    },

    /** 在资源管理器中定位该作品的文件（后端 explorer /select）。 */
    async revealMedia(awemeId) {
      const r = await fetch('/api/bridge/media/reveal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: awemeId }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || '定位失败 ' + r.status);
      return d;
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

    /* —— 重复视频扫描（抖音 + 快手一起）—— */
    async dedupScan() {
      const r = await fetch('/api/bridge/dedup/scan', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok && r.status !== 409) throw new Error(d.error || ('扫描失败 ' + r.status));
      return d;
    },

    async dedupStatus() {
      const r = await fetch('/api/bridge/dedup/status', { cache: 'no-store' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || ('读取状态失败 ' + r.status));
      return d;
    },

    async dedupDelete(paths) {
      const r = await fetch('/api/bridge/dedup/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths }),
      });
      let d = {};
      try { d = await r.json(); } catch (e) { /* 非 JSON 按状态码报错 */ }
      if (!r.ok) throw new Error(d.error || ('删除失败 ' + r.status));
      return d;
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
        // 该批次文件的落盘目录（后端按 download_time 从 aweme.file_path 聚合）
        outputDir: h.output_dir || null,
        outputDirs: h.output_dirs || [],
        // 名称 = 文件夹名/文件名；多目录批次显示「第一个 等 N 个」
        outputName: h.output_name || null,
        outputNames: h.output_names || [],
        source: 'bridge-history',
      }));
    },

    /** 分享短链解析：v.kuaishou.com 等短链无法从 URL 区分视频/直播，
     *  后端跟一次重定向判定；直播时返回 room_url（可直接加入监控）。 */
    async resolveLink(url) {
      const r = await fetch('/api/bridge/resolve-link', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: (url || '').trim() }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '解析失败 ' + r.status);
      return d;
    },

    /** 打开一个本机目录（下载历史的「打开目录」；路径须真实存在） */
    async openPath(path) {
      const r = await fetch('/api/bridge/open-dir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: String(path || '') }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '打开失败 ' + r.status);
      return d;
    },

    /** 桥接实时任务（本次浏览器会话发起的下载） */
    async bridgeTasks() {
      const r = await fetch('/api/bridge/tasks', { cache: 'no-store' });
      const d = await r.json();
      return (d.tasks || []).map((t) => {
        const kind = t.kind || 'douyin';
        const platform = kind === 'direct' ? 'direct' : kind === 'kuaishou' ? 'kuaishou' : 'douyin';
        const mode = kind === 'direct' ? 'clawfetch 直链'
          : kind === 'kuaishou' ? ('快手' + (t.mode === '1' ? '·仅最新' : '·全部'))
          : 'CLI run.py';
        // ⚠️⚠️ 进度**必须来自后端解析的真实日志**，不能在这里拍常数。
        // 以前写 `t.status === 'running' ? 50 : 100` → 进度条永远 50% 不动；
        // total/success 全写 0 → 完成后仍显示「等待开始」。
        const pg = t.progress_info || {};
        return {
          id: t.id,
          url: t.url,
          // 直链任务：后端从日志里取回的「真实直链」（中转页 → /stream/ 那种）。
          // 任务中心的复制按钮用它，别回放当初粘进去的取件码地址。
          resolved_urls: t.resolved_urls || [],
          platform,
          type: 'bridge',
          title: (t.status === 'running' ? '正在下载：' : '下载任务：') + U.truncate(t.url, 40),
          status: t.status === 'success' ? 'success'
            : t.status === 'failed' ? 'failed'
            : t.status === 'canceled' ? 'canceled'
            : t.status === 'pending' ? 'pending' : 'running',
          total: pg.total || 0,
          success: pg.done || 0,
          failed: pg.failed || 0,
          skipped: pg.skipped || 0,
          paid: pg.paid || 0,
          // null = 总量未知（还在收集清单等），前端渲染不确定态，不许冒充百分比
          progress: (pg.progress === null || pg.progress === undefined) ? null : pg.progress,
          stage: pg.stage || '',
          note: pg.note || '',
          tail: pg.tail || [],
          // 该任务的输出目录（按 kind 从工具产物目录取）与目录名
          outputDir: t.output_dir || null,
          outputName: t.output_name || null,
          elapsed: pg.elapsed || 0,
          mode,
          createdAt: t.created_at,
          error: t.status === 'failed' ? '进程退出码 ' + t.exit_code + '，查看日志了解详情' : null,
          exitCode: t.exit_code,
          source: 'bridge-live',
        };
      });
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

    /** 哔哩哔哩真实下载：URL 类型由后端自动识别（单作品/主页/直播）。
     *  mode: 'all' | 纯数字 N（主页限量）| 'audio'（单作品仅音频）| 'posts'（图文）| 'live' */
    async biliBridgeSubmit(url, mode) {
      const r = await fetch('/api/bridge/bili/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, mode: mode || 'all' }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '提交失败 ' + r.status);
      return d.task;
    },

    /** 哔哩哔哩 Cookie：查看（脱敏） */
    async biliCookieGet() {
      const r = await fetch('/api/bridge/bili/cookie', { cache: 'no-store' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '读取失败 ' + r.status);
      return d;
    },

    /** 哔哩哔哩 Cookie：保存 / 清除（空串=清除） */
    async biliCookieSet(cookie) {
      const r = await fetch('/api/bridge/bili/cookie', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cookie: cookie || '' }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '保存失败 ' + r.status);
      return d;
    },

    /* ---- 哔哩哔哩扫码登录（官方 passport 接口，成功后 Cookie 自动落盘） ---- */

    /** 开始扫码：返回 { session, svg }（svg 是二维码矢量图，直接内嵌） */
    async biliQrStart() {
      const r = await fetch('/api/bridge/bili/qr/start', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '二维码获取失败 ' + r.status);
      return d;
    },

    /** 轮询扫码结果：state = waiting | scanned | confirmed | expired | failed */
    async biliQrPoll(session) {
      const r = await fetch('/api/bridge/bili/qr/poll?session=' + encodeURIComponent(session || ''), { cache: 'no-store' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '状态读取失败 ' + r.status);
      return d;
    },

    /** 放弃本次扫码会话 */
    async biliQrCancel(session) {
      const r = await fetch('/api/bridge/bili/qr/cancel?session=' + encodeURIComponent(session || ''), { method: 'POST' });
      return r.json().catch(() => ({}));
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

    /** 保存设置到工程 config.yml（bridge 模式）。后端有白名单，非白名单键会被拒。 */
    async bridgeSaveConfig(settings) {
      const r = await fetch('/api/bridge/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: settings || {} }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        const extra = (d.details && d.details.length) ? ('：' + d.details.slice(0, 3).join('；')) : '';
        throw new Error((d.error || ('保存失败 ' + r.status)) + extra);
      }
      return d;
    },

    /** 读网页端专属设置（不属于 config.yml 的键，如 creators_update_*）。 */
    async bridgeWebuiSettings() {
      const r = await fetch('/api/bridge/webui-settings', { cache: 'no-store' });
      return r.json();
    },

    /** 全部恢复默认：后端把 SETTABLE_KEYS 各键写回 default_config.py 出厂值
     *  （写前自动备份 config.yml）；path 与 cookies 不在重置范围。 */
    async bridgeResetConfig() {
      const r = await fetch('/api/bridge/config/reset', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || '恢复默认失败 ' + r.status);
      return d;
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

    /**
     * 重试一个已结束的任务。
     *
     * ⚠️ bridge 模式下这里必须**真的发请求**。原实现只改前端 store 的状态再跑
     *    一段模拟进度 —— 也就是「点了重试什么都没发生」，而用户以为重新下上了
     *    （2026-11-01 直链批量 6/11 那次就是这么被误导的）。真实重试由后端
     *    POST /api/bridge/task/{id}/retry 重新起进程；直链批量只补失败的那几条。
     * 返回 Promise（调用方要处理失败，别再 fire-and-forget）。
     */
    async retryTask(id) {
      if (this.isBridge()) {
        const r = await fetch('/api/bridge/task/' + encodeURIComponent(id) + '/retry', {
          method: 'POST',
        });
        let d = {};
        try { d = await r.json(); } catch (e) { /* 非 JSON 按状态码报错 */ }
        if (!r.ok) throw new Error(d.error || ('重试失败 ' + r.status));
        return d;
      }
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
