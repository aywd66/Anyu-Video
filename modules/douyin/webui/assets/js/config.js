/* ============================================================
 * config.js — 全站集中配置（文案 / 导航 / 选项 / 平台参数）
 * ------------------------------------------------------------
 * ★ 这是"改文案只改这里"的核心文件。
 *   - 想改菜单名称、按钮文字、提示语 → 搜对应中文直接改
 *   - 想改默认下载参数 → DEFAULT_SETTINGS
 *   - 想加页面 → PAGES 加一条 + 新建 pages/<id>.js 注册路由
 *   注意：本文件只放"配置"，业务逻辑一律不写进来。
 * ============================================================ */
(function () {
  'use strict';

  window.DYConfig = {
    APP: {
      name: 'Douzy',            // 品牌字标（跟随官方桌面端命名）
      fullName: '抖音下载器',
      envLabel: 'WEB',          // 侧边栏 Logo 上方的小标签
      version: 'V1.0',          // 底部版本条
    },

    /* —— 导航结构（对照参考界面的侧边栏分组） —— */
    NAV: {
      /* 平台分段控件：点击切换三个平台工作台 */
      platforms: [
        { id: 'douyin',  label: '抖音',    icon: 'download', dot: 'pink',  default: true, sub: '视频 · 图集 · 主页 · 合集' },
        { id: 'tiktok',  label: 'TikTok',  icon: 'tiktok',   dot: 'cyan',  sub: 'Video · Photo · Profile' },
        { id: 'youtube', label: 'YouTube', icon: 'youtube',  dot: 'red',   sub: 'Videos · Shorts · Playlists' },
      ],
      /* 侧边栏菜单（group = 分组小标签） */
      menu: [
        { id: 'creators',  label: '关注',        icon: 'users',    group: '账号内容' },
        { id: 'favorites', label: '收藏与喜欢',  icon: 'bookmark', group: '账号内容' },
        { id: 'douyin',    label: '链接下载',    icon: 'download', group: '下载与管理', mirrorPlatform: true },
        { id: 'queue',     label: '多链排队',    icon: 'layers',   group: '下载与管理' },
        { id: 'tasks',     label: '任务中心',    icon: 'tasks',    group: '下载与管理', badge: true },
        { id: 'archive',   label: '作品档案',    icon: 'clock',    group: '下载与管理' },
        { id: 'settings',  label: '设置',        icon: 'settings', group: '系统' },
      ],
      /* 平台页与菜单项互为镜像时，菜单显示的平台归属 */
      platformLabel: { douyin: '抖音', tiktok: 'TikTok', youtube: 'YouTube' },
      /* 各平台账号卡文案（侧边栏随平台切换） */
      account: {
        douyin:  { name: '抖音账号',   meta: '已登录',           metaTone: 'green', icon: 'users' },
        tiktok:  { name: 'TikTok',     meta: '公开内容无需登录', metaTone: 'cyan',  icon: 'globe' },
        youtube: { name: 'YouTube 账号', meta: '已验证',          metaTone: 'green', icon: 'youtube' },
      },
    },

    /* —— 链接类型提示（粘贴链接后前端即时判断，真实解析以后端为准） —— */
    URL_TYPE_HINT: {
      short:      { label: '短链',     desc: '将自动解析为具体作品或主页' },
      video:      { label: '单个作品', desc: '点击「下载」即可' },
      note:       { label: '图文作品', desc: '图片将打包保存' },
      collection: { label: '合集',     desc: '合集内全部作品' },
      music:      { label: '音乐',     desc: '优先原声文件' },
      live:       { label: '直播录制', desc: '录到主播下播（实验性）' },
      user:       { label: '作者主页', desc: '按下方所选模式批量下载' },
      unknown:    { label: '待解析',   desc: '支持：视频 / 图文 / 主页 / 合集 / 音乐 / 短链' },
    },

    /* —— 主页批量下载模式（与 config.yml 的 mode 对应） —— */
    MODES: [
      { id: 'post',       label: '作品',     desc: '作者发布的全部作品' },
      { id: 'like',       label: '喜欢',     desc: '作者公开的喜欢列表' },
      { id: 'mix',        label: '合集',     desc: '作者创建的合集' },
      { id: 'music',      label: '音乐',     desc: '作者使用的原声' },
      { id: 'collect',    label: '收藏夹',   desc: '仅当前登录账号，需单独使用' },
      { id: 'collectmix', label: '收藏合集', desc: '仅当前登录账号，需单独使用' },
    ],

    /* —— 视频画质选项（与 config.yml 的 video_quality 对应） —— */
    QUALITY: [
      { id: 'highest',  label: '最高清（自动）' },
      { id: 'original', label: '原画（探测原片，较慢）' },
      { id: 'lowest',   label: '最低清（省流量）' },
      { id: '1080p',    label: '1080p' },
      { id: '720p',     label: '720p' },
      { id: '480p',     label: '480p' },
    ],

    /* —— YouTube 下载格式选项 —— */
    YT_FORMAT: [
      { id: 'video',    label: '视频 MP4' },
      { id: 'audio',    label: '音频 MP3' },
      { id: 'subtitle', label: '仅字幕' },
    ],

    /* —— 作者目录命名方式（设置页） —— */
    AUTHOR_DIR: [
      { id: 'nickname',     label: '昵称', desc: '直观，重名会合并' },
      { id: 'sec_uid',      label: 'sec_uid', desc: '稳定唯一，不直观' },
      { id: 'nickname_uid', label: '昵称_sec_uid', desc: '直观且唯一，推荐重度用户' },
    ],

    /* —— 三个平台工作台的差异（共用一套页面模板） —— */
    PLATFORMS: {
      douyin: {
        name: '抖音',
        accent: 'pink',
        crumb: '抖音 · Download',
        cardLabel: '抖音 URL',
        titleA: '粘贴抖音链接，',
        titleB: '马上开始。',
        sub: '视频、图文、主页合集一键下载；默认无水印，支持增量去重。',
        placeholder: 'https://www.douyin.com/video/… 或直接粘贴分享口令',
        accepts: '视频 · 图集 · 主页 · 合集',
        tips: '支持 v.douyin.com 短链自动解析；主页链接按所选模式批量下载。',
        options: ['mode', 'number', 'quality', 'increase'],
        sample: 'https://www.douyin.com/video/7412345678901234567',
      },
      tiktok: {
        name: 'TikTok',
        accent: 'cyan',
        crumb: 'TikTok · Download',
        cardLabel: 'TikTok URL',
        titleA: '粘贴 TikTok 链接，',
        titleB: '马上开始。',
        sub: '公开视频与图集无需登录即可下载；主页批量按所选模式执行。',
        placeholder: 'https://www.tiktok.com/@user/video/…',
        accepts: '视频 · 图集 · 主页',
        tips: '公开内容无需登录；主页批量下载按所选模式执行。',
        options: ['mode', 'number', 'quality'],
        sample: 'https://www.tiktok.com/@creator/video/7299000000000000000',
      },
      youtube: {
        name: 'YouTube',
        accent: 'amber',
        crumb: 'YouTube Workbench',
        cardLabel: 'SCAN',
        cardTitle: '输入 YouTube 链接',
        titleA: '扫描 YouTube，',
        titleB: '精准下载。',
        sub: '面向频道 / 播放列表 / Shorts 的独立工作台：先扫描、勾选，再下载视频、MP3 或字幕；公共设置页负责默认质量与命名，这里的选择只影响本次任务。',
        placeholder: 'https://www.youtube.com/watch?v=… 或播放列表链接',
        accepts: '视频 · Shorts · 频道 · 播放列表',
        tips: '频道与播放列表会展开为队列逐个下载；MP3 模式仅提取音轨。',
        options: ['ytFormat', 'number', 'quality'],
        sample: 'https://www.youtube.com/watch?v=aqz-KE-bpKQ',
      },
    },

    /* —— 设置页表单 schema（加一项设置 = 加一个字段，页面自动渲染） —— */
    SETTINGS_SCHEMA: [
      {
        group: '下载',
        fields: [
          { key: 'path', type: 'text', label: '保存目录', desc: '相对路径基于工程根目录', mono: true },
          { key: 'thread', type: 'number', label: '并发线程数', min: 1, max: 16 },
          { key: 'retry_times', type: 'number', label: '失败重试次数', min: 0, max: 10 },
          { key: 'rate_limit', type: 'number', label: '速率限制（请求/秒）', min: 1, max: 20 },
          { key: 'video_quality', type: 'select', label: '视频画质', options: 'QUALITY' },
          { key: 'video', type: 'switch', label: '保存视频本体' },
          { key: 'music', type: 'switch', label: '保存音乐/原声' },
          { key: 'cover', type: 'switch', label: '保存封面' },
          { key: 'avatar', type: 'switch', label: '保存头像' },
          { key: 'json', type: 'switch', label: '保存原始 JSON 元数据' },
        ],
      },
      {
        group: '增量与去重',
        fields: [
          { key: 'database', type: 'switch', label: '启用 SQLite 记录', desc: '增量下载依赖数据库' },
          { key: 'database_path', type: 'text', label: '数据库文件', mono: true },
          { key: 'redownload_missing_files', type: 'switch', label: '磁盘文件缺失时重新下载' },
          { key: 'increase_post', type: 'switch', label: '作品模式增量下载' },
          { key: 'increase_like', type: 'switch', label: '喜欢模式增量下载' },
          { key: 'increase_mix', type: 'switch', label: '合集模式增量下载' },
          { key: 'increase_music', type: 'switch', label: '音乐模式增量下载' },
        ],
      },
      {
        group: '文件与目录',
        fields: [
          { key: 'folderstyle', type: 'switch', label: '按日期建立子目录' },
          { key: 'author_dir', type: 'select', label: '作者目录命名', options: 'AUTHOR_DIR', desc: '切换只影响后续下载，不迁移已有目录' },
          { key: 'filename_template', type: 'text', label: '文件名模板', mono: true, desc: '可用变量：{date} {title} {id} {author} {type}' },
          { key: 'download_pinned', type: 'switch', label: '包含置顶作品' },
        ],
      },
      {
        group: '网络与兜底',
        fields: [
          { key: 'proxy', type: 'text', label: '代理地址', placeholder: 'http://127.0.0.1:7890', mono: true },
          { key: 'browser_fallback', type: 'switch', label: '启用浏览器兜底', desc: 'API 翻页受限时自动启动浏览器' },
          { key: 'browser_headless', type: 'switch', label: '浏览器无头模式' },
          { key: 'notifications_enabled', type: 'switch', label: '下载完成推送通知', desc: 'Bark / Telegram / Webhook，在 config.yml 里配置具体通道' },
        ],
      },
    ],

    /* —— 任务状态 —— */
    TASK_STATUS: {
      pending:  { label: '排队中', tone: 'muted' },
      running:  { label: '下载中', tone: 'cyan' },
      success:  { label: '已完成', tone: 'green' },
      failed:   { label: '失败',   tone: 'red' },
      canceled: { label: '已取消', tone: 'faint' },
      'muted-done': { label: '无新增', tone: 'faint' },
    },

    /* —— 通用文案 —— */
    TEXT: {
      download: '下载',
      parse: '解析',
      startDownload: '开始下载',
      enqueue: '加入队列',
      clearInput: '清空',
      retry: '重试',
      cancel: '取消',
      remove: '移除',
      confirm: '确认',
      close: '关闭',
      save: '保存',
      saved: '已保存',
      reset: '恢复默认',
      copy: '复制',
      copied: '已复制到剪贴板',
      openFolder: '打开所在目录',
      manage: '管理',
      sync: '同步',
      syncing: '同步中…',
      selectAll: '全选',
      exportData: '导出数据',
      resetDemo: '重置演示数据',
      dataLedger: '数据台账',
      demoBadge: '演示数据',
      recognized: '已识别',
      recognizedSingle: '单个作品 · 点击「下载」即可',
      loginOk: '已登录',
      loginOff: '未登录',
      netDiag: '网络诊断',
      diagTitle: '网络诊断',
    },

    /* —— 空状态文案（每页不同） —— */
    EMPTY: {
      douyin:   { title: '还没有下载任务', desc: '在上方粘贴链接，添加第一个下载任务' },
      tiktok:   { title: '还没有 TikTok 任务', desc: '粘贴公开视频或主页链接开始' },
      youtube:  { title: '还没有 YouTube 任务', desc: '支持视频、Shorts、频道与播放列表' },
      queue:    { title: '队列为空', desc: '在下方文本框粘贴多行链接，一次性加入队列' },
      tasks:    { title: '没有匹配的任务', desc: '换个筛选条件，或去工作台新建下载' },
      creators: { title: '还没有关注的作者', desc: '下载作者主页作品后会自动收录到这里' },
      favorites:{ title: '暂无收藏内容', desc: '同步登录账号后，收藏视频与合集会显示在这里' },
      archive:  { title: '作品档案为空', desc: '完成的下载任务会自动归档到这里' },
      search:   { title: '没有匹配结果', desc: '换个关键词试试' },
    },
  };
})();
