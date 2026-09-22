/* ============================================================
 * mock-data.js — 内置演示数据（首次运行时灌入数据层）
 * ------------------------------------------------------------
 * ★ 全部为演示数据：作者与作品均为虚构（作者名单取自官方
 *   桌面端参考截图所用的演示数据风格），不涉及真实用户隐私。
 *   想换成真实数据：改这个文件，或在设置页“数据台账”里重置。
 * ============================================================ */
(function () {
  'use strict';

  const now = Date.now();
  const H = 3600 * 1000;
  const D = 24 * H;
  const iso = (ms) => new Date(ms).toISOString();

  /* —— 作者库（对应“关注”列表；字段与数据库 aweme 表 / 目录结构对齐） —— */
  const CREATORS = [
    {
      id: 'c1', nickname: '旅行摄影日记', douyinId: '@travel_daily', verified: true,
      secUid: 'MS4wLjABAAAA6O7E…d8ss', url: 'https://www.douyin.com/user/MS4wLjABAAAA6O7E',
      bio: '持续分享值得收藏的内容', note: '旅行素材', group: '摄影',
      works: 426, fans: 328000, newWorks: 1, mutual: true,
      lastSync: iso(now - 2 * 60000), lastDownload: '昨天',
      modes: ['post'], autoSync: true, colorIndex: 0,
    },
    {
      id: 'c2', nickname: '城市漫游指南', douyinId: '@citywalk_lab', verified: false,
      secUid: 'MS4wLjABAAAAFymv…h8', url: 'https://www.douyin.com/user/MS4wLjABAAAAFymv',
      bio: '持续分享值得收藏的内容', note: '城市选题', group: '生活',
      works: 238, fans: 186000, newWorks: 2, mutual: false,
      lastSync: iso(now - 26 * H), lastDownload: '未下载过',
      modes: ['post'], autoSync: true, colorIndex: 1,
    },
    {
      id: 'c3', nickname: '手作研究所', douyinId: '@craft_studio', verified: true,
      secUid: 'MS4wLjABAAAACwMd…XJI', url: 'https://www.douyin.com/user/MS4wLjABAAAACwMd',
      bio: '每周三更新手工教程合集', note: '教程参考', group: '手工',
      works: 181, fans: 94000, newWorks: 3, mutual: true,
      lastSync: iso(now - 9 * H), lastDownload: '3 天前',
      modes: ['post', 'mix'], autoSync: false, colorIndex: 2,
    },
    {
      id: 'c4', nickname: '猫咪观察员', douyinId: '@cat_observer', verified: false,
      secUid: 'MS4wLjABAAAAXkQm…tKq', url: 'https://www.douyin.com/user/MS4wLjABAAAAXkQm',
      bio: '街猫记录 plan 日常投喂', note: '', group: '萌宠',
      works: 612, fans: 582000, newWorks: 0, mutual: false,
      lastSync: iso(now - 50 * H), lastDownload: '昨天',
      modes: ['post'], autoSync: false, colorIndex: 3,
    },
    {
      id: 'c5', nickname: '胶片冲印部', douyinId: '@film_lab_1998', verified: false,
      secUid: 'MS4wLjABAAAApQ2m…vNs', url: 'https://www.douyin.com/user/MS4wLjABAAAApQ2m',
      bio: '老相机与胶片色彩研究', note: '调色参考', group: '摄影',
      works: 97, fans: 45000, newWorks: 0, mutual: true,
      lastSync: iso(now - 74 * H), lastDownload: '上周',
      modes: ['post', 'music'], autoSync: false, colorIndex: 4,
    },
    {
      id: 'c6', nickname: '深夜厨房日记', douyinId: '@midnight_kitchen', verified: false,
      secUid: 'MS4wLjABAAAAtG8w…Lm2', url: 'https://www.douyin.com/user/MS4wLjABAAAAtG8w',
      bio: '一人食夜宵记录，偶尔翻车', note: '', group: '美食',
      works: 203, fans: 121000, newWorks: 5, mutual: false,
      lastSync: iso(now - 3 * D), lastDownload: '未下载过',
      modes: ['post'], autoSync: true, colorIndex: 5,
    },
    {
      id: 'c7', nickname: '小熊软糖🐻（旧号迁移）', douyinId: '@gummy_bear_moved', verified: false,
      secUid: 'MS4wLjABAAAAedge…case', url: 'https://www.douyin.com/user/MS4wLjABAAAAedge',
      bio: '这是一条特别长的备注：用于测试超长昵称、emoji 与长备注在列表卡片里的换行与截断表现，确保界面不会崩坏',
      note: '边界用例（长文本）', group: '未分组',
      works: 46, fans: 8200, newWorks: 0, mutual: false,
      lastSync: iso(now - 6 * D), lastDownload: '未下载过',
      modes: ['post'], autoSync: false, colorIndex: 1,
    },
  ];

  /* —— 本地作品库（对应数据库 aweme 表 + Downloaded 目录结构） —— */
  const WORK_TITLES = [
    '完整版：爆米花级别的夯爆了《Mayday》！ #新片速递', '秋天的第一场 citywalk 路线全记录',
    '把出租屋改成胶片感 studio，全流程记录', '三分钟讲清楚镜头焦段怎么选',
    '深夜食堂 ep.12：一碗完美的葱油拌面', 'no_title', '雨后的巷子（图文）', 'no_title',
    '海边日落延时摄影合集 | 4K', '健身两个月，体态变化对比', '集市里的旧相机摊',
    '教程：手机也能拍的追焦运镜', '穿过菜市场的光（图文）', '周五晚上的天台',
    '老小区的猫，第 7 次投喂', '收纳房间的 9 个细节', '地铁通勤随拍',
    '咖啡拉花练习 Day 30', '冰岛环岛自驾 vlog ep.3', '周末骑行 60km 记录',
  ];
  const WORKS = WORK_TITLES.map((title, i) => {
    const c = CREATORS[i % CREATORS.length];
    const type = /（图文）/.test(title) ? 'note' : 'video';
    const created = now - (i * 2.6 + 1) * D - (i % 5) * 3 * H;
    const size = type === 'video' ? 3.2 + ((i * 7.3) % 58) : 1.4 + ((i * 3.1) % 9);
    return {
      id: 'w' + (i + 1),
      awemeId: String(7660000000000000000 + i * 7919),
      type,
      title,
      creatorId: c.id,
      creatorName: c.nickname,
      createTime: iso(created),
      downloadTime: iso(created + 2 * H),
      sizeMB: Math.round(size * 10) / 10,
      durationSec: type === 'video' ? 9 + ((i * 37) % 620) : 0,
      quality: ['1080p', 'highest', '720p', 'highest'][i % 4],
      cover: i % 12, // 缩略图索引 → assets/img/thumbs/thumb-<n>.jpg（0-11，缺失时自动用程序化封面）
      filePath: 'Downloaded/' + c.nickname + '/post/' +
        new Date(created).toISOString().slice(0, 10) + '_' + (title === 'no_title' ? 'no_title' : '作品') +
        '_' + String(7660000000000000000 + i * 7919).slice(-6),
      fav: i === 2 || i === 8,
    };
  });

  /* —— 任务列表（对应数据库 job / download_history 表结构） —— */
  const TASKS = [
    { id: 'j-kx91m2', url: 'https://v.douyin.com/R9-1-a2FKfE/', platform: 'douyin', type: 'user',
      title: '旅行摄影日记 · 主页作品', status: 'success', total: 628, success: 628, failed: 0, skipped: 41,
      createdAt: iso(now - 6 * H), finishedAt: iso(now - 5.4 * H), progress: 100, mode: 'post' },
    { id: 'j-p83nqs', url: 'https://www.douyin.com/user/MS4wLjABAAAAFymv', platform: 'douyin', type: 'user',
      title: '城市漫游指南 · 主页作品+合集', status: 'running', total: 325, success: 187, failed: 0, skipped: 96,
      createdAt: iso(now - 40 * 60000), finishedAt: null, progress: 57, mode: 'post+mix' },
    { id: 'j-tz49ab', url: 'https://www.douyin.com/video/7604129988555574538', platform: 'douyin', type: 'video',
      title: '完整版：爆米花级别的夯爆了《Mayday》！', status: 'success', total: 1, success: 1, failed: 0, skipped: 0,
      createdAt: iso(now - 9 * H), finishedAt: iso(now - 8.9 * H), progress: 100, mode: 'video' },
    { id: 'j-me22xd', url: 'https://www.douyin.com/user/MS4wLjABAAAAhq7T', platform: 'douyin', type: 'user',
      title: '手作研究所 · 主页作品', status: 'failed', total: 71, success: 0, failed: 1, skipped: 0,
      createdAt: iso(now - 27 * H), finishedAt: iso(now - 26.8 * H), progress: 0, mode: 'post',
      error: 'APIError: 分页请求被风控拦截（-1），建议开启浏览器兜底或更换 Cookie 后重试' },
    { id: 'j-qq70zw', url: 'https://www.douyin.com/collection/7341234567890123456', platform: 'douyin', type: 'collection',
      title: '镜头语言入门 · 合集', status: 'pending', total: 0, success: 0, failed: 0, skipped: 0,
      createdAt: iso(now - 3 * 60000), finishedAt: null, progress: 0, mode: 'collection' },
    { id: 'j-bn15rk', url: 'https://www.tiktok.com/@cameraboy101/video/7299123456789012345', platform: 'tiktok', type: 'video',
      title: 'TikTok · 镜头配件开箱', status: 'success', total: 1, success: 1, failed: 0, skipped: 0,
      createdAt: iso(now - 2 * D), finishedAt: iso(now - 2 * D + 4 * 60000), progress: 100, mode: 'video' },
    { id: 'j-lf63vh', url: 'https://www.youtube.com/watch?v=aqz-KE-bpKQ', platform: 'youtube', type: 'video',
      title: 'YouTube · 4K 城市夜景素材', status: 'success', total: 1, success: 1, failed: 0, skipped: 0,
      createdAt: iso(now - 3 * D), finishedAt: iso(now - 3 * D + 12 * 60000), progress: 100, mode: 'yt-video' },
    { id: 'j-dt87cs', url: 'https://www.youtube.com/playlist?list=PLM0sd1', platform: 'youtube', type: 'playlist',
      title: 'YouTube · 剪辑教程播放列表', status: 'canceled', total: 24, success: 9, failed: 0, skipped: 0,
      createdAt: iso(now - 4 * D), finishedAt: iso(now - 4 * D + 1.2 * H), progress: 37, mode: 'yt-video' },
    { id: 'j-wq51jn', url: 'https://v.douyin.com/E3JnctpJaRo/', platform: 'douyin', type: 'user',
      title: '猫咪观察员 · 主页作品', status: 'success', total: 156, success: 155, failed: 1, skipped: 18,
      createdAt: iso(now - 5 * D), finishedAt: iso(now - 5 * D + 0.6 * H), progress: 100, mode: 'post',
      error: '1 个作品下载失败：视频已删除' },
  ];

  /* —— 收藏与喜欢（对应登录账号的收藏夹/收藏合集/喜欢列表） —— */
  const FAV_COLLECTIONS = [
    { id: 'fc1', title: '镜头语言入门', count: 24, creator: '手作研究所', cover: 8, syncedAt: iso(now - 12 * 60000) },
    { id: 'fc2', title: '探店清单 2026', count: 41, creator: '深夜厨房日记', cover: 4, syncedAt: iso(now - 3 * H) },
    { id: 'fc3', title: '胶片感调色参考', count: 12, creator: '胶片冲印部', cover: 2, syncedAt: iso(now - 26 * H) },
  ];
  const FAV_VIDEOS = [2, 4, 9, 12, 16, 19].map((i) => ({
    id: 'fv' + i, work: WORKS[i % WORKS.length].id,
    kind: i % 3 === 2 ? 'collection' : 'video',
  }));

  window.DYMock = { CREATORS, WORKS, TASKS, FAV_COLLECTIONS, FAV_VIDEOS, generatedAt: iso(now) };
})();
